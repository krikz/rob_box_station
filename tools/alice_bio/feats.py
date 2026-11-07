import glob, numpy as np, torch, torchaudio
def fbank(w):
    return torchaudio.compliance.kaldi.fbank(w.unsqueeze(0) * 32768, num_mel_bins=80, frame_length=40, frame_shift=24,
        preemphasis_coefficient=0.97, window_type="hamming", dither=0.0, sample_frequency=16000, use_log_fbank=True,
        use_power=True, round_to_power_of_two=True, snip_edges=True).numpy()
imp = torch.package.PackageImporter("/home/ros2/.cache/rob_box_voice/tts_models/v4_ru.pt")
tts = imp.load_pickle("tts_models", "model")
text = "Привет, Игорёк! Расскажи, пожалуйста, какая сегодня погода и что нового в мастерской."
out = {}
for spk in ["aidar", "eugene", "baya", "kseniya", "xenia"]:
    out["silero_" + spk] = fbank(torchaudio.functional.resample(tts.apply_tts(text=text, speaker=spk, sample_rate=48000), 48000, 16000))
for p in sorted(glob.glob("/tmp/kws_real/*/*.wav")):
    w, sr = torchaudio.load(p)
    out["kolonka_" + p.split("/")[-2] + "_" + p.split("/")[-1][-10:-4]] = fbank(w[0])
np.savez("/tmp/bio_feats.npz", **out)
print("признаки:", {k: v.shape[0] for k, v in out.items()})
