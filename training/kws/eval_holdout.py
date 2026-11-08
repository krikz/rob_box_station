#!/usr/bin/env python3
"""Честная проверка двух моделей на живых записях колонки, которых НЕ было в обучении.

Синтетическая проверка (`train.py`, голос ``val_speaker``) говорит на голосах Silero; на живой комнате и
микрофоне колонки цифры другие. 08.11: v2 (без живых) на отложенных живых фразах слышала вейк в 58 %
случаев, v3 (живые ×5) — в 100 %.

Запуск на песочнице (после ``train.py``), модели — собранные ``kws_model.bin``:

    venv/bin/python eval_holdout.py --golden out/v2/kws_model.bin --candidate out/kws_model.bin \\
        --pos data/holdout/igorek --neg data/holdout/_unknown_

Сравнение идёт через ту же C-реализацию (``out/kws_run.x86``), что стоит на колонке, поэтому цифры — про то,
что реально будет на железе, а не про PyTorch.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYWORDS = {"igorek", "stop", "louder", "quieter"}


def run(runner: Path, model: Path, wav: str) -> tuple:
    """(метка, вероятность) отрезка: WAV-заголовок 44 б, дальше PCM как отдаёт микрофон колонки."""
    pcm = open(wav, "rb").read()[44:]
    out = subprocess.run([str(runner), str(model), "2.0", "0.1"], input=pcm, capture_output=True).stdout.decode().split()
    return (out[0], float(out[1])) if len(out) > 1 else ("", 0.0)


def evaluate(runner: Path, model: Path, pos: list, neg: list, thr: float) -> dict:
    hits = [run(runner, model, w) for w in pos]
    others = [run(runner, model, w) for w in neg]
    return {
        "n_pos": len(hits),
        "n_neg": len(others),
        "wake_hit": sum(1 for label, _ in hits if label == "igorek") / max(1, len(hits)),
        "wake_confident": sum(1 for label, p in hits if label == "igorek" and p > thr) / max(1, len(hits)),
        "false_accept": sum(1 for label, p in others if label in KEYWORDS and p > thr) / max(1, len(others)),
        "mean_p_pos": round(sum(p for _, p in hits) / max(1, len(hits)), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--golden", required=True, help="kws_model.bin, с которым сравниваем (например, прошлый релиз)")
    ap.add_argument("--candidate", required=True, help="kws_model.bin новой модели")
    ap.add_argument("--pos", default=str(HERE / "data" / "holdout" / "igorek"), help="отложенные фразы с вейком")
    ap.add_argument("--neg", default=str(HERE / "data" / "holdout" / "_unknown_"), help="отложенные фразы без вейка")
    ap.add_argument("--runner", default=str(HERE / "out" / "kws_run.x86"), help="C-раннер, как на колонке")
    ap.add_argument("--threshold", type=float, default=0.8, help="порог уверенности вейка (хост: 0.8)")
    ap.add_argument("--json", action="store_true", help="только JSON, без таблицы")
    args = ap.parse_args()

    pos = sorted(glob.glob(os.path.join(args.pos, "*.wav")))
    neg = sorted(glob.glob(os.path.join(args.neg, "*.wav")))
    if not pos or not neg:
        print(f"нет отложенных записей: pos={len(pos)}, neg={len(neg)} — сначала отложи часть data/real", file=sys.stderr)
        return 2

    result = {}
    for tag, model in (("golden", args.golden), ("candidate", args.candidate)):
        result[tag] = evaluate(Path(args.runner), Path(model), pos, neg, args.threshold)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0

    print(f"отложено: {len(pos)} с вейком, {len(neg)} без вейка; порог {args.threshold}")
    print(f"{'модель':<10} {'вейк услышан':>13} {'уверенно':>9} {'ложный вейк':>12}")
    for tag in ("golden", "candidate"):
        r = result[tag]
        print(f"{tag:<10} {r['wake_hit']:>12.1%} {r['wake_confident']:>9.1%} {r['false_accept']:>12.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
