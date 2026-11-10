#!/usr/bin/env python3
"""Бандл ядра диалога для колонки из ТЕГА ``krikz/rob_box_dialog_core`` (ADR-0167 Ш3, #4547 в rob_box_project).

Ядро версионируется тегами в своём репозитории; ``tools/core_ref.txt`` хранит тег (до Ш3 — монорепо-коммит
``krikz/rob_box_project``, оттуда больше не собираем). Ассет релиза ``station-core.tar.gz`` (``rbx/core/…``)
собирается этим скриптом из чекаута core-репозитория:

    python -I tools/build_core.py ../rob_box_dialog_core station-core.tar.gz          # ref из tools/core_ref.txt
    python -I tools/build_core.py ../rob_box_dialog_core out.tar.gz --ref v0.7.2

Файлы берутся ``git archive`` из тега (ровно то, что в истории, LF), по списку ``tools/core_manifest.txt``:
каталог — целиком, кроме тестов и кэша; файл — как есть. В архив кладётся ``rbx/core/SOURCE`` (репозиторий, ref,
полный хэш) — по нему видно, из чего собрано ядро на колонке. ``.env`` в архиве нет и быть не может.

ВНИМАНИЕ (связанные правки хоста, без них колонка на v0.7.2 не поднимется): в ядре больше нет ``rob_box_voice``
— STT MiniMax хост берёт из ``rob_box_llm.stt_providers`` (тот же интерфейс), а стенд после c81309c17 в
rob_box_project системный промпт строит из данных пакета, контракт ``PROMPT_DIR`` выпилен.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "core_manifest.txt"
REF_FILE = HERE / "core_ref.txt"
PREFIX = "rbx/core/"
#: Что из каталогов пакетов не нужно колонке: тесты, кэш, документация.
SKIP_PARTS = {"test", "tests", "__pycache__"}
SKIP_SUFFIXES = (".pyc", ".md")


def manifest(path: Path = MANIFEST) -> list:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def wanted(name: str, entries: list) -> bool:
    """Файл архива входит в бандл: совпал с файлом списка или лежит в каталоге списка (не тест, не кэш)."""
    parts = name.split("/")
    if SKIP_PARTS & set(parts[:-1]) or name.endswith(SKIP_SUFFIXES) or parts[-1] == ".env":
        return False
    return any(name == e or (e.endswith("/") and name.startswith(e)) for e in entries)


def git(repo: Path, *args: str) -> bytes:
    # без перевода строк: на Windows с core.autocrlf=true ``git archive`` отдаёт CRLF (так CRLF и попал в ядро
    # колонки 07.11), а сборка на ПК и в CI должна давать один и тот же архив — как в коммите
    return subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "core.eol=lf", *args],
                          check=True, capture_output=True).stdout


def build(repo: Path, out: Path, ref: str, entries: list) -> dict:
    sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    paths = [e.rstrip("/") for e in entries]
    raw = git(repo, "archive", "--format=tar", sha, "--", *paths)
    stamp = int(git(repo, "show", "-s", "--format=%ct", sha).decode().strip() or time.time())
    count = size = 0
    digest = hashlib.sha256()
    # gzip без имени и времени в заголовке: повторная сборка того же коммита — байт в байт тот же архив
    with tarfile.open(fileobj=io.BytesIO(raw)) as src, open(out, "wb") as fh,             gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz,             tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as dst:
        for member in sorted((m for m in src.getmembers() if m.isfile()), key=lambda m: m.name):
            if not wanted(member.name, entries):
                continue
            data = src.extractfile(member).read()
            _add(dst, PREFIX + member.name, data, stamp)
            digest.update(member.name.encode() + b"\0" + data)
            count, size = count + 1, size + len(data)
        source = (f"repo krikz/rob_box_dialog_core\nref {ref}\ncommit {sha}\nfiles {count}\n"
                  f"content_sha256 {digest.hexdigest()}\n")
        _add(dst, PREFIX + "SOURCE", source.encode(), stamp)
    return {"commit": sha, "files": count, "bytes": size, "archive": out.stat().st_size,
            "content_sha256": digest.hexdigest()}


def _add(tar: tarfile.TarFile, name: str, data: bytes, mtime: int) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o644, mtime  # mtime коммита — повторная сборка даёт тот же архив
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo", type=Path, help="чекаут krikz/rob_box_dialog_core (нужен тег из tools/core_ref.txt)")
    ap.add_argument("out", type=Path)
    ap.add_argument("--ref", default="", help=f"тег/коммит; по умолчанию — {REF_FILE.name}")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows — cp1252
    ref = args.ref or REF_FILE.read_text(encoding="utf-8").split()[0]
    info = build(args.repo, args.out, ref, manifest())
    print(f"ядро: тег {ref}, коммит {info['commit'][:12]}, файлов {info['files']}, {info['bytes'] / 2**20:.2f} МБ → "
          f"{args.out} {info['archive'] / 2**20:.2f} МБ, содержимое sha256 {info['content_sha256'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
