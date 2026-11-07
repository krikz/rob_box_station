#!/usr/bin/env python3
"""``out/model.pt`` → ``out/kws_model.bin`` для ``kws_run`` на колонке: BatchNorm слит в свёртки, признаки — те же.

Формат (little-endian): ``KWS1``, int32 [width, n_classes, n_mels, n_fft, hop, frames, sample_rate], float32: окно
Ханна [n_fft], банк mel [n_fft/2+1 × n_mels], stem [W×25] + [W], 4 блока (dw [W×9] + [W], pw [W×W] + [W]),
fc [C×W] + [C]; затем int32 длина и UTF-8 метки через ``\\n``.

    venv/bin/python export_c.py
"""
import json
import struct
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent


def fold(w: torch.Tensor, sd: dict, bn: str):
    scale = sd[f"{bn}.weight"] / torch.sqrt(sd[f"{bn}.running_var"] + 1e-5)
    bias = sd[f"{bn}.bias"] - sd[f"{bn}.running_mean"] * scale
    return w * scale.view(-1, *([1] * (w.dim() - 1))), bias


def main() -> None:
    out = HERE / "out"
    sd = torch.load(out / "model.pt", map_location="cpu")
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    width, labels = meta["width"], meta["labels"]
    n_fft, hop, n_mels, sr = meta["n_fft"], meta["hop"], meta["n_mels"], meta["sample_rate"]
    frames = 1 + (int(sr * meta["window_s"]) - n_fft) // hop
    fb = np.load(out / "kws.npz")["mel_fb"].astype(np.float32)  # [n_fft/2+1, n_mels]
    hann = torch.hann_window(n_fft, periodic=True).numpy().astype(np.float32)
    parts = [hann, fb]
    w, b = fold(sd["stem.0.weight"], sd, "stem.1")
    parts += [w.numpy(), b.numpy()]
    for k in range(4):
        i = k * 6
        w, b = fold(sd[f"body.{i}.weight"], sd, f"body.{i + 1}")
        parts += [w.numpy(), b.numpy()]
        w, b = fold(sd[f"body.{i + 3}.weight"], sd, f"body.{i + 4}")
        parts += [w.numpy(), b.numpy()]
    parts += [sd["head.weight"].numpy(), sd["head.bias"].numpy()]
    text = "\n".join(labels).encode("utf-8")
    with open(out / "kws_model.bin", "wb") as f:
        f.write(b"KWS1" + struct.pack("<7i", width, len(labels), n_mels, n_fft, hop, frames, sr))
        for p in parts:
            f.write(np.ascontiguousarray(p, dtype=np.float32).tobytes())
        f.write(struct.pack("<i", len(text)) + text)
    print(f"kws_model.bin: {(out / 'kws_model.bin').stat().st_size} байт, ширина {width}, классы {labels}")


if __name__ == "__main__":
    main()
