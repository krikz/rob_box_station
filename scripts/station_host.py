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
#: 429 от MiniMax STT: провайдер режет одновременные запросы (08.11: две колонки на одном ключе).
#: Повтор с паузой дешевле молчания — 20 с паузы хост делает только если все попытки отбились.
STT_429_TRIES = int(os.environ.get("RBX_STT_429_TRIES", "3"))
STT_429_PAUSE = float(os.environ.get("RBX_STT_429_PAUSE", "0.6"))
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
    """STT и TTS MiniMax (ключи из ``scripts/dialogue/.env``)."""

    def __init__(self) -> None:
        from rob_box_llm.providers.minimax_tts import MiniMaxTTSProvider  # noqa: PLC0415
        from rob_box_voice.stt_providers.minimax_provider import MiniMaxSTTProvider  # noqa: PLC0415

        self.tts = MiniMaxTTSProvider()
        self.stt = MiniMaxSTTProvider.maybe_from_env()
        if self.stt is None:
            raise SystemExit("нет MINIMAX_API_KEY для STT")
        self.limited = False
        self.proc: subprocess.Popen | None = None  # текущий aplay речи — его глушит кнопка
        self.cut = False

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
        """Последний отказ STT — лимит провайдера (429): без паузы шум съест квоту."""
        return self.limited

    def hear(self, pcm: bytes) -> str:
        from rob_box_voice.stt_providers.minimax_provider import (  # noqa: PLC0415
            MiniMaxSTTError, MiniMaxSTTRateLimitError)

        self.limited = False
        wav = wav_bytes(pcm)
        for attempt in range(STT_429_TRIES):
            try:
                return (self.stt.transcribe(wav).text or "").strip()
            except MiniMaxSTTRateLimitError:
                # 429 у MiniMax транзиентный: 08.11 две колонки на одном ключе по очереди получали отказ.
                # Переждать дешевле, чем молчать 20 с: пауза растёт 0,6 -> 1,2 с.
                if attempt + 1 >= STT_429_TRIES:
                    self.limited = True
                    break
                log(f"STT 429 — повтор {attempt + 2}/{STT_429_TRIES}")
                time.sleep(STT_429_PAUSE * (attempt + 1))
            except MiniMaxSTTError as exc:
                log(f"STT ошибка: {type(exc).__name__}")
                break
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


#: Живое состояние колонки для инструментов статуса (кнопки и хост его обновляют).
STATION_STATE = {"volume": int(VOLUME), "mic_muted": False, "started": time.time()}


#: Живые примеры для модели ключевых слов (``training/kws``): ``pos`` — первая секунда обращения с вейком в начале,
#: ``neg`` — секунда из фразы без вейка. Потолок — место на ``/data`` (колонка, 35 МБ свободно); ``/tmp`` — в ОЗУ.
KWS_DIR = Path(os.environ.get("RBX_KWS_DIR", str(BASE / "kws_real")))
KWS_CAP_BYTES = int(os.environ.get("RBX_KWS_CAP_MB", "10")) * 2**20


#: Своя модель ключевых слов (``training/kws``, программа на C ``kws_run``). Пока режим тени: считает каждую фразу
#: параллельно с облачным STT и пишет ответ в лог — решение ещё за облаком (сравнить на живой речи, потом доверять).
KWS_BIN = BASE / "kws" / "kws_run"
KWS_MODEL = BASE / "kws" / "kws_model.bin"


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


def kws_collect(pcm: bytes, text: str) -> None:
    """Сохранить секунду фразы как живой пример (вейк в начале — ``pos``, без вейка — ``neg``); полно — молча мимо."""
    words = [w.strip(",.!?") for w in text.lower().split()]
    if not words:
        return
    kind = "pos" if words[0] in WAKE_WORDS else ("neg" if not set(words) & set(WAKE_WORDS) else "")
    if not kind:
        return  # вейк в середине — где он, без меток времени STT не знаем
    try:
        used = sum(f.stat().st_size for f in KWS_DIR.rglob("*.wav")) if KWS_DIR.exists() else 0
        if used > KWS_CAP_BYTES:
            return
        sec = RATE * 2
        start = 0 if kind == "pos" else max(0, (len(pcm) - sec) // 2 // 2 * 2)
        chunk = pcm[start:start + int(sec * 1.2)]
        (KWS_DIR / kind).mkdir(parents=True, exist_ok=True)
        (KWS_DIR / kind / f"real__{int(time.time() * 1000)}.wav").write_bytes(wav_bytes(chunk))
    except OSError as exc:
        log(f"kws: пример не сохранён: {exc}")


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
            "тринадцать»).")


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


def make_stand(args: argparse.Namespace):
    import chat  # noqa: PLC0415

    import station_setup as setup  # noqa: PLC0415

    install_llm_hedge()
    prompts = station_prompts(setup)
    chat.dm.PROMPT_DIR = prompts.materialize(Path(args.workdir) / "prompt", device_facts())  # основа + дополнение
    log(f"промпт: основа {'своя' if prompts.is_own() else 'ядра'}"
        f"{', дополнение ' + str(len(prompts.extra().strip())) + ' знаков' if prompts.extra().strip() else ''}")
    t0 = time.time()
    cargs = chat.build_arg_parser().parse_args(["--wake-word"] + (["--db", args.db] if args.db else []))
    stand = chat.build_stand(cargs, Path(args.workdir))
    stand.wake = list(WAKE_WORDS)  # таблица вейков робота (wake_words.yaml) колонке не подходит
    install_station_tools(stand)
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
    updater = station_update.Updater(BASE, lambda: os.environ.get("GITHUB_TOKEN", ""), log)
    asyncio.get_running_loop().call_later(30, station_update.OK_MARK.touch)  # «здоров» — сторож отката не нужен
    log(f"версия колонки: {updater.current()}")
    Keys(asyncio.get_running_loop(), buttons.on_key)
    await start_setup(stand, mic, ring, buttons)
    try:
        while True:
            pcm = await asyncio.to_thread(vad.utterance, mic)
            t1 = time.time()
            kws_task = asyncio.ensure_future(asyncio.to_thread(kws_score, pcm))  # тень: параллельно с облаком
            text = await asyncio.to_thread(voice.hear, pcm)
            t2 = time.time()
            label, prob = await kws_task
            log(f"🎤 {len(pcm) / 2 / RATE:.1f} с → STT {t2 - t1:.2f} с: «{text}»"
                + (f"  🔑 kws {label} {prob:.2f}" if label else ""))
            kws_collect(pcm, text)
            if not text:
                if voice.stt_limited():
                    log("STT 429 — пауза 20 с")
                    await asyncio.sleep(20)
                    mic.flush()
                continue
            text = buttons.address(text)
            live.begin(t1)
            words = text.split()
            if words and words[0].lower().strip(",.!?") in WAKE_WORDS:
                command = station_update.update_command(" ".join(words[1:]))
                if command:  # обновление: решает код; «проверь» — только рассказать, «обновись» — поставить
                    live.earcon()
                    tag, phrase = await asyncio.to_thread(updater.latest)
                    live.speak(phrase + (" Скажи «Игорёк, обновись», чтобы поставить." if tag and command == "check"
                                         else ""))
                    done = await asyncio.to_thread(updater.install, tag) if tag and command == "install" else ""
                    if tag and command == "install":
                        live.speak(done or "Не смог скачать обновление, попробуй позже.")
                    await live.drain()
                    if done.startswith("Обновляюсь"):
                        restart_self(mic, "обновление")
                    mic.flush()
                    continue
                reply = await bt.handle(" ".join(words[1:]))
                if reply:  # Bluetooth-музыка: решает код, ход модели не нужен
                    live.earcon()
                    live.speak(reply)
                    await live.drain()
                    mic.flush()
                    continue
            result = await stand.hear(text, chat.CONSOLE_VOICE, source=chat.SOURCE_VOICE)
            t3 = time.time()
            if not result.addressed:
                log(f"… не адресовано ({result.reason})")
                continue
            for line in result.log.splitlines():
                if any(k in line for k in ("violation", "bad_", "repair", "rejected", "fields_dropped")):
                    log(f"   ядро: {line[:300]}")
            log(f"ход {t3 - t2:.2f} с: {result.said}")
            await live.drain()
            if wordless_tail(ring, result):
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
        self._queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        self._stand = stand
        self._t1 = 0.0
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
        if applied and applied != os.environ.get("RBX_VOICE"):
            save_setting("RBX_VOICE", applied)  # голос, выбранный словами, переживает перезапуск
        return applied or os.environ.get("RBX_VOICE") or None

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
            if self._ring:
                self._ring.set("speak")
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
                if self._ring and self._queue.empty():
                    self._ring.idle()

    def speak(self, text: str) -> None:
        """Фраза кода (не хода модели) — в ту же очередь динамика."""
        self._queue.put_nowait((text, self.voice()))

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
        if self._ring and self._queue.empty():
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

    Слушаю / думаю / говорю / подключаюсь к сети / ошибка — родные анимации Алисы (``RING_PATTERNS``), их нет —
    эффекты кода (дыхание, бегущий огонёк, пульс). Микрофон выкл — ровный красный, громкость — белые диоды по уровню
    на 1,2 с. Яркость ``LED_BRIGHT`` из 255: 255 — как у Алисы. В sysfs пишется только изменившееся."""

    COLORS = {"off": (0, 0, 0), "listen": (0, 0, 1), "think": (1, 0, 1), "speak": (0, 1, 0), "muted": (1, 0, 0),
              "connect": (1, 1, 1), "error": (1, 0, 0)}
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
        if self.state in ("think", "connect"):
            head = (t / 0.9 * 3) % 3  # огонёк обходит три диода за 0,9 с
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
                pattern = self._patterns.get(self.state)
                frame = self._pattern_frame(pattern, now - self._since) if pattern else None
                if frame is not None:
                    self._show(frame)
                    wait = 0.04  # кадры Алисы — по 40 мс
                elif pattern is not None:  # одноразовый шаблон (ошибка) кончился
                    self.idle()
                else:
                    rgb = self.COLORS[self.state]
                    self._show([tuple(int(255 * c * k) for c in rgb) for k in self._frame(now - self._since)])
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
    for name, value in (("RBX_WAKE", ",".join(WAKE_WORDS)), ("RBX_VOLUME", VOLUME), ("RBX_LED", str(LED_BRIGHT))):
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

    improv = setup.Improv(auth, identify, log)
    try:
        await improv.start()
    except Exception as exc:  # noqa: BLE001
        log(f"📶 Improv не поднялся: {type(exc).__name__}: {exc}")
    try:
        await setup.SettingsServer(SETTINGS, CORE / "scripts" / "dialogue" / ".env", auth, on_saved, log,
                                   prompts=station_prompts(setup),
                                   on_prompt=lambda: asyncio.get_running_loop().call_later(
                                       1.0, restart_self, mic, "промпт"),
                                   on_wifi=lambda ok: wifi_done(ok, ring, mic)).start()
    except OSError as exc:
        log(f"⚙ страница настроек не поднялась: {exc}")


def wifi_done(ok: bool, ring: "Ring", mic: Mic) -> None:
    """Итог смены сети: кольцо цветом; удача — перезапуск, чтобы факты устройства взяли новый адрес."""
    ring.flash("speak" if ok else "muted")
    if ok:
        asyncio.get_running_loop().call_later(4.0, restart_self, mic, "сеть Wi-Fi")


def wordless_tail(ring: "Ring | None", result) -> bool:
    """Ход без слов (ядро: ``Addressed("", BARE_WAKE)`` — голое «Игорёк»): показать, что продолжаем слушать.

    08.11 Шифу: «на Игорька он булькает, но молчит» — кольцо гасилось сразу после earcon, и человек
    не понимал, ждут его или нет. Отвечать голосом на голое имя не надо (в ядре это штатный пустой ход),
    но кольцо должно держать «слушаю» столько же, сколько кнопка (``ARM_S``).
    """
    if ring is None or not getattr(result, "addressed", False) or getattr(result, "said", None):
        return False
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


def restart_self(mic: Mic, why: str = "сменились ключи") -> None:
    """Перезапуск хоста тем же процессом (init-цикл не считает это падением): новые ключи, промпт, версия."""
    log(f"⚙ перезапуск хоста: {why}")
    mic.proc.kill()
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
