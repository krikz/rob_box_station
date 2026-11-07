#!/usr/bin/env python3
"""Хост колонки (ADR-0160 §8 К2, #4247): микрофон → VAD → STT → ход v2 → TTS → динамик, ядро на самой колонке.

Ход строит тот же стенд, что консоль (``scripts/dialogue/chat.py``): настоящие адресация (вейк «Робот»), Tier-1, цепочка
LLM с мостом команд и исполнитель; владельцы, кроме речи, фейковые. Ядро грузится один раз, дальше процесс слушает.

Звук — штатные ``arecord``/``aplay`` колонки: ``multi_capture`` отдаёт 48 кГц, 6 каналов (микрофоны 0–3, петля 4–5),
берётся канал 0 и ресемплится в 16 кГц. Эхоподавления нет, поэтому полудуплекс: пока колонка говорит, микрофон
не слушается. VAD — по энергии с плавающим порогом шума.

    python3.12 -X utf8 station_host.py                 # слушать и отвечать
    python3.12 -X utf8 station_host.py --say "текст"   # только синтез в динамик (проверка TTS)
    python3.12 -X utf8 station_host.py --selftest      # колонка говорит фразу и сама её распознаёт
"""
from __future__ import annotations

import argparse
import array
import asyncio
import audioop  # в 3.12 ещё есть (удалён в 3.13); numpy на колонке нет
import io
import os
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

IN_RATE, IN_CH, RATE = 48000, 6, 16000
FRAME_S = 0.03
FRAME_IN = int(IN_RATE * FRAME_S) * IN_CH * 2  # байт сырого кадра 30 мс
START_FRAMES, END_SILENCE_S, MIN_S, MAX_S = 3, 0.8, 0.4, 12.0
TTS_FORMAT, TTS_RATE = "pcm_24000", 24000


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
    """Поток кадров 30 мс, 16 кГц моно, канал 0 ``multi_capture``; ``muted`` — кадры выбрасываются (полудуплекс)."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(["arecord", "-q", "-D", "multi_capture", "-f", "S16_LE", "-r", str(IN_RATE),
                                      "-c", str(IN_CH), "-t", "raw"], stdout=subprocess.PIPE, bufsize=0)
        self.state = None
        self.muted = False

    def frame(self) -> bytes:
        raw = b""
        while len(raw) < FRAME_IN:
            chunk = self.proc.stdout.read(FRAME_IN - len(raw))
            if not chunk:
                raise RuntimeError(f"arecord закрылся, код {self.proc.poll()}")
            raw += chunk
        mono = array.array("h", raw)[0::IN_CH].tobytes()
        out, self.state = audioop.ratecv(mono, 2, 1, IN_RATE, RATE, self.state)
        return out


class Vad:
    """Реплика по энергии: порог = шум × 3 (не ниже 300), конец — 0,8 с тишины."""

    def __init__(self) -> None:
        self.noise = 200.0

    def utterance(self, mic: Mic) -> bytes:
        voiced, pre, buf, silent = 0, [], [], 0.0
        while True:
            fr = mic.frame()
            if mic.muted:
                voiced, buf, silent = 0, [], 0.0
                continue
            rms = audioop.rms(fr, 2)
            thr = max(300.0, self.noise * 3)
            if not buf:
                self.noise = 0.95 * self.noise + 0.05 * rms if rms < thr else self.noise
                pre = (pre + [fr])[-10:]
                voiced = voiced + 1 if rms > thr else 0
                if voiced >= START_FRAMES:
                    buf = list(pre)
                continue
            buf.append(fr)
            silent = silent + FRAME_S if rms <= thr else 0.0
            dur = len(buf) * FRAME_S
            if (silent >= END_SILENCE_S and dur >= MIN_S) or dur >= MAX_S:
                return b"".join(buf)


def play(pcm: bytes, rate: int = TTS_RATE) -> None:
    subprocess.run(["aplay", "-q", "-D", "default", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-t", "raw"],
                   input=pcm, check=False)


class Voice:
    """STT и TTS MiniMax (ключи из ``scripts/dialogue/.env``)."""

    def __init__(self) -> None:
        from rob_box_llm.providers.minimax_tts import MiniMaxTTSProvider  # noqa: PLC0415
        from rob_box_voice.stt_providers.minimax_provider import MiniMaxSTTProvider  # noqa: PLC0415

        self.tts = MiniMaxTTSProvider()
        self.stt = MiniMaxSTTProvider.maybe_from_env()
        if self.stt is None:
            raise SystemExit("нет MINIMAX_API_KEY для STT")

    async def say(self, text: str, mic: Mic | None = None) -> float:
        t0 = time.time()
        pcm = await self.tts.synthesize_bytes(text, format=TTS_FORMAT)
        synth = time.time() - t0
        if mic:
            mic.muted = True
        try:
            await asyncio.to_thread(play, pcm)
        finally:
            if mic:
                await asyncio.sleep(0.3)  # хвост эха комнаты
                mic.muted = False
        return synth

    def hear(self, pcm: bytes) -> str:
        return (self.stt.recognize(wav_bytes(pcm)) or "").strip()


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


async def serve(voice: Voice, args: argparse.Namespace) -> None:
    import chat  # noqa: PLC0415

    t0 = time.time()
    cargs = chat.build_arg_parser().parse_args(["--wake-word"] + (["--db", args.db] if args.db else []))
    stand = chat.build_stand(cargs, Path(args.workdir))
    log(f"ядро готово за {time.time() - t0:.1f} с; слушаю (вейк «Робот»)")
    mic, vad = Mic(), Vad()
    try:
        while True:
            pcm = await asyncio.to_thread(vad.utterance, mic)
            t1 = time.time()
            text = await asyncio.to_thread(voice.hear, pcm)
            t2 = time.time()
            log(f"🎤 {len(pcm) / 2 / RATE:.1f} с → STT {t2 - t1:.2f} с: «{text}»")
            if not text:
                continue
            result = await stand.hear(text, chat.CONSOLE_VOICE, source=chat.SOURCE_VOICE)
            t3 = time.time()
            if not result.addressed:
                log(f"… не адресовано ({result.reason})")
                continue
            log(f"ход {t3 - t2:.2f} с: {result.said}")
            for line in result.said:
                synth = await voice.say(line, mic)
                log(f"🔊 синтез {synth:.2f} с, от конца фразы до звука {time.time() - t1:.2f} с: «{line}»")
    finally:
        mic.proc.kill()
        stand.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--say", default="", help="только синтез текста в динамик")
    ap.add_argument("--selftest", nargs="?", const="Робот, который час?", default=None, help="фраза самопроверки")
    ap.add_argument("--db", default="", help="постоянная e2e-память колонки")
    ap.add_argument("--workdir", default="/tmp/rbx_station", help="каталог стенда")
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    os.chdir(CORE)
    load_env()
    voice = Voice()
    if args.say:
        log(f"синтез {asyncio.run(voice.say(args.say)):.2f} с")
    elif args.selftest is not None:
        asyncio.run(selftest(voice, args.selftest))
    else:
        asyncio.run(serve(voice, args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
