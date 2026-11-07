#!/usr/bin/env python3
"""Обучение модели ключевых слов колонки (DS-CNN на log-mel) по ``config.yaml``.

Данные:
* ``data/synth/<класс>/*.wav`` — озвучка ``synth.py`` (голос — префикс имени файла до ``__``);
* ``data/real/<класс>/*.wav`` — живые записи (колонка копит их сама, ``pull_real.sh``), всегда в обучении и в проверке;
* ``data/noise/*.wav`` — фон мастерской (запись с колонки); нет — белый/розовый шум.

Выход ``out/``: ``model.pt``, ``model.onnx``, ``kws.npz`` (веса + банк mel-фильтров — то же самое считает колонка),
``meta.json`` (метки, параметры признаков), ``report.txt`` (точность по классам, ложные срабатывания).

    venv/bin/python train.py [--config config.yaml]
"""
import argparse
import json
import random
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchaudio
import yaml

HERE = Path(__file__).resolve().parent
SILENCE, UNKNOWN = "_silence_", "_unknown_"
N_MELS, N_FFT, HOP = 40, 400, 160  # 25 мс окно, 10 мс шаг (как у споттера Алисы)


def load_wav(path: Path, sr: int) -> torch.Tensor:
    wav, rate = torchaudio.load(str(path))
    wav = wav.mean(0)
    return torchaudio.functional.resample(wav, rate, sr) if rate != sr else wav


def collect(root: Path, sr: int):
    """{класс: [(голос, волна)]} из ``root/<класс>/*.wav``."""
    out = {}
    for folder in sorted(p for p in root.glob("*") if p.is_dir()):
        out[folder.name] = [(f.name.split("__")[0], load_wav(f, sr)) for f in sorted(folder.glob("*.wav"))]
    return out


class Features(nn.Module):
    """log-mel 40 полос + вычитание среднего по времени (так же считает колонка)."""

    def __init__(self, sr: int) -> None:
        super().__init__()
        self.mel = torchaudio.transforms.MelSpectrogram(sr, n_fft=N_FFT, win_length=N_FFT, hop_length=HOP,
                                                        f_min=20, f_max=sr // 2 - 400, n_mels=N_MELS, center=False)

    def forward(self, wav: torch.Tensor) -> torch.Tensor:
        x = torch.log(self.mel(wav) + 1e-6)
        return (x - x.mean(-1, keepdim=True)).unsqueeze(1)  # [B, 1, mel, time]


class DSCNN(nn.Module):
    """Маленькая DS-CNN (при ширине 32 — ~6 тыс. параметров): под процессор колонки."""

    def __init__(self, n_classes: int, width: int = 64) -> None:
        super().__init__()
        self.stem = nn.Sequential(nn.Conv2d(1, width, (5, 5), stride=(2, 2), padding=2, bias=False),
                                  nn.BatchNorm2d(width), nn.ReLU())
        blocks = []
        for _ in range(4):
            blocks += [nn.Conv2d(width, width, 3, padding=1, groups=width, bias=False), nn.BatchNorm2d(width),
                       nn.ReLU(), nn.Conv2d(width, width, 1, bias=False), nn.BatchNorm2d(width), nn.ReLU()]
        self.body = nn.Sequential(*blocks)
        self.head = nn.Linear(width, n_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.body(self.stem(x))
        return self.head(x.mean((2, 3)))


class Mixer:
    """Примеры на лету: слово в случайном месте окна, сдвиг скорости, громкость, эхо, шум с нужным SNR."""

    def __init__(self, cfg: dict, clips: dict, noise: list, labels: list, rnd: random.Random) -> None:
        self.sr, self.n = cfg["sample_rate"], int(cfg["sample_rate"] * cfg["window_s"])
        self.t, self.clips, self.noise, self.labels, self.rnd = cfg["train"], clips, noise, labels, rnd

    def _noise(self) -> torch.Tensor:
        if self.noise:
            src = self.rnd.choice(self.noise)
            if len(src) > self.n:
                start = self.rnd.randrange(len(src) - self.n)
                return src[start:start + self.n].clone()
        white = torch.randn(self.n)
        return white if self.rnd.random() < 0.5 else torch.cumsum(white, 0) * 0.02  # белый / «красный»

    def _place(self, wav: torch.Tensor) -> torch.Tensor:
        if self.rnd.random() < 0.5:  # скорость ±10 %
            k = self.rnd.uniform(0.9, 1.1)
            wav = torchaudio.functional.resample(wav, int(self.sr * k), self.sr)
        out = torch.zeros(self.n)
        if len(wav) >= self.n:
            start = self.rnd.randrange(len(wav) - self.n + 1)
            out[:] = wav[start:start + self.n]
        else:
            start = self.rnd.randrange(self.n - len(wav) + 1)
            out[start:start + len(wav)] = wav
        if self.rnd.random() < 0.3:  # простое эхо комнаты
            ir = torch.exp(-torch.arange(int(0.08 * self.sr)) / (0.02 * self.sr)) * torch.randn(int(0.08 * self.sr))
            ir[0] = 1.0
            out = torchaudio.functional.fftconvolve(out, ir)[: self.n]
        return out / (out.abs().max() + 1e-6) * self.rnd.uniform(0.1, 0.9)

    def example(self, label: str, pool: str):
        noise = self._noise()
        if label == SILENCE:
            return noise / (noise.abs().max() + 1e-6) * self.rnd.uniform(0.0, 0.5), self.labels.index(SILENCE)
        speech = self._place(self.rnd.choice(self.clips[pool][label])[1])
        snr = self.rnd.uniform(*self.t["snr_db"])
        p_s, p_n = speech.pow(2).mean() + 1e-9, noise.pow(2).mean() + 1e-9
        mix = speech + noise * torch.sqrt(p_s / (p_n * 10 ** (snr / 10)))
        return mix / (mix.abs().max() + 1e-6) * self.rnd.uniform(0.2, 0.95), self.labels.index(label)

    def batch(self, size: int, pool: str):
        keywords = [c for c in self.labels if c not in (SILENCE, UNKNOWN) and self.clips[pool].get(c)]
        xs, ys = [], []
        for _ in range(size):
            r = self.rnd.random()
            if r < self.t["silence_share"]:
                label = SILENCE
            elif r < self.t["silence_share"] + self.t["unknown_share"] and self.clips[pool].get(UNKNOWN):
                label = UNKNOWN
            else:
                label = self.rnd.choice(keywords)
            x, y = self.example(label, pool)
            xs.append(x)
            ys.append(y)
        return torch.stack(xs), torch.tensor(ys)


def split(synth: dict, real: dict, val_speaker: str):
    """Пулы train/val: голос ``val_speaker`` — только в проверке; живые записи — в обоих (их мало)."""
    pools = {"train": {}, "val": {}}
    for cls, items in synth.items():
        pools["train"][cls] = [it for it in items if it[0] != val_speaker]
        pools["val"][cls] = [it for it in items if it[0] == val_speaker]
    for cls, items in real.items():
        for pool in pools.values():
            pool.setdefault(cls, []).extend(items)
    return pools


def evaluate(model, feats, mixer, labels, device, n: int = 3000):
    rnd_state = mixer.rnd.getstate()
    mixer.rnd.seed(12345)  # одна и та же проверочная выборка каждую эпоху
    x, y = mixer.batch(n, "val")
    mixer.rnd.setstate(rnd_state)
    model.eval()
    with torch.no_grad():
        prob = F.softmax(model(feats(x.to(device))), -1).cpu()
    model.train()
    pred = prob.argmax(-1)
    conf = np.zeros((len(labels), len(labels)), dtype=int)
    for t, p in zip(y.tolist(), pred.tolist()):
        conf[t, p] += 1
    kw = [i for i, c in enumerate(labels) if c not in (SILENCE, UNKNOWN)]
    neg = torch.tensor([t not in kw for t in y.tolist()])
    false_accept = ((prob[:, kw].max(-1).values > 0.8) & neg).sum().item() / max(1, neg.sum().item())
    return (pred == y).float().mean().item(), false_accept, conf


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    sr, t = cfg["sample_rate"], cfg["train"]
    labels = [SILENCE, UNKNOWN] + list(cfg["classes"])
    synth = collect(HERE / "data" / "synth", sr)
    real = collect(HERE / "data" / "real", sr) if (HERE / "data" / "real").exists() else {}
    noise = [load_wav(p, sr) for p in sorted((HERE / "data" / "noise").glob("*.wav"))]
    print(f"классы {labels}; озвучка {sum(map(len, synth.values()))}, живые {sum(map(len, real.values()))}, "
          f"фон {len(noise)} файлов")
    pools = split(synth, real, t["val_speaker"])
    mixer = Mixer(cfg, pools, noise, labels, random.Random(0))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    width = int(cfg.get("model", {}).get("width", 32))
    feats, model = Features(sr).to(device), DSCNN(len(labels), width).to(device)
    print(f"параметров модели: {sum(p.numel() for p in model.parameters())}, устройство {device}")
    opt = torch.optim.AdamW(model.parameters(), lr=t["lr"], weight_decay=1e-3)
    steps = t["epochs"] * (t["examples_per_epoch"] // t["batch"])
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=t["lr"], total_steps=steps)
    best = (-1.0, None)
    for epoch in range(t["epochs"]):
        for _ in range(t["examples_per_epoch"] // t["batch"]):
            x, y = mixer.batch(t["batch"], "train")
            loss = F.cross_entropy(model(feats(x.to(device))), y.to(device), label_smoothing=0.05)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
        acc, fa, _ = evaluate(model, feats, mixer, labels, device)
        print(f"эпоха {epoch + 1}/{t['epochs']}: потеря {loss.item():.3f}, точность {acc:.3f}, "
              f"ложные срабатывания {fa:.4f}", flush=True)
        if acc - fa > best[0]:
            best = (acc - fa, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()})
    model.load_state_dict(best[1])
    acc, fa, conf = evaluate(model, feats, mixer, labels, device, n=6000)
    export(model.cpu(), feats.cpu(), labels, cfg, acc, fa, conf)


def export(model, feats, labels, cfg, acc, fa, conf) -> None:
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    model.eval()
    torch.save(model.state_dict(), out / "model.pt")
    n = int(cfg["sample_rate"] * cfg["window_s"])
    example = feats(torch.zeros(1, n))
    torch.onnx.export(model, example, str(out / "model.onnx"), input_names=["logmel"], output_names=["logits"],
                      opset_version=13)
    fb = feats.mel.mel_scale.fb.numpy()  # [n_fft/2+1, n_mels]
    np.savez(out / "kws.npz", mel_fb=fb, **{k: v.numpy() for k, v in model.state_dict().items()})
    meta = {"labels": labels, "sample_rate": cfg["sample_rate"], "window_s": cfg["window_s"], "n_fft": N_FFT,
            "hop": HOP, "n_mels": N_MELS, "log_eps": 1e-6, "cmn": "mean over time", "input": list(example.shape),
            "width": int(cfg.get("model", {}).get("width", 32))}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"точность (голос, которого не было в обучении): {acc:.3f}",
             f"ложные срабатывания (чужие слова и тишина с уверенностью > 0.8): {fa:.4f}", "",
             "матрица ошибок (строка — правда, столбец — ответ модели):",
             "        " + " ".join(f"{c[:8]:>8}" for c in labels)]
    lines += [f"{c[:8]:>8}" + " ".join(f"{v:>8}" for v in row) for c, row in zip(labels, conf)]
    (out / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
