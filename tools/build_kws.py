#!/usr/bin/env python3
"""Ассет своей модели вейка ``station-kws-armv7.tar.gz`` → ``/data/opt/rbx/kws/`` (#4552 в rob_box_project).

Вторая колонка (10.1.1.141) стояла без ``rbx/kws``: ``install.sh`` модель не ставил, она жила только на первой
колонке и на песочнице. Теперь:

* ``kws_run`` — собирается из ``training/kws/kws_run.c`` (Debian bookworm, ``arm-linux-gnueabihf-gcc`` 12.2, статически,
  NEON): 09.11 такая сборка байт в байт совпала с бинарником на 10.1.1.13 (md5 ``df2ecaae…``);
* ``kws_model.bin`` — веса из ``training/kws/models/`` (данные обучения на GPU песочницы, в CI не пересчитываются;
  sha256 — в ``KWS`` внутри архива).

    python -I tools/build_kws.py training/kws/out/kws_run.armv7 station-kws-armv7.tar.gz
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = HERE.parent / "training" / "kws" / "models" / "kws_model.bin"
EPOCH = 1793404800  # то же фиксированное время, что у рунтайма: повторная сборка — тот же архив


def build(kws_run: Path, out: Path, model: Path = MODEL) -> str:
    binary, weights = kws_run.read_bytes(), model.read_bytes()
    if binary[:4] != b"\x7fELF" or binary[18] != 40:  # e_machine 40 = ARM
        raise SystemExit(f"{kws_run}: не ELF под ARM — собери build_kws.sh")
    note = (f"kws_run sha256 {hashlib.sha256(binary).hexdigest()} (training/kws/kws_run.c)\n"
            f"kws_model.bin sha256 {hashlib.sha256(weights).hexdigest()} (training/kws/models/kws_model.bin)\n").encode()
    with open(out, "wb") as fh, gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data, mode in (("rbx/kws/KWS", note, 0o644), ("rbx/kws/kws_model.bin", weights, 0o644),
                                 ("rbx/kws/kws_run", binary, 0o755)):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime, info.uname, info.gname = len(data), mode, EPOCH, "root", "root"
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(out.read_bytes()).hexdigest()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("kws_run", type=Path, help="kws_run.armv7 из training/kws/build_kws.sh")
    ap.add_argument("out", type=Path)
    ap.add_argument("--model", type=Path, default=MODEL)
    args = ap.parse_args()
    print(f"kws: {args.out} sha256 {build(args.kws_run, args.out, args.model)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
