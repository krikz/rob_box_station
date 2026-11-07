import numpy as np
from tflite_runtime.interpreter import Interpreter
B = "alice_bio/"
def run(path, feed):
    it = Interpreter(model_path=B + path)
    inp = it.get_input_details()[0]
    if list(inp["shape"]) != list(feed.shape):
        it.resize_tensor_input(inp["index"], list(feed.shape))
    it.allocate_tensors()
    it.set_tensor(it.get_input_details()[0]["index"], feed.astype(np.float32))
    it.invoke()
    return it.get_tensor(it.get_output_details()[0]["index"])
feats = np.load("bio_feats.npz")
embs = {}
for name in feats.files:
    fb = feats[name]; n = len(fb) // 20 * 20
    if n == 0: continue
    body = np.concatenate([run("tflite-online-ecapa-tdnn-frontend/body.tflite", fb[i:i+20][None]) for i in range(0, n, 20)], 1)
    emb = run("tflite-online-ecapa-tdnn-frontend/head.tflite", body)
    g = float(run("tflite-linear-gender-classifier/gender_classifier.tflite", emb)[0, 0])
    a = float(run("tflite-linear-age-classifier/age_classifier.tflite", emb)[0, 0])
    embs[name] = emb[0]
    print(f"{name:28} пол={g:.3f} возраст={a:.3f} кадров={len(fb)}")
cos = lambda x, y: float(np.dot(x, y) / np.linalg.norm(x) / np.linalg.norm(y))
s = [k for k in embs if k.startswith("silero")]
print("сходство голосов Silero:"); [print(f"{a[7:]:8}", " ".join(f"{cos(embs[a], embs[b]):.2f}" for b in s)) for a in s]
k = [x for x in embs if x.startswith("kolonka")]
if len(k) > 1: print("сходство записей колонки между собой:", " ".join(f"{cos(embs[k[0]], embs[x]):.2f}" for x in k[1:6]))
