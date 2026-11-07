"""Сверка kws_run (C) с PyTorch на одних и тех же файлах: метка и вероятность лучшего окна."""
import glob, json, random, subprocess, sys
import torch, torchaudio
sys.path.insert(0, ".")
import train as T
meta = json.load(open("out/meta.json"))
labels, sr, n = meta["labels"], meta["sample_rate"], int(meta["sample_rate"] * meta["window_s"])
feats, model = T.Features(sr), T.DSCNN(len(labels), meta["width"])
model.load_state_dict(torch.load("out/model.pt", map_location="cpu")); model.eval()
files = []
for cls in ["igorek", "stop", "louder", "quieter", "_unknown_"]:
    fs = sorted(glob.glob(f"data/synth/{cls}/eugene__*.wav")) + sorted(glob.glob(f"data/real/{cls}/*.wav"))
    files += random.Random(1).sample(fs, min(4, len(fs)))
ok = 0
for f in files:
    w = T.load_wav(__import__("pathlib").Path(f), sr)[: int(sr * 2.0)]
    pcm = (w.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()
    c = subprocess.run(["out/kws_run.x86", "out/kws_model.bin", "2.0", "0.1"], input=pcm, capture_output=True).stdout.decode().split()
    x = torch.nn.functional.pad(w, (0, max(0, n - len(w))))
    starts = range(0, len(x) - n + 1, 1600) or [0]
    with torch.no_grad():
        probs = torch.softmax(model(feats(torch.stack([x[s:s + n] for s in starts]))), -1)
    probs[:, labels.index("_silence_")] = -1
    best = probs.max(0).values; k = int(best.argmax())
    same = c[0] == labels[k] and abs(float(c[1]) - float(best[k])) < 0.02
    ok += same
    print(f"{f.split('/')[-2]:10} C: {c[0]:9} {c[1]}  torch: {labels[k]:9} {best[k]:.3f}  {'✓' if same else '✗'}")
print(f"совпало {ok} из {len(files)}")
