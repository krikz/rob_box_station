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
import json
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
#: Настройки со страницы (``station_setup``): ``RBX_*`` по умолчанию, окружение процесса важнее.
SETTINGS = BASE / "settings.json"
try:
    for _k, _v in json.loads(SETTINGS.read_text(encoding="utf-8")).items():
        os.environ.setdefault(str(_k), str(_v))
except (OSError, ValueError):
    pass
for sub in ("site", "core", "core/scripts/dialog", "core/scripts/dialogue"):
    sys.path.insert(0, str(BASE / sub))
for pkg in ("rob_box_core", "rob_box_dialog", "rob_box_harness", "rob_box_llm", "rob_box_voice", "rob_box_mcp_tools"):
    sys.path.insert(0, str(CORE / "src" / pkg))

import station_kws  # noqa: E402  (живые примеры вейка и промахи облака)
import station_offline  # noqa: E402  (фразы заранее — звучат без сети)
import station_stt  # noqa: E402  (STT: MiniMax → Яндекс, хедж)

RATE = 16000
FRAME_S = 0.03
FRAME_IN = int(RATE * FRAME_S) * 2  # байт кадра 30 мс, 16 кГц моно
MIC_DEVICE = os.environ.get("RBX_MIC", "plughw:0,1")
VOLUME = os.environ.get("RBX_VOLUME", "230")
#: Вейк колонки — свой, чтобы не отзываться вместе с роботом (Шифу 07.11: «пусть будет Игорёк»).
#: Порядок важен: длинные формы первыми (матчинг по словам, как в wake_words.yaml робота).
#: «Егорёк» — частое искажение STT (догадка, не замер).
#: Вейк по умолчанию: формы имени и искажения STT из логов колонки 07.11 («Легарёк», «Игорёв»). «Игорь» и «Егорка» —
#: нет: обычные имена, будут ложные срабатывания. Шифу 07.11: «игоряна побольше вариаций».
WAKE_DEFAULT = ["игорёчек", "игоречек", "игорёша", "игореша", "игорюша", "игорёха", "игореха", "игорян", "игорёк",
                "игорек", "игарёк", "игарек", "егорёк", "егорек", "легарёк", "легарек", "игорёв", "игорев"]
WAKE_WORDS = [w.strip() for w in os.environ.get(
    "RBX_WAKE", ",".join(WAKE_DEFAULT)).split(",") if w.strip()]
START_FRAMES, END_SILENCE_S, MIN_S, MAX_S = 5, 0.8, 0.7, 12.0
TTS_FORMAT, TTS_RATE = "pcm_24000", 24000
#: Потоковый синтез (SSE MiniMax): звук играет с первого куска, а не после всей фразы. 0 — целым куском (сравнение).
TTS_STREAM = os.environ.get("RBX_TTS_STREAM", "1") != "0"
#: Запас звука перед стартом ``aplay`` в потоке: куски SSE приходят неровно, без запаса речь рвётся.
PREBUF_S = float(os.environ.get("RBX_PREBUF_S", "0.3"))
#: Earcon «услышал»: ``sound_pack/button_click.mp3`` робота → s16le 24 кГц моно (ffmpeg на ПК, mp3 колонка не играет).
HEARD_PCM = BASE / "assets" / "heard.raw"
#: Хедж запроса LLM: DeepSeek на тот же промпт отвечает то за 2 с, то за 12 с (замер 07.11, ~4 из 15 запросов
#: 6–12 с). Не ответил за столько секунд — тот же запрос уходит второй раз, берётся первый ответ. 0 — выключено.
HEDGE_S = float(os.environ.get("RBX_HEDGE_S", "4"))
#: Кнопки колонки (замер 07.11, нажатия Шифу): event0 — «микрофон выкл», event1 (сенсорные) — громкость и кнопка Алисы.
KEY_MUTE, KEY_VOL_DOWN, KEY_VOL_UP, KEY_ACTION = 113, 114, 115, 582
#: Сенсорные кнопки срабатывают парой (Алиса + громкость минус в одну метку времени): второе нажатие в окне — шум.
KEY_GUARD_S = 0.15
VOL_MIN, VOL_MAX, VOL_STEP = 60, 255, 25
#: Кнопка Алисы без речи: следующая фраза в это окно адресована колонке без вейка.
ARM_S = 8.0
LED_BRIGHT = int(os.environ.get("RBX_LED", "255"))  # из 255; 255 — яркость анимаций Алисы


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
        self.user_muted = False  # кнопка «микрофон выкл»: кадры не идут в VAD, пока не включат
        #: Звонок по телефону: кадры уходят собеседнику, а не в VAD (вейк в разговоре не слушаем).
        self.tap = None
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
            tap = self.tap
            if tap is not None:
                tap(fr)
                continue
            if self.muted or self.user_muted:
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
    """STT — цепочка провайдеров (``station_stt``: MiniMax → Яндекс), TTS — MiniMax (ключи ``scripts/dialogue/.env``)."""

    def __init__(self) -> None:
        from rob_box_llm.providers.minimax_tts import MiniMaxTTSProvider  # noqa: PLC0415

        self.tts = MiniMaxTTSProvider()
        self.stt = station_stt.SttChain.from_env()
        if not self.stt.providers:
            raise SystemExit("нет ключа STT (MINIMAX_API_KEY или YANDEX_API_KEY)")
        log(f"STT: {' → '.join(self.stt.names())}, хедж {self.stt.hedge_s:.1f} с")
        self.limited = False
        self.proc: subprocess.Popen | None = None  # текущий aplay речи — его глушит кнопка
        self.cut = False

    async def synth_pcm(self, text: str, voice: str | None = None) -> bytes:
        """Синтез целиком в PCM 24 кГц (офлайн-фразы заранее)."""
        return await self.tts.synthesize_bytes(text, voice or None, format=TTS_FORMAT)

    async def play_pcm(self, pcm: bytes, mic: Mic | None = None, on_sound=None) -> None:
        """Готовый звук (офлайн-фраза, тон) в динамик, микрофон заглушён на время — свой звук не уходит в STT."""
        self.cut = False
        if mic:
            mic.muted = True
        try:
            if on_sound:
                on_sound()
            self.proc = subprocess.Popen(aplay_cmd(), stdin=subprocess.PIPE)
            await asyncio.to_thread(self.proc.communicate, pcm)
        finally:
            if mic:
                await asyncio.sleep(0.3)
                mic.flush()
                mic.muted = False

    def stop(self) -> None:
        """Кнопка: оборвать текущую реплику (aplay убить, поток синтеза бросить)."""
        self.cut = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    async def say(self, text: str, mic: Mic | None = None, on_sound=None, voice: str | None = None) -> float:
        """Синтез и проигрывание; возвращает время до первого звука от начала синтеза."""
        self.cut = False
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
        if self.cut:
            return synth
        if on_sound:
            on_sound()  # звук пошёл: отсюда считается задержка, не от конца проигрывания
        self.proc = subprocess.Popen(aplay_cmd(), stdin=subprocess.PIPE)
        await asyncio.to_thread(self.proc.communicate, pcm)
        return synth

    async def _say_stream(self, text: str, on_sound, voice: str | None = None) -> float:
        """Куски SSE пишутся в stdin ``aplay`` по мере прихода, после запаса ``PREBUF_S`` звука (07.11 без запаса
        был провал ``underrun 450 мс`` посреди фразы). Ошибка до начала звука — ``_NoAudioYet``."""
        from rob_box_llm.tts import TTSFormat, TTSSettings  # noqa: PLC0415

        t0 = time.time()
        first = 0.0
        proc: subprocess.Popen | None = None
        pending = b""
        settings = TTSSettings(format=TTSFormat.PCM, sample_rate=TTS_RATE, voice=voice)

        def start(rate: int) -> subprocess.Popen:
            nonlocal first
            first = time.time() - t0
            started = self.proc = subprocess.Popen(aplay_cmd(rate), stdin=subprocess.PIPE)
            if on_sound:
                on_sound()
            return started

        rate = TTS_RATE
        try:
            async for chunk in self.tts.stream(text, settings=settings):
                if chunk.finish_reason == "error":
                    log("поток TTS оборвался посреди фразы — остаток не прозвучит")
                    break
                if self.cut:
                    break
                if not chunk.samples:
                    continue
                rate = chunk.sample_rate or TTS_RATE
                if proc is None:
                    pending += chunk.samples
                    if len(pending) < int(PREBUF_S * rate) * 2:
                        continue
                    proc, data, pending = start(rate), pending, b""
                else:
                    data = chunk.samples
                await asyncio.to_thread(proc.stdin.write, data)
            if proc is None and pending and not self.cut:  # фраза короче запаса
                proc = start(rate)
                await asyncio.to_thread(proc.stdin.write, pending)
        except Exception as exc:  # noqa: BLE001
            if proc is None:
                raise _NoAudioYet() from exc
            if not self.cut:
                log(f"поток TTS: {type(exc).__name__} посреди фразы")
        finally:
            if proc is not None:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass  # aplay убит кнопкой
                await asyncio.to_thread(proc.wait)
        return first

    def stt_limited(self) -> bool:
        """Последний отказ STT — лимит всех провайдеров (429): без паузы шум съест квоту."""
        return self.limited

    def hear(self, pcm: bytes) -> "station_stt.Heard":
        """Распознать фразу цепочкой; отказ — ``Heard.error`` (а не пустой текст: тишина и сбой — разные вещи)."""
        heard = self.stt.hear(pcm)
        self.limited = heard.error == "limited"
        for note in heard.notes:
            log(f"STT {note}")
        return heard


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
    heard = voice.hear(pcm)
    log(f"STT {heard.provider or heard.error} за {time.time() - t0:.2f} с: «{heard.text}»")


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


#: Живое состояние колонки для инструментов статуса (кнопки и хост его обновляют).
STATION_STATE = {"volume": int(VOLUME), "mic_muted": False, "started": time.time()}


#: Живые примеры для модели ключевых слов (``training/kws``): ``pos`` / ``neg`` / ``miss`` — ``station_kws``.
#: Каталог на ``/data`` (``/tmp`` — в ОЗУ).
KWS_DIR = Path(os.environ.get("RBX_KWS_DIR", str(BASE / "kws_real")))
#: Офлайн-фразы (``station_offline``): не в ``assets/`` — их не трогает обновление.
OFFLINE_DIR = BASE / "offline"
#: Регистрация Improv в BlueZ дольше этого — без Improv: колонка не должна ждать Bluetooth, чтобы слушать.
IMPROV_START_S = 10.0
#: Ход дольше этого — сбой (модель или сеть повисли): колонка говорит, что не смогла, а не молчит.
TURN_TIMEOUT_S = float(os.environ.get("RBX_TURN_TIMEOUT_S", "45"))


#: Своя модель ключевых слов (``training/kws``, программа на C ``kws_run``). Пока режим тени: считает каждую фразу
#: параллельно с облачным STT и пишет ответ в лог — решение ещё за облаком (сравнить на живой речи, потом доверять).
KWS_BIN = BASE / "kws" / "kws_run"
KWS_MODEL = BASE / "kws" / "kws_model.bin"
#: Своя модель решает вейк (а не только пишет в лог): ``RBX_KWS_DECIDE=1``. Читается при каждом ходе —
#: можно включить в ``.env`` (страница его не перетирает) или в ``settings.json`` и перезапустить хост.
#: Порог — ``RBX_KWS_THRESHOLD`` (дефолт 0,8; на живых замерах 0,6 даёт recall 90,2 % при ложных 0,8 %).
KWS_WAKE_LABEL = "igorek"


def kws_score(pcm: bytes) -> tuple:
    """(метка, вероятность) лучшего окна первых 2 с фразы; нет модели или сбой — ``("", 0.0)``."""
    if not (KWS_BIN.exists() and KWS_MODEL.exists()):
        return "", 0.0
    try:
        out = subprocess.run([str(KWS_BIN), str(KWS_MODEL), "2.0", "0.1"], input=pcm[: RATE * 2 * 2],
                             capture_output=True, timeout=5).stdout.decode().split()
        return out[0], float(out[1])
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
        return "", 0.0


def kws_wake(label: str, prob: float) -> bool:
    """Своя модель услышала вейк с достаточной уверенностью и ей разрешено решать (``RBX_KWS_DECIDE=1``).

    По умолчанию режим тени: модель считает и пишет ``🔑 kws`` в лог, решение остаётся за облачным STT.
    """
    if os.environ.get("RBX_KWS_DECIDE", "0").strip().lower() in ("0", "", "off", "false", "no"):
        return False
    try:
        threshold = float(os.environ.get("RBX_KWS_THRESHOLD", "0.8"))
    except ValueError:
        threshold = 0.8
    return label == KWS_WAKE_LABEL and prob >= threshold


def kws_wake_text(text: str, label: str, prob: float) -> str:
    """Модель услышала вейк, а STT его не разобрал — код подставляет вейк сам (ADR-0148: решает код).

    Ядро считает фразу адресованной по тексту (``stand.wake``), поэтому без подстановки распознанный
    «Горек, который час» остался бы ``no_wake``, хотя модель уверена, что это вейк.
    """
    words = [w.strip(",.!?").lower() for w in text.split()]
    if not kws_wake(label, prob) or (words and words[0] in WAKE_WORDS) or set(words) & set(WAKE_WORDS):
        return text
    log(f"🔑 вейк по модели ({label} {prob:.2f}) — подставляю «{WAKE_WORDS[0]}»")
    return f"{WAKE_WORDS[0]}, {text}"


#: Своя модель уверена, что звучало имя, — кольцо «думаю» сразу после фразы, до ответа облака (0,4 с против 2–3 с):
#: человек видит, что его услышали. Решения это не меняет — только показ.
KWS_EARLY_P = float(os.environ.get("RBX_KWS_EARLY_P", "0.6"))
#: Облако текста не дало, а модель слышит имя с такой уверенностью — «Слушаю.» и окно без вейка, даже в режиме тени.
KWS_HEARD_P = float(os.environ.get("RBX_KWS_HEARD_P", "0.9"))


def _lev(a: str, b: str) -> int:
    """Расстояние правки (слова короткие — квадрат не страшен)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def wake_fix(text: str, label: str = "", prob: float = 0.0) -> str:
    """Имя, искажённое облаком, — всё равно имя: «Горек, который час» → «Игорёк, который час» (решает код, ADR-0148).

    Первое слово (или два первых слитно: «и горек», «игорь ок») ближе к форме вейка, чем на 1 правку, — вейк;
    своя модель слышит «Игорька» (≥ 0,5) — допуск 2. Иначе — как раньше: вейк по модели в режиме «решает модель».
    09.11 Шифу: «приходится по многу раз звать Игорька» — каждое искажение облака было промахом.
    """
    words = text.split()
    if not words or station_kws.wake_position(text, WAKE_WORDS) in ("start", "inside"):
        return text
    clean = [w.strip(",.!?;:«»\"'—–-").lower().replace("ё", "е") for w in words[:2]]
    kws_yes = label == KWS_WAKE_LABEL and prob >= station_kws.MISS_KWS_P
    forms = {w.replace("ё", "е") for w in WAKE_WORDS}
    for n, cand in ((1, clean[0]), (2, "".join(clean))):
        if n > len(clean) or len(cand) < 5:
            continue
        dist = min(_lev(cand, w) for w in forms)
        if dist <= (2 if kws_yes else 1) and not (n == 2 and dist > 0 and not kws_yes):
            log(f"🔑 вейк по созвучию: «{' '.join(words[:n])}» (правок {dist}"
                + (f", kws {label} {prob:.2f}" if label else "") + f") → «{WAKE_WORDS[0]}»")
            return f"{WAKE_WORDS[0]}, " + " ".join(words[n:])
    return kws_wake_text(text, label, prob)


def net_ok(host: str = "api.minimax.io", timeout: float = 1.5) -> bool:
    """Есть ли сеть: адрес Wi-Fi и DNS отвечает. Зовётся только при сбое — выбрать фразу «нет интернета»."""
    if not wifi_now()["ip"]:
        return False
    import concurrent.futures as cf  # noqa: PLC0415
    import socket  # noqa: PLC0415

    pool = cf.ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(socket.getaddrinfo, host, 443).result(timeout=timeout)
        return True
    except (cf.TimeoutError, OSError):
        return False
    finally:
        pool.shutdown(wait=False)


def save_setting(name: str, value: str) -> None:
    """Записать одну настройку в ``settings.json`` (как страница) и в окружение процесса."""
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[name] = value
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.environ[name] = value
    log(f"⚙ {name} = {value!r} сохранено")


def wifi_now() -> dict:
    """Сеть и адрес колонки сейчас (``wpa_cli status``); нет утилиты или сети — пустые поля."""
    try:
        out = subprocess.run(["wpa_cli", "-i", "wlan0", "status"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"ssid": "", "ip": ""}
    status = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"ssid": status.get("ssid", ""), "ip": status.get("ip_address", "")}


def device_facts() -> str:
    """Факты устройства в промпт (ADR-0148: факт строит код, модель произносит). 07.11 модель на «какой у тебя адрес»
    отвечала «не знаю», не вызывая ``get_robot_status``. Адрес меняется со сменой Wi-Fi — тогда хост перезапускается."""
    net = wifi_now()
    port = os.environ.get("RBX_HTTP_PORT", "8080")
    page = f"http://{net['ip']}:{port}/" if net["ip"] else "нет (нет сети)"
    return (f"- Wi-Fi network: {net['ssid'] or 'none'}; IP address: {net['ip'] or 'none'}.\n"
            f"- Settings page (keys, prompt, Wi-Fi, voice): {page}. Saving needs the Alice button press first.\n"
            "- Say an IP address by its number groups, as people read it aloud (10.1.1.13 → «десять, один, один, "
            "тринадцать»).\n"
            "- Your code (not you) handles these phrases; if asked, tell the person the exact words: «Игорёк, проверь "
            "обновление» (tells what is new), «Игорёк, обновись» (installs it), «Игорёк, включи блютуз» (music from a "
            "phone).\n"
            "- Complaints and bug reports for your developers: you CAN file them yourself with the `report_issue` tool "
            "(a GitHub issue with your log). Use it when the person asks to complain, report a bug or pass something "
            "to the developers — never say you cannot. Never use it on your own initiative.\n"
            "- Phone calls go through the person's phone by Bluetooth, and your code places them: «Игорёк, набери "
            "Кира» / «позвони маме», «прими», «сбрось» (or the Alice button). Tell people these words when asked.\n"
            + JOKE_RULES)


#: Анекдоты (Шифу 09.11: «Игорёк рассказывает не смешные анекдоты»). Живут в коде, а не в промпте колонки: промпт
#: Шифу правит на странице, а это правило должно доехать обновлением. Характер зануды из дополнения тянул
#: «уточню формулировку» сразу после концовки — шутка умирала.
JOKE_RULES = (
    "- JOKES («расскажи анекдот», «пошути»): tell ONE real, well-known Russian anekdot that people actually laugh at "
    "(Штирлиц, Вовочка, Петька и Василий Иванович, Рабинович, программисты, работа, семья, врачи) — not a pun you "
    "invent on the spot and not a children's riddle. Keep it short: setup in 1–3 sentences, dialogue with dashes, "
    "«…» right before the punchline, and the punchline is the LAST words you say. Never explain the joke, never "
    "add a moral, a clarification or your character's remark after the punchline (one short line before the joke "
    "is fine). «Ещё» — a different joke on a different theme; never repeat one told in this conversation. A topic "
    "named — a joke on that topic. No jokes about nationality, disability or illness of real people; adult jokes "
    "only when asked. Never change the punchline of a known joke and never make up a new «Штирлиц…» ending: if you "
    "do not remember a joke word for word, tell one you do. The level to aim for (folk classics — tell these or "
    "others just as famous; 09.11 «tell other ones» made the model invent unfunny endings, so these are allowed):\n"
    "  «Штирлиц выстрелил в упор. Упор упал.»\n"
    "  «Штирлиц долго смотрел в одну точку. Потом в другую. «Двоеточие!» — догадался Штирлиц.»\n"
    "  «— Вовочка, назови два местоимения. — Кто, я? — Правильно.»\n"
    "  «— Петька, приборы! — Двести! — Что «двести»? — А что «приборы»?»\n"
    "  «Программист ставит на ночь у кровати два стакана: с водой — если захочет пить, и пустой — если не "
    "захочет.»\n"
    "  «— Доктор, меня все игнорируют. — Следующий!»\n"
    "  «— Доктор, у меня всё болит: куда ни нажму — везде больно. — У вас, батенька, палец сломан.»\n"
    "  «Кошка сидит у норки и лает. Мышь думает: собака, значит, кошки нет, — и вылезла. Кошка её хвать: "
    "«Вот что значит знать иностранные языки!»»\n"
    "  «— Алло, это прачечная? — Хреначечная! Это Министерство культуры!»\n"
    "  «— Василий Иванович, а ты армией командовать можешь? — Могу, Петька. — А фронтом? — Могу. — А всем "
    "миром? — Нет, Петька, языков не знаю.»")


class _LogNode:
    """Нода-заглушка для MCP-тулов робота: их ``log_info``/``log_warning`` идут в лог колонки."""

    def get_logger(self):
        return self

    def info(self, msg: str) -> None:
        log(msg)

    warning = error = info

    def debug(self, msg: str) -> None:
        return


def http_transport(method: str, url: str, headers, body, timeout: float) -> tuple:
    """Транспорт поиска ``(статус, тело)`` на urllib: боевой транспорт робота живёт в модуле KIE и тянет
    ``rob_box_music`` (на колонке его нет). HTTP-ошибка — статус; сеть — исключение (тул считает бэкенд упавшим)."""
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    import ssl  # noqa: PLC0415

    import certifi  # noqa: PLC0415  (у Питона колонки нет системных корневых сертификатов — тот же набор, что у httpx)

    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 — https, фикс. хост
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def web_search(args: dict) -> tuple:
    """Поиск в интернете — тот же тул, что у робота (``rob_box_mcp_tools.tools.web_search``): DuckDuckGo, запасной —
    поиск DeepSeek по ключу ``DEEPSEEK_API_KEY``. HTTP синхронный: ход ждёт ответа поиска."""
    try:
        from dry_stand import load_tool_module  # noqa: PLC0415  (без tools/__init__: тот тянет rclpy)

        SearchWebTool = load_tool_module("web_search").SearchWebTool  # noqa: N806
    except ImportError as exc:
        return False, {"status": "rejected", "reason": "unavailable", "detail": f"поиск не установлен: {exc}"}
    query, limit = str(args.get("query") or ""), int(args.get("max_results") or 3)
    res = SearchWebTool(_LogNode(), transport=http_transport).execute(query=query, max_results=limit)
    data = dict(res.data or {})
    if not res.success:
        data.setdefault("status", "rejected")
        data.setdefault("reason", res.error or "search_unavailable")
    elif res.message:
        data.setdefault("note", res.message)
    return bool(res.success), data


def install_station_tools(stand) -> None:
    """Инструменты состояния — настоящие колонки, а не выдумки стенда консоли (07.11: «заряд 87 %», «никого в кадре»,
    и колонка не знала свой адрес). Стенд ищет обработчик ``_t_<тул>`` у ``stand.tools`` — подменяем в экземпляре."""
    tools = stand.tools

    def status(args: dict) -> tuple:
        net = wifi_now()
        port = os.environ.get("RBX_HTTP_PORT", "8080")
        return True, {
            "device": "колонка (Яндекс Станция Лайт), не робот: не ездит, камеры и батареи нет",
            "name": os.environ.get("RBX_NAME", "Игорёк"),
            "network": {"wifi_ssid": net["ssid"] or None, "ip_address": net["ip"] or None,
                        "settings_page": f"http://{net['ip']}:{port}/" if net["ip"] else None},
            "volume": f"{STATION_STATE['volume']} из 255", "microphone": "выключен кнопкой"
            if STATION_STATE["mic_muted"] else "включён",
            "power": "от сети, батареи нет", "uptime_min": round((time.time() - STATION_STATE["started"]) / 60),
        }

    def battery(args: dict) -> tuple:
        return True, {"status": "no_battery", "battery_level": None, "power": "колонка работает от сети, батареи нет"}

    def perception(args: dict) -> tuple:
        return False, {"status": "rejected", "reason": "unavailable", "detail": "у колонки нет камеры"}

    def volume(args: dict) -> tuple:
        """Громкость голоса — настоящая (``amixer``), как кнопки; проценты — по шкале кнопок."""
        old = STATION_STATE["volume"]
        action = str(args.get("action") or "")
        if action == "set" and str(args.get("level", "")).lstrip("-").isdigit():
            new = VOL_MIN + int(args["level"]) * (VOL_MAX - VOL_MIN) // 100
        else:
            new = {"louder": old + VOL_STEP, "quieter": old - VOL_STEP, "max": VOL_MAX, "normal": int(VOLUME)}.get(
                action, old)
        new = max(VOL_MIN, min(VOL_MAX, new))
        subprocess.run(["amixer", "-q", "sset", "Master", str(new)], check=False)
        STATION_STATE["volume"] = new
        pct = lambda v: round(100 * (v - VOL_MIN) / (VOL_MAX - VOL_MIN))  # noqa: E731
        log(f"🔈 громкость голосом: {old} → {new}")
        return True, {"status": "done", "old_volume": pct(old), "new_volume": pct(new)}

    def unavailable(what: str):
        def handler(args: dict) -> tuple:
            return False, {"status": "rejected", "reason": "unavailable", "detail": f"на колонке нет: {what}"}
        return handler

    tools._t_get_robot_status, tools._t_get_battery_level = status, battery
    tools._t_get_perception_context = perception
    tools._t_set_volume = volume
    tools._t_search_web = web_search
    # Заглушки стенда консоли рапортуют «готово» за то, чего у колонки нет (музыка, лицо, звуки) — честный отказ.
    for name, what in (("request_music", "музыки"), ("generate_music", "музыки"), ("music_library", "музыки"),
                       ("dj_set", "музыки"), ("set_music_volume", "музыки"), ("play_animation", "лица и анимаций"),
                       ("play_sound", "библиотеки звуков"), ("get_sound_info", "библиотеки звуков")):
        setattr(tools, f"_t_{name}", unavailable(what))


REPORT_TOOL = "report_issue"
REPORT_TOOL_DESCRIPTION = (
    "File a complaint or bug report about this speaker as a GitHub issue for its developers; the speaker's version, "
    "settings and recent log are attached automatically. Call it ONLY when the person explicitly asks to complain, "
    "report a bug or pass something to the developers («пожалуйся», «заведи баг», «передай разработчикам»). "
    "After the result, say its `say` field (or tell the issue number) in your own words, briefly.")
REPORT_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "complaint": {"type": "string", "description": "The person's complaint in their own words, verbatim "
                      "(Russian), without the wake word and without «пожалуйся»."},
        "title": {"type": "string", "description": "Short issue title in Russian, up to 70 characters."},
        "details": {"type": "string", "description": "What happened, from this conversation: what the person "
                    "asked, what you answered or failed to do. Facts only, 1-4 sentences, Russian. No guesses "
                    "about the cause."},
        "with_log": {"type": "boolean", "description": "Attach recent log lines (default true; false if the "
                     "person said «без лога»)."},
    },
    "required": ["complaint"],
}


def register_report_tool() -> bool:
    """Тул модели ``report_issue`` (Шифу 09.11: «код не всегда подхватывает, а модель тупит и говорит, что не может
    жалобу создать — надо дать инструмент модели»).

    Каталог тулов ядра неизменяемый (``MappingProxyType``), поэтому хост дописывает запись при старте: каталог,
    класс ``query`` (данные — обратно модели, фразу строит она; ждут 15 с — второго вызова LLM нет) и «виден всегда»
    (``TOOLS_ALWAYS``). Таблицы читаются через модуль (``kn.X``, ``tc._BY_NAME``) — подмена атрибута доходит.
    Разбор фразы кодом («пожалуйся, что…») остаётся первым: он быстрее и не зависит от модели.
    TODO: место тулу — ядро колонки (ADR-0167), когда ядро станет пакетом.
    """
    from types import MappingProxyType  # noqa: PLC0415

    try:
        from rob_box_core import tool_catalog as tc  # noqa: PLC0415
        from rob_box_dialog import knowledge as kn  # noqa: PLC0415
    except ImportError as exc:
        log(f"📮 тул {REPORT_TOOL} не поставлен: {exc}")
        return False
    if REPORT_TOOL not in tc._BY_NAME:  # noqa: SLF001
        entry = tc.ToolCatalogEntry(name=REPORT_TOOL, description=REPORT_TOOL_DESCRIPTION,
                                    parameters=MappingProxyType(dict(REPORT_TOOL_PARAMETERS)), read_only=False,
                                    destructive=False, idempotent=False, skill=(tc.CORE_SKILL,))
        tc.TOOL_CATALOG = tc.TOOL_CATALOG + (entry,)
        tc._BY_NAME = MappingProxyType({**tc._BY_NAME, REPORT_TOOL: entry})  # noqa: SLF001
        kn.TOOL_CLASSES = MappingProxyType({**kn.TOOL_CLASSES, REPORT_TOOL: "query"})
        kn.TOOLS_ALWAYS = tuple(kn.TOOLS_ALWAYS) + (REPORT_TOOL,)
    return True


def install_report_tool(stand, reporter) -> bool:
    """Обработчик ``report_issue`` в исполнителе стенда (каталог — :func:`register_report_tool`, до сборки стенда)."""
    if not register_report_tool():
        return False

    def report(args: dict) -> tuple:
        complaint = str(args.get("complaint") or "").strip()
        log(f"📮 тул {REPORT_TOOL}: «{complaint}»")
        live = getattr(stand, "rbx_live", None)
        if live is not None:
            live.earcon()  # ход ждёт GitHub пару секунд — щелчок, что дело пошло
        if os.environ.get("RBX_REPORT_DRY") == "1":  # проверка без записи в GitHub
            return True, {"status": "done", "issue_number": 0, "say": "Проверка: issue не заведён (RBX_REPORT_DRY)."}
        data = reporter.file_by_model(complaint, str(args.get("title") or ""), str(args.get("details") or ""),
                                      args.get("with_log") is not False)
        return data["status"] == "done", data

    stand.tools._t_report_issue = report
    log(f"📮 тул модели {REPORT_TOOL} поставлен")
    return True


def make_stand(args: argparse.Namespace):
    import chat  # noqa: PLC0415

    import station_setup as setup  # noqa: PLC0415

    install_llm_hedge()
    prompts = station_prompts(setup)
    chat.dm.PROMPT_DIR = prompts.materialize(Path(args.workdir) / "prompt", device_facts())  # основа + дополнение
    log(f"промпт: основа {'своя' if prompts.is_own() else 'ядра'}"
        f"{', дополнение ' + str(len(prompts.extra().strip())) + ' знаков' if prompts.extra().strip() else ''}")
    t0 = time.time()
    register_report_tool()  # до сборки стенда: срез тулов не должен успеть закэшироваться без него
    cargs = chat.build_arg_parser().parse_args(["--wake-word"] + (["--db", args.db] if args.db else []))
    stand = chat.build_stand(cargs, Path(args.workdir))
    stand.wake = list(WAKE_WORDS)  # таблица вейков робота (wake_words.yaml) колонке не подходит
    install_station_tools(stand)
    import station_report  # noqa: PLC0415  (жалоба в GitHub: «пожалуйся» → issue с логом)

    stand.rbx_reporter = station_report.Reporter(BASE, lambda: os.environ.get("GH_ISSUE_TOKEN", ""), log,
                                                 format_with=station_report.deepseek_formatter(log))
    install_report_tool(stand, stand.rbx_reporter)
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


async def first_heard(chain_task: asyncio.Future, sprint_task: asyncio.Future):
    """Гонка цепочки STT и быстрого запасного: первый удачный ответ; отказ одного — ждём другого."""
    pending = {chain_task, sprint_task}
    last = None
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            last = task.result()
            if last.ok:
                for other in pending:  # проигравший досчитает в своём потоке, результат не нужен
                    other.add_done_callback(lambda t: t.exception())
                if task is sprint_task:  # заметки цепочки уже в логе (Voice.hear), у гонщика — свои
                    log(f"STT гонка: первым {last.provider} ({last.seconds:.2f} с)")
                return last
    return last


async def code_command(text: str, live: "LiveOut", updater, reporter, bt, mic: Mic, hfp=None) -> bool:
    """Команды, которые решает код (ADR-0148), — после вейка: обновление, жалоба, звонок, Bluetooth.
    Выполнил — ``True``."""
    import station_report  # noqa: PLC0415
    import station_update  # noqa: PLC0415

    words = text.split()
    if not words or words[0].lower().strip(",.!?") not in WAKE_WORDS:
        return False
    rest = " ".join(words[1:])
    command = station_update.update_command(rest)
    if command:  # обновление: «проверь» — только рассказать, «обновись» — поставить
        live.earcon()
        if live._ring:  # noqa: SLF001
            live._ring.set("think")  # noqa: SLF001  (GitHub отвечает секунды — видно, что колонка занята)
        tag, phrase = await asyncio.to_thread(updater.latest)
        live.speak(phrase + (" Скажи «Игорёк, обновись», чтобы поставить." if tag and command == "check" else ""))
        done = await asyncio.to_thread(updater.install, tag) if tag and command == "install" else ""
        if tag and command == "install":
            live.speak(done or "Не смог скачать обновление, попробуй позже.")
        await live.drain()
        if done.startswith("Обновляюсь"):
            updater.arm_watchdog()  # окно сторожа начинается здесь: озвучка уже позади (08.11: откат)
            restart_self(mic, "обновление", announce=False)
        return True
    action, complaint = station_report.report_command(rest)
    if action:  # жалоба по фразе: быстрее и надёжнее модели; иначе — тул модели report_issue
        live.earcon()
        if live._ring:  # noqa: SLF001
            live._ring.set("think")  # noqa: SLF001
        live.speak(await asyncio.to_thread(reporter.file, complaint, action == "file"))
        await live.drain()
        return True
    if hfp is not None:
        reply = await hfp.handle(rest)
        if reply is not None:  # звонок: «набери Кира», «прими», «сбрось» — AT-команды телефону
            live.earcon()
            if reply:
                live.speak(reply)
            await live.drain()
            return True
    reply = await bt.handle(rest)
    if reply:  # Bluetooth-музыка: ход модели не нужен
        live.earcon()
        live.speak(reply)
        await live.drain()
        return True
    return False


async def start_hfp(live: "LiveOut", mic: Mic, ring: "Ring"):
    """Звонки через телефон (``station_hfp``, #16): ``RBX_HFP=0`` — выключить (bluealsa остаётся как у Алисы)."""
    if os.environ.get("RBX_HFP", "1").strip().lower() in ("0", "off", "false", "no"):
        return None
    import station_hfp  # noqa: PLC0415

    def on_ring(who: str) -> None:
        live.tone("ok")
        live.speak(f"Звонит {who}. Скажи «Игорёк, прими» или нажми кнопку сверху.")

    def on_call(up: bool) -> None:
        if up:
            live.stop()  # своя речь в разговор не лезет
            mic.tap = hfp.audio.feed if hfp.audio else None
            ring.set("call")
        else:
            mic.tap = None
            mic.flush()
            ring.idle()
            live.tone("ok")

    hfp = station_hfp.Hfp(log, on_ring, on_call)
    await asyncio.to_thread(station_hfp.Hfp.ensure_profile, log)
    asyncio.ensure_future(hfp.keep_attached())
    return hfp


def announce_start(live: "LiveOut", version: str) -> None:
    """Голосом сказать, что колонка поднялась (#15: «когда стартуешь, ничего не говоришь, непонятно, запустился ли»).

    Новая версия — «Обновился до …» (синтезом: версия меняется, заранее её не запишешь), иначе офлайн-фраза.
    """
    mark = BASE / "announced_version"
    live.begin(time.time())  # отсчёт задержки — от старта, а не от нуля эпохи
    try:
        before = mark.read_text(encoding="utf-8").strip()
    except OSError:
        before = ""
    if before and version and before != version:
        live.speak(f"Обновился до версии {version}. Слушаю.")
    else:
        live.say_offline("started")
    try:
        mark.write_text(version, encoding="utf-8")
    except OSError:
        pass


async def serve(voice: Voice, args: argparse.Namespace) -> None:
    import station_bt  # noqa: PLC0415  (Bluetooth-музыка: сопряжение и пульт плеера телефона)
    import station_update  # noqa: PLC0415  (обновление колонки с GitHub по тегу)

    chat, stand = make_stand(args)
    log("слушаю")
    mic, vad = Mic(), Vad()
    ring = Ring(mic)
    ring.idle()
    live = LiveOut(stand, voice, mic, ring)
    buttons = Buttons(mic, ring, live)
    bt = station_bt.BtMedia(log)
    asyncio.ensure_future(bt.ensure_alias())  # имя для телефонов слетает с перезагрузкой
    updater = station_update.Updater(BASE, lambda: os.environ.get("GITHUB_TOKEN", ""), log)
    reporter = stand.rbx_reporter
    kws = station_kws.KwsCollector(KWS_DIR, log)
    asyncio.get_running_loop().call_later(30, station_update.OK_MARK.touch)  # «здоров» — сторож отката не нужен
    version = updater.current()
    log(f"версия колонки: {version}")
    hfp = await start_hfp(live, mic, ring)
    buttons.hfp = hfp
    Keys(asyncio.get_running_loop(), buttons.on_key)
    await start_setup(stand, mic, ring, buttons)
    announce_start(live, version)
    live.refresh_offline(delay=8.0)  # пока сеть есть — досинтезировать фразы на случай, когда её не будет
    try:
        while True:
            pcm = await asyncio.to_thread(vad.utterance, mic)
            t1 = time.time()
            live.begin(t1)
            stt_task = asyncio.ensure_future(asyncio.to_thread(voice.hear, pcm))
            label, prob = await asyncio.to_thread(kws_score, pcm)  # 0,4 с — раньше облака
            early = label == KWS_WAKE_LABEL and prob >= KWS_EARLY_P
            if early and ring.state in ("off", "listen"):
                ring.set("think")  # своя модель слышит имя: показать сразу, а не через 2–3 с облака
            sprint = voice.stt.sprinter() if (early or buttons.armed()) and not stt_task.done() else ""
            heard = await first_heard(stt_task, asyncio.ensure_future(
                asyncio.to_thread(voice.stt.hear_one, sprint, pcm))) if sprint else await stt_task
            text, t2 = heard.text, time.time()
            log(f"🎤 {len(pcm) / 2 / RATE:.1f} с → STT {heard.provider or heard.error} {t2 - t1:.2f} с: «{text}»"
                + (f"  🔑 kws {label} {prob:.2f}" if label else ""))
            armed = buttons.armed()
            if not heard.ok:  # сбой облака — не тишина: если звали колонку, сказать, что не так
                if early or armed or kws_wake(label, prob):
                    key = {"limited": "stt_limited"}.get(heard.error) or (
                        "stt_fail" if await asyncio.to_thread(net_ok) else "no_net")
                    await live.trouble(key)
                else:
                    ring.idle() if ring.state == "think" else None
                if heard.error == "limited":
                    await asyncio.sleep(3)  # все провайдеры в лимите — переждать, а не жечь квоту шумом
                mic.flush()
                continue
            kws.observe(pcm, text, WAKE_WORDS, label, prob, heard.provider, now=t1)
            if not text:
                if kws_wake(label, prob) or (label == KWS_WAKE_LABEL and prob >= KWS_HEARD_P) or armed:
                    # облако текста не дало, а имя (или кнопку) слышно — коротко ответить и ждать фразу без вейка
                    live.earcon()
                    live.say_offline("heard")
                    await live.drain()
                    buttons.arm(ring)
                elif ring.state == "think":
                    ring.idle()
                mic.flush()
                continue
            text = buttons.address(wake_fix(text, label, prob))
            if station_kws.wake_position(text, WAKE_WORDS) == "start":
                tries = kws.promote_misses(now=t1)
                if tries:
                    log(f"🔑 вейк с {tries + 1}-й попытки: до этого {tries} фраз(ы) без имени — в kws_real/miss")
            if await code_command(text, live, updater, reporter, bt, mic, hfp):
                mic.flush()
                continue
            log(f"🧠 окно до хода: записей {len(stand.engine.log.window())}, токенов {stand.engine.log.tokens()}")
            try:
                result = await asyncio.wait_for(stand.hear(text, chat.CONSOLE_VOICE, source=chat.SOURCE_VOICE),
                                                TURN_TIMEOUT_S)
            except asyncio.TimeoutError:
                log(f"ход дольше {TURN_TIMEOUT_S:.0f} с — прерван")
                await live.trouble("llm_fail" if await asyncio.to_thread(net_ok) else "no_net")
                mic.flush()
                continue
            except Exception as exc:  # noqa: BLE001 — сбой хода не роняет хост (раньше — рестарт и тишина)
                log(f"ход упал: {type(exc).__name__}: {str(exc)[:200]}")
                await live.trouble("llm_fail" if await asyncio.to_thread(net_ok) else "no_net")
                mic.flush()
                continue
            t3 = time.time()
            if not result.addressed:
                log(f"… не адресовано ({result.reason})")
                if ring.state == "think" and not live.busy():
                    ring.idle()
                continue
            for line in result.log.splitlines():
                if any(k in line for k in ("violation", "bad_", "repair", "rejected", "fields_dropped")):
                    log(f"   ядро: {line[:300]}")
            log(f"ход {t3 - t2:.2f} с: {result.said}")
            log(f"🧠 окно после хода: записей {len(stand.engine.log.window())}, токенов {stand.engine.log.tokens()}")
            await live.drain()
            if wordless_tail(ring, result, buttons):
                log(f"🎤 ход без слов — слушаю дальше {ARM_S:.0f} с")
            mic.flush()
    finally:
        live.close()
        ring.idle()  # что бы ни случилось в цикле, кольцо не должно остаться в «думаю»/«говорю»
        mic.proc.kill()
        stand.close()


class LiveOut:
    """Выходы хода — в динамик сразу, а не после хода, как у стенда консоли (его ``speech`` и ``stage`` только пишут).

    * ``stage("addressed", "started")`` → earcon «услышал» (у робота ``EARCON_SOUNDS["heard"]`` = ``button_click``).
    * ``speech.say`` → очередь проигрывателя: реплика-заглушка («Секунду, думаю») звучит, пока модель думает.

    Обёртки — через ``functools.wraps``: ход сверяет сигнатуру портов (``turn._accepts`` по ``inspect.signature``).
    """

    def __init__(self, stand, voice: Voice, mic: Mic | None, ring: "Ring | None" = None) -> None:
        import functools  # noqa: PLC0415

        self._voice, self._mic, self._ring = voice, mic, ring
        self._queue: asyncio.Queue[tuple[str, str | None, str]] = asyncio.Queue()  # (текст, голос, офлайн-ключ)
        self._stand = stand
        self._t1 = 0.0
        self.offline = station_offline.OfflinePhrases(OFFLINE_DIR, log)
        self._refreshing: asyncio.Future | None = None
        stand.rbx_live = self
        self._player = asyncio.ensure_future(self._play())
        stage, say = stand.ports.stage, stand.speech.say

        @functools.wraps(stage)
        def live_stage(turn_id, epoch, stage_name, status, *a, **kw):
            if stage_name == "addressed" and status == "started":
                self.earcon()
                if self._ring:
                    self._ring.set("think")
            return stage(turn_id, epoch, stage_name, status, *a, **kw)

        @functools.wraps(say)
        def live_say(turn_id, epoch, text, voice_id):
            ref = say(turn_id, epoch, text, voice_id)
            if text and text.strip():
                self._queue.put_nowait((text, self.voice(), ""))
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
        if applied and applied != os.environ.get("RBX_VOICE"):
            save_setting("RBX_VOICE", applied)  # голос, выбранный словами, переживает перезапуск
            self.refresh_offline(delay=5.0)  # офлайн-фразы — новым голосом
        return applied or os.environ.get("RBX_VOICE") or None

    def refresh_offline(self, delay: float = 0.0) -> None:
        """Досинтезировать офлайн-фразы текущим голосом в фоне (пока сеть есть); уже идёт — не дублировать."""
        if self._refreshing is not None and not self._refreshing.done():
            return

        async def run() -> None:
            await asyncio.sleep(delay)
            voice = self.voice() or ""
            await self.offline.refresh(lambda text, v: self._voice.synth_pcm(text, v or None), voice)

        self._refreshing = asyncio.ensure_future(run())

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
            line, voice_id, key = await self._queue.get()
            if self._ring and self._ring.state != "error":
                self._ring.set("speak")
            started: list = []
            mark = lambda: started.append(time.time())  # noqa: E731
            try:
                key = key or self.offline.key_for(line)
                cached = self.offline.pcm(key) if key else None
                if key == "tone":
                    await self._voice.play_pcm(station_offline.tone(line), self._mic, mark)
                elif cached:  # заранее синтезированная — сразу, без сети
                    await self._voice.play_pcm(cached, self._mic, mark)
                    log(f"🔊 офлайн «{key}», от конца фразы до звука {self._gap(started)} с: «{line}»")
                else:
                    synth = await self._voice.say(line, self._mic, on_sound=mark, voice=voice_id)
                    log(f"🔊 синтез до звука {synth:.2f} с, от конца фразы до звука {self._gap(started)} с, "
                        f"голос {voice_id or '—'}: «{line}»")
            except Exception as exc:  # noqa: BLE001
                log(f"реплика не прозвучала ({type(exc).__name__}): «{line}»")
                if not started:  # синтез упал до звука — молчать нельзя: офлайн-фраза или тон ошибки
                    await self._fallback_sound()
            finally:
                self._queue.task_done()
                if self._ring and self._queue.empty() and self._ring.state != "error":
                    self._ring.idle()

    def _gap(self, started: list) -> str:
        return f"{started[0] - self._t1:.2f}" if started else "—"

    async def _fallback_sound(self) -> None:
        if self._ring:
            self._ring.flash("error", 2.0)
        pcm = self.offline.pcm("tts_fail") or station_offline.tone("error")
        try:
            await self._voice.play_pcm(pcm, self._mic)
            log("🔊 синтез не ответил — сыграл запасной звук")
        except Exception as exc:  # noqa: BLE001
            log(f"запасной звук не сыграл: {type(exc).__name__}")

    def speak(self, text: str, key: str = "") -> None:
        """Фраза кода (не хода модели) — в ту же очередь динамика; ``key`` — офлайн-фраза, если она уже есть."""
        self._queue.put_nowait((text, self.voice(), key))

    def say_offline(self, key: str) -> None:
        """Офлайн-фраза по ключу: файл есть — звучит сразу, нет — синтезом (сеть есть) или тоном ошибки."""
        self.speak(self.offline.text(key) or key, key)

    def tone(self, kind: str) -> None:
        self._queue.put_nowait((kind, None, "tone"))

    async def trouble(self, key: str) -> None:
        """Сбой, о котором человек должен узнать: кольцо «ошибка» и фраза, что именно не так (офлайн, без сети)."""
        log(f"⚠ {key}: {self.offline.text(key)}")
        if self._ring:
            self._ring.flash("error", 2.5)
        if self.offline.pcm(key) is None and key in ("no_net", "stt_fail", "llm_fail", "stt_limited"):
            self.tone("error")  # фразы ещё нет, а сеть, скорее всего, и есть причина — тон точно прозвучит
        else:
            self.say_offline(key)
        await self.drain()

    def busy(self) -> bool:
        return not self._queue.empty() or (self._voice.proc is not None and self._voice.proc.poll() is None)

    def stop(self) -> None:
        """Кнопка Алисы во время речи: оборвать реплику и всё, что стоит в очереди."""
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._voice.stop()

    async def drain(self) -> None:
        await self._queue.join()
        # Ход без реплики (адресовано, но `said` пуст) ничего не кладёт в очередь — `_play` не проснётся,
        # и кольцо останется в «думаю» навсегда (08.11: «Игорёк, задумался» — think висел 3 минуты).
        if self._ring and self._queue.empty() and self._ring.state != "error":
            self._ring.idle()

    def close(self) -> None:
        self._player.cancel()


#: Родные анимации кольца Алисы (прошивка колонки): ``loop`` / ``gammacorrection`` в заголовке, дальше кадры
#: «цвет1 цвет2 цвет3 мс» (hex RGB трёх диодов). Нет каталога — эффекты кольца считает код (``Ring._frame``).
LED_PATTERNS = Path(os.environ.get("RBX_LED_PATTERNS", "/system/vendor/quasar/ledpatterns"))
#: Состояние кольца → шаблон Алисы.
RING_PATTERNS = {"listen": "alice_listening", "think": "alice_thinking", "speak": "alice_speaking",
                 "connect": "connecting_to_network", "error": "error"}


def load_led_pattern(path: Path):
    """Шаблон ``.led`` → (зациклен, гамма, [((r,g,b)×3, мс)]); битый или нет файла — ``None``."""
    try:
        lines = path.read_text(encoding="ascii").split("\n")
    except (OSError, UnicodeDecodeError):
        return None
    loop = gamma = False
    frames = []
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if parts[0] in ("loop", "gammacorrection"):
            loop, gamma = loop or parts[0] == "loop", gamma or parts[0] == "gammacorrection"
            continue
        if len(parts) != 4:
            continue
        try:
            leds = tuple(tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) for c in parts[:3])
            frames.append((leds, max(10, int(parts[3]))))
        except ValueError:
            continue
    return (loop, gamma, frames) if frames else None


class Ring:
    """Кольцо из трёх RGB-диодов (``/sys/class/leds/micro:{red,green,blue}:ring{1,2,3}``): состояние колонки эффектом.

    Стиль — ``RBX_RING_STYLE`` (страница настроек, читается на каждом кадре):

    * ``color`` (по умолчанию) — эффекты кода, у каждого диода свой цвет (:data:`PALETTES`). Шифу 09.11: «три
      светодиода, а горят всегда одним цветом — нельзя разноцветно?» — у каждого диода свои R, G, B, можно.
    * ``alice`` — родные анимации Алисы (``RING_PATTERNS``), где они есть; остальное — эффекты кода.

    Микрофон выкл — ровный красный, громкость — белые диоды по уровню на 1,2 с. Яркость ``LED_BRIGHT`` из 255.
    В sysfs пишется только изменившееся."""

    #: Состояние → цвета трёх диодов (0..1). Оттенки рядом по кругу, чтобы бегущий огонёк переливался.
    PALETTES = {
        "off": ((0, 0, 0),) * 3,
        "listen": ((0, 0.35, 1), (0, 0.9, 1), (0.35, 0, 1)),  # синий · бирюзовый · фиолетово-синий
        "think": ((0.8, 0, 1), (1, 0, 0.45), (0.25, 0.1, 1)),  # фиолетовый · малиновый · индиго
        "speak": ((0, 1, 0.15), (0.45, 1, 0), (0, 0.85, 0.6)),  # зелёный · салатовый · морская волна
        "connect": ((1, 1, 1), (0.4, 0.7, 1), (1, 1, 1)),
        "call": ((0, 1, 0.3), (0, 0.55, 1), (0, 1, 0.3)),  # разговор по телефону: зелёный с голубым
        "muted": ((1, 0, 0),) * 3,
        "error": ((1, 0, 0), (1, 0.35, 0), (1, 0, 0)),
    }
    #: Цвет состояния для экрана без анимации (один на всё кольцо) — первый цвет палитры.
    COLORS = {state: colors[0] for state, colors in PALETTES.items()}
    FPS = 12

    def __init__(self, mic: Mic | None) -> None:
        self._mic = mic
        self.state = "off"
        self._warned = False
        self._last: dict = {}
        self._override: tuple | None = None  # (rgb, сколько диодов, до какого времени) — громкость
        self._since = time.monotonic()
        self._patterns = {state: load_led_pattern(LED_PATTERNS / f"{name}.led")
                          for state, name in RING_PATTERNS.items()}
        found = [s for s, p in self._patterns.items() if p]
        log(f"кольцо: анимации Алисы — {', '.join(found) or 'нет (эффекты кода)'}")
        try:
            self._task = asyncio.get_running_loop().create_task(self._run())
        except RuntimeError:  # без цикла (тесты, --say) — без анимации
            self._task = None

    def _led(self, color: str, i: int, value: int) -> None:
        key = (color, i)
        if self._last.get(key) == value:
            return
        try:
            with open(f"/sys/class/leds/micro:{color}:ring{i}/brightness", "w") as led:
                led.write(str(value))
            self._last[key] = value
        except OSError as exc:
            if not self._warned:
                log(f"LED недоступны: {exc}")
                self._warned = True

    def _show(self, leds) -> None:
        """Три цвета ``(r, g, b)`` 0..255 — на диоды, с яркостью ``LED_BRIGHT``."""
        k = max(0, min(255, LED_BRIGHT)) / 255
        for i, rgb in enumerate(leds, start=1):
            for color, value in zip(("red", "green", "blue"), rgb):
                self._led(color, i, int(value * k))

    def _frame(self, t: float) -> list:
        """Запасной эффект кода: яркость (0..1) трёх диодов в момент ``t`` от начала состояния."""
        import math  # noqa: PLC0415

        if self.state == "listen":
            k = 0.25 + 0.75 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 1.6))
            return [k, k, k]
        if self.state in ("think", "connect", "call"):
            head = (t / (1.6 if self.state == "call" else 0.9) * 3) % 3  # огонёк обходит три диода за 0,9 с
            return [max(0.12, 1 - min(abs(i - head), 3 - abs(i - head))) for i in range(3)]
        if self.state == "speak":
            k = 0.45 + 0.55 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 0.7))
            return [k, k, k]
        return [1.0, 1.0, 1.0] if self.state in ("muted", "error") else [0.0, 0.0, 0.0]

    def _pattern_frame(self, pattern, t: float):
        """Кадр шаблона Алисы в момент ``t``; шаблон без ``loop`` кончился — ``None``."""
        loop, gamma, frames = pattern
        total = sum(ms for _, ms in frames) / 1000
        if not loop and t >= total:
            return None
        t = t % total
        for leds, ms in frames:
            t -= ms / 1000
            if t < 0:
                return tuple(tuple(int(255 * (v / 255) ** 2.2) for v in rgb) for rgb in leds) if gamma else leds
        return frames[-1][0]

    async def _run(self) -> None:
        while True:
            now = time.monotonic()
            wait = 1 / self.FPS
            if self._override and now < self._override[2]:
                rgb, lit, _ = self._override
                self._show([tuple(255 * c for c in rgb) if i < lit else (0, 0, 0) for i in range(3)])
            else:
                self._override = None
                alice = os.environ.get("RBX_RING_STYLE", "color").strip().lower() == "alice"
                pattern = self._patterns.get(self.state) if alice else None
                frame = self._pattern_frame(pattern, now - self._since) if pattern else None
                if frame is not None:
                    self._show(frame)
                    wait = 0.04  # кадры Алисы — по 40 мс
                elif pattern is not None:  # одноразовый шаблон (ошибка) кончился
                    self.idle()
                else:
                    colors = self.PALETTES.get(self.state, self.PALETTES["off"])
                    if alice:  # стиль Алисы: эффекты кода одним цветом, как раньше
                        colors = (colors[0],) * 3
                    self._show([tuple(int(255 * c * k) for c in rgb)
                                for rgb, k in zip(colors, self._frame(now - self._since))])
                    if self.state in ("off", "muted"):
                        wait = 0.2  # без анимации — реже просыпаться
            await asyncio.sleep(wait)

    def set(self, state: str) -> None:
        if state != self.state:
            self._since = time.monotonic()
        self.state = state
        if self._task is None:
            rgb = self.COLORS.get(state, (0, 0, 0))
            self._show([tuple(255 * c for c in rgb)] * 3)

    def idle(self) -> None:
        self.set("muted" if self._mic is not None and self._mic.user_muted else "off")

    def flash(self, state: str, seconds: float = 3.0) -> None:
        """Показать состояние на время (итог смены Wi-Fi) и вернуть покой."""
        self.set(state)
        asyncio.get_running_loop().call_later(seconds, self.idle)

    def level(self, volume: int) -> None:
        """Громкость белым: 1–3 диода по уровню на 1,2 с, потом — прежний эффект."""
        lit = 1 + round(2 * (volume - VOL_MIN) / (VOL_MAX - VOL_MIN))
        self._override = ((1, 1, 1), lit, time.monotonic() + 1.2)


class Keys:
    """Кнопки колонки: поток читает ``/dev/input/event0,1`` и отдаёт код нажатия в цикл asyncio."""

    def __init__(self, loop: asyncio.AbstractEventLoop, handler) -> None:
        self._loop, self._handler = loop, handler
        self._last = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        import select  # noqa: PLC0415
        import struct  # noqa: PLC0415

        fds = [os.open(f"/dev/input/event{i}", os.O_RDONLY) for i in (0, 1)]
        while True:
            ready, _, _ = select.select(fds, [], [])
            for fd in ready:
                data = os.read(fd, 16 * 16)
                for off in range(0, len(data) - 15, 16):
                    _sec, _usec, typ, code, value = struct.unpack_from("IIHHi", data, off)  # input_event armhf
                    if typ == 1 and value == 1:
                        self._press(code)

    def _press(self, code: int) -> None:
        now = time.monotonic()
        if now - self._last < KEY_GUARD_S:
            return  # пара сенсорных кнопок в одну метку — второе нажатие не считаем
        self._last = now
        self._loop.call_soon_threadsafe(self._handler, code)


def click() -> None:
    """Короткий щелчок (тот же звук, что «услышал») — отклик кнопки."""
    with open(HEARD_PCM, "rb") as pcm:
        subprocess.Popen(aplay_cmd(TTS_RATE), stdin=pcm)


class Buttons:
    """Кнопки: громкость ±, «микрофон выкл», кнопка Алисы (во время речи — стоп, иначе — слушать без вейка)."""

    def __init__(self, mic: Mic, ring: Ring, live: LiveOut, auth=None) -> None:
        self._mic, self._ring, self._live, self.auth = mic, ring, live, auth
        self.volume = int(VOLUME)
        self.armed_until = 0.0
        self.hfp = None  # звонок: кнопка Алисы принимает/сбрасывает (station_hfp)

    def on_key(self, code: int) -> None:
        if self.auth is not None:
            # окно настройки (Improv по Bluetooth, страница) — любой кнопкой: важно, что человек рядом с колонкой;
            # 07.11 Шифу искал «авторизационную кнопку» и нажал микрофон
            self.auth.grant()
        if code in (KEY_VOL_UP, KEY_VOL_DOWN):
            self.volume = STATION_STATE["volume"]  # голосом её мог поменять set_volume
            self._volume(VOL_STEP if code == KEY_VOL_UP else -VOL_STEP)
        elif code == KEY_MUTE:
            self._mic.user_muted = not self._mic.user_muted
            STATION_STATE["mic_muted"] = self._mic.user_muted
            self._mic.flush()
            if not self._live.busy():
                self._ring.idle()
            # голосом, а не только красным кольцом: днём кольцо плохо видно (Шифу 09.11: «непонятно, что произошло»)
            self._live.say_offline("mic_off" if self._mic.user_muted else "mic_on")
            log(f"🔘 микрофон {'выключен' if self._mic.user_muted else 'включён'}")
        elif code == KEY_ACTION:
            self._action()

    def _volume(self, step: int) -> None:
        self.volume = max(VOL_MIN, min(VOL_MAX, self.volume + step))
        STATION_STATE["volume"] = self.volume
        subprocess.run(["amixer", "-q", "sset", "Master", str(self.volume)], check=False)
        self._ring.level(self.volume)
        if not self._live.busy():
            self._click()
        log(f"🔘 громкость {self.volume}")

    def _action(self) -> None:
        hfp = self.hfp
        if hfp is not None and (hfp.ringing or hfp.in_call):
            asyncio.ensure_future(hfp.press())
            log(f"🔘 звонок: {'сброс' if hfp.in_call else 'принять'}")
            return
        if self._live.busy():
            self._live.stop()
            log("🔘 стоп: речь оборвана кнопкой")
        elif not self._mic.user_muted:
            self.armed_until = time.time() + ARM_S
            self._click()
            self._ring.set("listen")
            asyncio.get_running_loop().call_later(ARM_S, self._disarm)
            log(f"🔘 слушаю без вейка {ARM_S:.0f} с")

    def _click(self) -> None:
        """Щелчок отклика при заглушённом микрофоне: свой звук не должен уйти в VAD и облачный STT (07.11: каждый
        щелчок давал пустое распознавание)."""
        self._mic.muted = True
        click()

        def unmute() -> None:
            self._mic.flush()
            if not self._live.busy():
                self._mic.muted = False

        asyncio.get_running_loop().call_later(0.5, unmute)

    def _disarm(self) -> None:
        if time.time() >= self.armed_until and self._ring.state == "listen":
            self._ring.idle()

    def armed(self) -> bool:
        """Идёт окно «слушаю без вейка» (кнопка, голое имя, «Слушаю.»)."""
        return time.time() < self.armed_until

    def arm(self, ring: "Ring | None" = None, seconds: float = ARM_S) -> None:
        """Окно без вейка без щелчка: следующая фраза адресована колонке, и команды кода (обновись, пожалуйся)
        в ней работают — 08.11 «Игорёк» … «проверь обновления» ушло модели, и та ответила «не могу» (#13)."""
        self.armed_until = time.time() + seconds
        ring = ring or self._ring
        ring.set("listen")
        asyncio.get_running_loop().call_later(seconds, self._disarm)

    def address(self, text: str) -> str:
        """Фраза после кнопки Алисы адресована колонке: без вейка код подставляет его сам."""
        if not text or time.time() >= self.armed_until:
            return text
        self.armed_until = 0.0
        if any(w in text.lower() for w in WAKE_WORDS):
            return text
        return f"Игорёк, {text}"


async def start_setup(stand, mic: Mic, ring: Ring, buttons: "Buttons") -> None:
    """Improv Wi-Fi по Bluetooth и страница настроек (``station_setup``); сбой одного не роняет хост."""
    import station_setup as setup  # noqa: PLC0415

    auth = setup.Auth()
    buttons.auth = auth
    for name, value in (("RBX_WAKE", ",".join(WAKE_WORDS)), ("RBX_VOLUME", VOLUME), ("RBX_LED", str(LED_BRIGHT)),
                        ("RBX_RING_STYLE", "color")):
        os.environ.setdefault(name, value)  # форма страницы показывает то, что сейчас в силе

    def identify() -> None:
        click()
        ring.set("listen")
        asyncio.get_running_loop().call_later(1.5, ring.idle)

    def on_saved(new: dict, keys: list) -> None:
        global LED_BRIGHT
        os.environ.update(new)
        WAKE_WORDS[:] = [w for w in new["RBX_WAKE"].split(",") if w]
        stand.wake = list(WAKE_WORDS)
        LED_BRIGHT = int(new["RBX_LED"])
        buttons.volume = STATION_STATE["volume"] = int(new["RBX_VOLUME"])
        subprocess.run(["amixer", "-q", "sset", "Master", new["RBX_VOLUME"]], check=False)
        ring.idle()
        if keys:  # провайдеры собраны при старте — новые ключи берёт только новый процесс
            asyncio.get_running_loop().call_later(1.0, restart_self, mic)

    # страница первой и Improv под тайм-аутом: 09.11 BlueZ не ответил на регистрацию Improv (после перезапусков
    # bluealsa для звонков), ``await improv.start()`` повис — и вместе с ним страница, приветствие и весь слух колонки
    try:
        await setup.SettingsServer(SETTINGS, CORE / "scripts" / "dialogue" / ".env", auth, on_saved, log,
                                   prompts=station_prompts(setup),
                                   on_prompt=lambda: asyncio.get_running_loop().call_later(
                                       1.0, restart_self, mic, "промпт"),
                                   on_wifi=lambda ok: wifi_done(ok, ring, mic)).start()
    except OSError as exc:
        log(f"⚙ страница настроек не поднялась: {exc}")
    improv = setup.Improv(auth, identify, log)
    try:
        await asyncio.wait_for(improv.start(), IMPROV_START_S)
    except asyncio.TimeoutError:
        log(f"📶 Improv не ответил за {IMPROV_START_S:.0f} с — без Wi-Fi по Bluetooth, остальное работает")
    except Exception as exc:  # noqa: BLE001
        log(f"📶 Improv не поднялся: {type(exc).__name__}: {exc}")


def wifi_done(ok: bool, ring: "Ring", mic: Mic) -> None:
    """Итог смены сети: кольцо цветом; удача — перезапуск, чтобы факты устройства взяли новый адрес."""
    ring.flash("speak" if ok else "muted")
    if ok:
        asyncio.get_running_loop().call_later(4.0, restart_self, mic, "сеть Wi-Fi")


def wordless_tail(ring: "Ring | None", result, buttons: "Buttons | None" = None) -> bool:
    """Ход без слов (ядро: ``Addressed("", BARE_WAKE)`` — голое «Игорёк»): показать, что продолжаем слушать.

    08.11 Шифу: «на Игорька он булькает, но молчит» — кольцо гасилось сразу после earcon, и человек
    не понимал, ждут его или нет. Отвечать голосом на голое имя не надо (в ядре это штатный пустой ход),
    но кольцо должно держать «слушаю» столько же, сколько кнопка (``ARM_S``).
    """
    if ring is None or not getattr(result, "addressed", False) or getattr(result, "said", None):
        return False
    if buttons is not None:
        buttons.arm(ring)  # и фраза следом без имени — колонке (с командами кода), а не только кольцо
        return True
    ring.set("listen")
    asyncio.get_running_loop().call_later(ARM_S, ring.idle)
    return True


async def serve_setup_only() -> None:
    """Ключей STT нет — держим только страницу настроек: ключи вписывают там, дальше процесс перезапускается."""
    import station_setup as setup  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    auth = setup.Auth()
    Keys(loop, lambda code: auth.grant())  # любая кнопка колонки открывает окно настройки
    for name, value in (("RBX_WAKE", ",".join(WAKE_WORDS)), ("RBX_VOLUME", VOLUME), ("RBX_LED", str(LED_BRIGHT))):
        os.environ.setdefault(name, value)  # форма показывает то, что сейчас в силе

    def restart_self_soft() -> None:
        os.execv(sys.executable, [sys.executable, "-X", "utf8", "-u", SELF] + sys.argv[1:])

    def on_saved(new: dict, keys: list) -> None:
        os.environ.update(new)
        volume = int(new.get("RBX_VOLUME", VOLUME))
        subprocess.run(["amixer", "-q", "sset", "Master", str(volume)], check=False)
        if keys:  # ключи читает только новый процесс
            log("⚙ ключи сохранены — перезапуск")
            loop.call_later(1.0, restart_self_soft)

    try:
        await setup.SettingsServer(SETTINGS, CORE / "scripts" / "dialogue" / ".env", auth, on_saved, log,
                                   prompts=station_prompts(setup)).start()
    except OSError as exc:
        log(f"⚙ страница настроек не поднялась: {exc}")
        return
    await asyncio.Event().wait()


def station_prompts(setup):
    """Промпт колонки: свои копии в ``BASE/prompts``, основа ядра — в его ``rob_box_voice/prompts``."""
    return setup.Prompts(CORE / "src" / "rob_box_voice" / "prompts", BASE / "prompts")


#: Абсолютный путь хоста: ``main`` уходит в каталог ядра (``os.chdir(CORE)``), и относительный ``sys.argv[0]`` после
#: ``execv`` не находился (07.11: rc=2, хост поднимал только init-цикл через 10 с).
SELF = str(Path(__file__).resolve())


def restart_self(mic: Mic, why: str = "сменились ключи", announce: bool = True) -> None:
    """Перезапуск хоста тем же процессом (init-цикл не считает это падением): новые ключи, промпт, версия.

    ``announce`` — сказать «Перезапускаюсь» офлайн-фразой (у обновления своя фраза — там ``False``)."""
    log(f"⚙ перезапуск хоста: {why}")
    mic.proc.kill()
    pcm = station_offline.OfflinePhrases(OFFLINE_DIR, log).pcm("restarting") if announce else None
    if pcm:
        subprocess.run(aplay_cmd(), input=pcm, check=False, timeout=15)
    os.execv(sys.executable, [sys.executable, "-X", "utf8", "-u", SELF] + sys.argv[1:])


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
    try:
        voice = Voice()
    except SystemExit as exc:  # ключа STT нет: диалога не будет, но страницу для ввода ключа отдаём
        log(f"⚙ {exc} — поднимаю только страницу настроек")
        asyncio.run(serve_setup_only())
        return 0
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
