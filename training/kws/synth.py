#!/usr/bin/env python3
"""Озвучка фраз из ``config.yaml`` голосами Silero (офлайн, GPU): ``data/synth/<класс>/*.wav``, 16 кГц моно.

Каждая фраза — ``per_phrase`` раз случайным голосом, скоростью и высотой (SSML ``prosody``). Негативы — в
``data/synth/_unknown_``. Уже озвученное не переозвучивается: добавил слово в конфиг — допишется только оно.

    venv/bin/python synth.py [--config config.yaml]
"""
import argparse
import hashlib
import random
from pathlib import Path

import torch
import torchaudio
import yaml

HERE = Path(__file__).resolve().parent


def load_tts(device: str):
    model, _ = torch.hub.load("snakers4/silero-models", "silero_tts", language="ru", speaker="v4_ru",
                              trust_repo=True)
    model.to(torch.device(device))
    return model


def say(model, text: str, speaker: str, rate: str, pitch: str) -> torch.Tensor:
    ssml = f'<speak><prosody rate="{rate}" pitch="{pitch}">{text}</prosody></speak>'
    audio = model.apply_tts(ssml_text=ssml, speaker=speaker, sample_rate=24000, put_accent=True, put_yo=True)
    return torchaudio.functional.resample(audio.float().cpu(), 24000, 16000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    s = cfg["synth"]
    out = HERE / "data" / "synth"
    jobs = [(name, p, s["per_phrase"]) for name, phrases in cfg["classes"].items() for p in phrases]
    jobs += [("_unknown_", p, s["per_negative"]) for p in cfg["negatives"]]
    model = load_tts("cuda" if torch.cuda.is_available() else "cpu")
    made = 0
    for cls, phrase, count in jobs:
        folder = out / cls
        folder.mkdir(parents=True, exist_ok=True)
        rnd = random.Random(phrase)
        for i in range(count):
            speaker, rate, pitch = rnd.choice(s["speakers"]), rnd.choice(s["rates"]), rnd.choice(s["pitches"])
            tag = hashlib.md5(f"{phrase}|{speaker}|{rate}|{pitch}|{i}".encode()).hexdigest()[:10]
            path = folder / f"{speaker}__{tag}.wav"
            if path.exists():
                continue
            try:
                wav = say(model, phrase, speaker, rate, pitch)
            except Exception as exc:  # noqa: BLE001 — одна фраза не валит озвучку
                print(f"пропуск «{phrase}» {speaker}: {exc}")
                continue
            torchaudio.save(str(path), wav.unsqueeze(0), 16000)
            made += 1
    total = sum(1 for _ in out.rglob("*.wav"))
    print(f"озвучено новых: {made}, всего файлов: {total}")


if __name__ == "__main__":
    main()
