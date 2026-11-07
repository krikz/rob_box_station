#!/usr/bin/env python3
"""Хост колонки (ADR-0160 §8 К2, #4247): микрофон → VAD → STT → ход v2 → TTS → динамик, ядро на самой колонке.

Ход строит тот же стенд, что консоль (``scripts/dialogue/chat.py``): настоящие адресация (вейк «Робот»), Tier-1, цепочка
LLM с мостом команд и исполнитель; владельцы, кроме речи, фейковые. Ядро грузится один раз, дальше процесс слушает.

Звук — штатные ``arecord``/``aplay`` колонки: микрофон ``plughw:0,1`` (PDM, ALSA отдаёт 16 кГц моно), громкость —
``amixer sset Master`` при старте. Эхоподавления нет, поэтому полудуплекс: пока колонка говорит, микрофон
не слушается. VAD — по энергии с плавающим порогом шума.

    python3.12 -X utf8 station_host.py                 # слушать и отвечать
    python3.12 -X utf8 station_host.py --say "текст"   # только синтез в динамик (проверка TTS)
    python3.12 -X utf8 station_host.py --selftest      # колонка говорит фразу и сама её распознаёт
    python3.12 -X utf8 station_host.py --turn "Игорёк, который час?"  # один ход без микрофона, звук живой
"""
from __future__ import annotations

import argparse
import asyncio
import audioop  # в 3.12 ещё есть (удалён в 3.13); numpy на колонке нет
import io
import os
import queue
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

BASE = Path(os.environ.get("RBX_BASE", "/data/opt/rbx"))
CORE = BASE / "core"
for sub in ("site", "core", "core/scripts/dialog", "core/scripts/dialogue"):
    sys.path.insert(0, str(BASE / sub))
for pkg in ("rob_box_core", "rob_box_dialog", "rob_box_harness", "rob_box_llm", "rob_box_voice", "rob_box_mcp_tools"):
    sys.path.insert(0, str(CORE / "src" / pkg))

RATE = 16000
FRAME_S = 0.03
FRAME_IN = int(RATE * FRAME_S) * 2  # байт кадра 30 мс, 16 кГц моно
MIC_DEVICE = os.environ.get("RBX_MIC", "plughw:0,1")
VOLUME = os.environ.get("RBX_VOLUME", "230")
#: Вейк колонки — свой, чтобы не отзываться вместе с роботом (Шифу 07.11: «пусть будет Игорёк»).
#: Порядок важен: длинные формы первыми (матчинг по словам, как в wake_words.yaml робота).
#: «Егорёк» — частое искажение STT (догадка, не замер).
WAKE_WORDS = [w.strip() for w in os.environ.get(
    "RBX_WAKE", "игорёчек,игоречек,игорёк,игорек,егорёк,егорек").split(",") if w.strip()]
START_FRAMES, END_SILENCE_S, MIN_S, MAX_S = 5, 0.8, 0.7, 12.0
TTS_FORMAT, TTS_RATE = "pcm_24000", 24000
#: Потоковый синтез (SSE MiniMax): звук играет с первого куска, а не после всей фразы. 0 — целым куском (сравнение).
TTS_STREAM = os.environ.get("RBX_TTS_STREAM", "1") != "0"
#: Earcon «услышал»: ``sound_pack/button_click.mp3`` робота → s16le 24 кГц моно (ffmpeg на ПК, mp3 колонка не играет).
HEARD_PCM = BASE / "assets" / "heard.raw"
#: Хедж запроса LLM: DeepSeek на тот же промпт отвечает то за 2 с, то за 12 с (замер 07.11, ~4 из 15 запросов
#: 6–12 с). Не ответил за столько секунд — тот же запрос уходит второй раз, берётся первый ответ. 0 — выключено.
HEDGE_S = float(os.environ.get("RBX_HEDGE_S", "4"))


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def wav_bytes(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Mic:
    """Кадры 30 мс, 16 кГц моно, канал 0 ``multi_capture``. Читает отдельный поток в очередь: пока ход ждёт STT или LLM,
    ``arecord`` не переполняется (overrun). ``muted`` — кадры выбрасываются (полудуплекс)."""

    def __init__(self) -> None:
        # plughw:0,1 — PDM-микрофоны; ALSA сама отдаёт 16 кГц моно (канал 0). Резать 6 каналов и ресемплить на Питоне
        # нельзя: на A53 поток микрофона под GIL растягивал ход консоли с 3,6 до 22 с (замер 07.11).
        self.proc = subprocess.Popen(["arecord", "-q", "-D", MIC_DEVICE, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                                      "-t", "raw"], stdout=subprocess.PIPE, bufsize=0)
        self.state = None
        self.muted = False
        self.q: "queue.Queue[bytes]" = queue.Queue(maxsize=int(30 / FRAME_S))  # 30 с запаса
        threading.Thread(target=self._pump, daemon=True).start()

    def _read(self) -> bytes:
        raw = b""
        while len(raw) < FRAME_IN:
            chunk = self.proc.stdout.read(FRAME_IN - len(raw))
            if not chunk:
                raise RuntimeError(f"arecord закрылся, код {self.proc.poll()}")
            raw += chunk
        return raw

    def _pump(self) -> None:
        while True:
            fr = self._read()
            if self.muted:
                continue
            try:
                self.q.put_nowait(fr)
            except queue.Full:
                self.q.get_nowait()
                self.q.put_nowait(fr)

    def flush(self) -> None:
        while not self.q.empty():
            self.q.get_nowait()

    def frame(self) -> bytes:
        return self.q.get()


class Vad:
    """Реплика по энергии: порог = шум × 4, не ниже 150; старт — 5 кадров из 10 выше порога, конец — 0,8 с тишины.

    Замер 07.11 (PDM Gain 0): шум 13–45 RMS, голос Шифу с метра — до ~200, свой динамик — 200–350."""

    def __init__(self) -> None:
        self.noise = 300.0
        self.dbg = os.environ.get("RBX_DEBUG") == "1"
        self.n, self.peak, self.t = 0, 0, time.time()

    def _debug(self, rms: int, thr: float, inside: bool) -> None:
        if not self.dbg:
            return
        self.n, self.peak = self.n + 1, max(self.peak, rms)
        if time.time() - self.t >= 3:
            log(f"vad: кадров {self.n}, пик {self.peak}, шум {self.noise:.0f}, порог {thr:.0f}, в реплике {inside}")
            self.n, self.peak, self.t = 0, 0, time.time()

    def utterance(self, mic: Mic) -> bytes:
        hits, pre, buf, silent = [], [], [], 0.0
        while True:
            fr = mic.frame()
            rms = audioop.rms(fr, 2)
            thr = max(150.0, self.noise * 4)
            self._debug(rms, thr, bool(buf))
            if not buf:
                if rms < thr:
                    self.noise = 0.98 * self.noise + 0.02 * rms
                pre = (pre + [fr])[-10:]
                hits = (hits + [rms > thr])[-10:]
                if sum(hits) >= START_FRAMES:  # речь рваная: 5 кадров из 10 выше порога
                    buf = list(pre)
                continue
            buf.append(fr)
            silent = silent + FRAME_S if rms <= thr else 0.0
            dur = len(buf) * FRAME_S
            if (silent >= END_SILENCE_S and dur >= MIN_S) or dur >= MAX_S:
                return b"".join(buf)


def aplay_cmd(rate: int = TTS_RATE) -> list[str]:
    return ["aplay", "-q", "-D", "default", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-t", "raw"]


def play(pcm: bytes, rate: int = TTS_RATE) -> None:
    subprocess.run(aplay_cmd(rate), input=pcm, check=False)


class _NoAudioYet(Exception):
    """Поток TTS упал до первого куска звука — можно повторить целым синтезом, слушатель ничего не услышал."""


class Voice:
    """STT и TTS MiniMax (ключи из ``scripts/dialogue/.env``)."""

    def __init__(self) -> None:
        from rob_box_llm.providers.minimax_tts import MiniMaxTTSProvider  # noqa: PLC0415
        from rob_box_voice.stt_providers.minimax_provider import MiniMaxSTTProvider  # noqa: PLC0415

        self.tts = MiniMaxTTSProvider()
        self.stt = MiniMaxSTTProvider.maybe_from_env()
        if self.stt is None:
            raise SystemExit("нет MINIMAX_API_KEY для STT")
        self.limited = False

    async def say(self, text: str, mic: Mic | None = None, on_sound=None, voice: str | None = None) -> float:
        """Синтез и проигрывание; возвращает время до первого звука от начала синтеза."""
        if mic:
            mic.muted = True
        try:
            if TTS_STREAM:
                try:
                    return await self._say_stream(text, on_sound, voice)
                except _NoAudioYet as exc:
                    log(f"поток TTS не дал звука ({exc.__cause__!r}) — синтез целым куском")
            return await self._say_whole(text, on_sound, voice)
        finally:
            if mic:
                await asyncio.sleep(0.3)  # хвост эха комнаты
                mic.flush()
                mic.muted = False

    async def _say_whole(self, text: str, on_sound, voice: str | None = None) -> float:
        t0 = time.time()
        pcm = await self.tts.synthesize_bytes(text, voice, format=TTS_FORMAT)
        synth = time.time() - t0
        if on_sound:
            on_sound()  # звук пошёл: отсюда считается задержка, не от конца проигрывания
        await asyncio.to_thread(play, pcm)
        return synth

    async def _say_stream(self, text: str, on_sound, voice: str | None = None) -> float:
        """Куски SSE пишутся в stdin ``aplay`` по мере прихода. Ошибка до первого куска — ``_NoAudioYet``."""
        from rob_box_llm.tts import TTSFormat, TTSSettings  # noqa: PLC0415

        t0 = time.time()
        first = 0.0
        proc: subprocess.Popen | None = None
        settings = TTSSettings(format=TTSFormat.PCM, sample_rate=TTS_RATE, voice=voice)
        try:
            async for chunk in self.tts.stream(text, settings=settings):
                if chunk.finish_reason == "error":
                    log("поток TTS оборвался посреди фразы — остаток не прозвучит")
                    break
                if not chunk.samples:
                    continue
                if proc is None:
                    first = time.time() - t0
                    proc = subprocess.Popen(aplay_cmd(chunk.sample_rate or TTS_RATE), stdin=subprocess.PIPE)
                    if on_sound:
                        on_sound()
                await asyncio.to_thread(proc.stdin.write, chunk.samples)
        except Exception as exc:  # noqa: BLE001
            if proc is None:
                raise _NoAudioYet() from exc
            log(f"поток TTS: {type(exc).__name__} посреди фразы")
        finally:
            if proc is not None:
                proc.stdin.close()
                await asyncio.to_thread(proc.wait)
        return first

    def stt_limited(self) -> bool:
        """Последний отказ STT — лимит провайдера (429): без паузы шум съест квоту."""
        return self.limited

    def hear(self, pcm: bytes) -> str:
        from rob_box_voice.stt_providers.minimax_provider import (  # noqa: PLC0415
            MiniMaxSTTError, MiniMaxSTTRateLimitError)

        self.limited = False
        try:
            return (self.stt.transcribe(wav_bytes(pcm)).text or "").strip()
        except MiniMaxSTTRateLimitError:
            self.limited = True
        except MiniMaxSTTError as exc:
            log(f"STT ошибка: {type(exc).__name__}")
        return ""


def load_env() -> None:
    import chat  # noqa: PLC0415  # scripts/dialogue/chat.py

    for path, applied in chat.load_env_files([CORE / "scripts" / "dialogue" / ".env"]):
        log(f"env: {path} ({applied} перем.)")


async def selftest(voice: Voice, phrase: str) -> None:
    """Колонка говорит фразу в динамик и одновременно пишет её своим микрофоном; затем распознаёт запись."""
    mic = Mic()
    frames: list = []
    stop = threading.Event()

    def grab() -> None:
        while not stop.is_set():
            frames.append(mic.frame())

    th = threading.Thread(target=grab, daemon=True)
    th.start()
    await asyncio.sleep(0.5)
    synth = await voice.say(phrase)
    await asyncio.sleep(0.5)
    stop.set()
    th.join(2)
    mic.proc.kill()
    pcm = b"".join(frames)
    log(f"синтез {synth:.2f} с; записано {len(pcm) / 2 / RATE:.1f} с, RMS {audioop.rms(pcm, 2)}")
    t0 = time.time()
    text = voice.hear(pcm)
    log(f"STT за {time.time() - t0:.2f} с: «{text}»")


def install_llm_hedge(hedge_s: float = HEDGE_S) -> None:
    """Хедж ``DeepSeekProvider.complete``: молчит дольше ``hedge_s`` — второй такой же запрос, берётся первый ответ.

    Отказ одного из двух ждёт второго; отказ обоих — исключение первого (как без хеджа). Проигравший отменяется.
    TODO: место хеджу — ядро (``rob_box_llm``), там он поможет и роботу; здесь — пока ядро не вынесено (ADR-0167).
    """
    if hedge_s <= 0:
        return
    from rob_box_llm.providers.deepseek import DeepSeekProvider  # noqa: PLC0415

    orig = DeepSeekProvider.complete
    if getattr(orig, "_rbx_hedged", False):
        return

    async def hedged(self, *a, **kw):
        t0 = time.time()
        first = asyncio.ensure_future(orig(self, *a, **kw))
        done, _ = await asyncio.wait({first}, timeout=hedge_s)
        if done:
            return first.result()
        second = asyncio.ensure_future(orig(self, *a, **kw))
        pending = {first, second}
        error: BaseException | None = None
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None:
                        who = "первый" if task is first else "второй"
                        log(f"⏱ LLM хедж: ответил {who} запрос, {time.time() - t0:.2f} с")
                        return task.result()
                    error = error or task.exception()
            raise error
        finally:
            for task in pending:
                task.cancel()

    hedged._rbx_hedged = True
    DeepSeekProvider.complete = hedged


def make_stand(args: argparse.Namespace):
    import chat  # noqa: PLC0415

    install_llm_hedge()
    t0 = time.time()
    cargs = chat.build_arg_parser().parse_args(["--wake-word"] + (["--db", args.db] if args.db else []))
    stand = chat.build_stand(cargs, Path(args.workdir))
    stand.wake = list(WAKE_WORDS)  # таблица вейков робота (wake_words.yaml) колонке не подходит
    log(f"ядро готово за {time.time() - t0:.1f} с (вейк: {', '.join(WAKE_WORDS)})")
    return chat, stand


async def one_turn(voice: Voice, args: argparse.Namespace) -> None:
    """Один ход по тексту без микрофона, выходы живые (earcon, реплики по ходу) — проверка без голоса человека."""
    chat, stand = make_stand(args)
    live = LiveOut(stand, voice, None)
    try:
        t1 = time.time()
        live.begin(t1)
        result = await stand.hear(args.turn, chat.CONSOLE_VOICE, source=chat.SOURCE_VOICE)
        log(f"ход {time.time() - t1:.2f} с, адресовано={result.addressed} ({result.reason}): {result.said}")
        await live.drain()
    finally:
        live.close()
        stand.close()


async def serve(voice: Voice, args: argparse.Namespace) -> None:
    chat, stand = make_stand(args)
    log("слушаю")
    mic, vad = Mic(), Vad()
    live = LiveOut(stand, voice, mic)
    try:
        while True:
            pcm = await asyncio.to_thread(vad.utterance, mic)
            t1 = time.time()
            text = await asyncio.to_thread(voice.hear, pcm)
            t2 = time.time()
            log(f"🎤 {len(pcm) / 2 / RATE:.1f} с → STT {t2 - t1:.2f} с: «{text}»")
            if not text:
                if voice.stt_limited():
                    log("STT 429 — пауза 20 с")
                    await asyncio.sleep(20)
                    mic.flush()
                continue
            live.begin(t1)
            result = await stand.hear(text, chat.CONSOLE_VOICE, source=chat.SOURCE_VOICE)
            t3 = time.time()
            if not result.addressed:
                log(f"… не адресовано ({result.reason})")
                continue
            log(f"ход {t3 - t2:.2f} с: {result.said}")
            await live.drain()
            mic.flush()
    finally:
        live.close()
        mic.proc.kill()
        stand.close()


class LiveOut:
    """Выходы хода — в динамик сразу, а не после хода, как у стенда консоли (его ``speech`` и ``stage`` только пишут).

    * ``stage("addressed", "started")`` → earcon «услышал» (у робота ``EARCON_SOUNDS["heard"]`` = ``button_click``).
    * ``speech.say`` → очередь проигрывателя: реплика-заглушка («Секунду, думаю») звучит, пока модель думает.

    Обёртки — через ``functools.wraps``: ход сверяет сигнатуру портов (``turn._accepts`` по ``inspect.signature``).
    """

    def __init__(self, stand, voice: Voice, mic: Mic | None) -> None:
        import functools  # noqa: PLC0415

        self._voice, self._mic = voice, mic
        self._queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        self._stand = stand
        self._t1 = 0.0
        self._player = asyncio.ensure_future(self._play())
        stage, say = stand.ports.stage, stand.speech.say

        @functools.wraps(stage)
        def live_stage(turn_id, epoch, stage_name, status, *a, **kw):
            if stage_name == "addressed" and status == "started":
                self.earcon()
            return stage(turn_id, epoch, stage_name, status, *a, **kw)

        @functools.wraps(say)
        def live_say(turn_id, epoch, text, voice_id):
            ref = say(turn_id, epoch, text, voice_id)
            if text and text.strip():
                self._queue.put_nowait((text, self.voice()))
            return ref

        stand.ports.stage, stand.speech.say = live_stage, live_say

    def voice(self) -> str | None:
        """Голос сессии (``set_voice`` хода) для MiniMax; ``None`` — голос провайдера по умолчанию.

        Колонка говорит только MiniMax: голос Яндекса или Silero здесь не прозвучит — пишем в лог, говорим MiniMax."""
        provider = getattr(self._stand, "voice_provider", "minimax")
        applied = self._stand.session.snapshot.voice.applied
        if provider != "minimax":
            if applied:
                log(f"голос {applied!r} провайдера {provider} на колонке не играет — говорю MiniMax по умолчанию")
            return None
        return applied or None

    def begin(self, t1: float) -> None:
        self._t1 = t1

    def earcon(self) -> None:
        try:
            with open(HEARD_PCM, "rb") as pcm:
                subprocess.Popen(aplay_cmd(TTS_RATE), stdin=pcm)
            log(f"🔔 услышал, от конца фразы {time.time() - self._t1:.2f} с")
        except OSError as exc:
            log(f"earcon не сыграл: {exc}")

    async def _play(self) -> None:
        while True:
            line, voice_id = await self._queue.get()
            try:
                started: list = []
                synth = await self._voice.say(line, self._mic, on_sound=lambda: started.append(time.time()),
                                              voice=voice_id)
                gap = f"{started[0] - self._t1:.2f}" if started else "—"
                log(f"🔊 синтез до звука {synth:.2f} с, от конца фразы до звука {gap} с, "
                    f"голос {voice_id or '—'}: «{line}»")
            except Exception as exc:  # noqa: BLE001
                log(f"реплика не прозвучала ({type(exc).__name__}): «{line}»")
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        await self._queue.join()

    def close(self) -> None:
        self._player.cancel()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--say", default="", help="только синтез текста в динамик")
    ap.add_argument("--selftest", nargs="?", const="Робот, который час?", default=None, help="фраза самопроверки")
    ap.add_argument("--turn", default="", help="один ход по тексту (без микрофона), звук живой")
    ap.add_argument("--db", default="", help="постоянная e2e-память колонки")
    ap.add_argument("--workdir", default="/tmp/rbx_station", help="каталог стенда")
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    os.chdir(CORE)
    load_env()
    subprocess.run(["amixer", "-q", "sset", "Master", VOLUME], check=False)
    voice = Voice()
    if args.say:
        log(f"синтез {asyncio.run(voice.say(args.say)):.2f} с")
    elif args.turn:
        asyncio.run(one_turn(voice, args))
    elif args.selftest is not None:
        asyncio.run(selftest(voice, args.selftest))
    else:
        asyncio.run(serve(voice, args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
