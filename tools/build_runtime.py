#!/usr/bin/env python3
"""Рунтайм колонки ``station-runtime-armv7.tar.gz`` из исходников, а не снимком живой колонки (#4549 в rob_box_project).

До 09.11 ассет снимали с колонки (``tar cf - python rbx/site``) — повторить нельзя. Теперь всё закреплено в
``tools/runtime.lock``: Питон python-build-standalone (URL + sha256) и колёса PyPI (имя файла + sha256). Сборка
только скачивает ровно эти файлы, проверяет хэши, режет Питон правилом ``prune_python.DROP``, раскладывает колёса
в ``rbx/site/`` и пакует детерминированно (одинаковые входы — байт в байт тот же архив):

    python -I tools/build_runtime.py station-runtime-armv7.tar.gz [--cache dl/]
    python -I tools/build_runtime.py --lock                     # пересобрать lock из PINS (сеть, PyPI JSON)

В архиве ``python/…`` (в ``/data/opt``) и ``rbx/site/…`` (``/data/opt/rbx/site``) + ``rbx/RUNTIME`` — что внутри.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prune_python  # noqa: E402

LOCK = HERE / "runtime.lock"
PYTHON = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
          "cpython-3.12.16%2B20261026-armv7-unknown-linux-gnueabihf-install_only_stripped.tar.gz",
          "8ebcbfd83290fe028acd4de8af54b973332895d5aab3cb6382610437e35896cd")
#: Версии — как на 10.1.1.13 09.11 (``site/*.dist-info``): с ними ядро и хост проверены вживую.
PINS = ["annotated-types==0.8.0", "anyio==4.16.0", "certifi==2026.10.23", "dbus-next==0.2.3", "h11==0.16.0",
        "httpcore==1.0.9", "httpcore2==2.16.0", "httpx==0.28.1", "httpx2==2.16.0", "idna==3.20", "jiter==0.19.0",
        "openai==3.46.0", "pydantic==2.14.3", "pydantic-core==2.50.4", "sniffio==1.3.1", "truststore==0.10.5",
        "typing-extensions==4.16.0", "typing-inspection==0.4.5", "pyyaml==6.0.3"]
#: Колесо под колонку: CPython 3.12 armv7 (glibc колонки 2.30 — manylinux ≤ 2_30) или чистый Питон.
ARM_TAGS = ("cp312-cp312-manylinux", "cp312-abi3-manylinux", "cp38-abi3-manylinux", "cp37-abi3-manylinux",
            "cp39-abi3-manylinux")
#: Фиксированное время файлов в архиве (повторная сборка — тот же архив): 2026-10-31 00:00 UTC.
EPOCH = 1793404800


def _fetch(url: str, cache: Path) -> bytes:
    name = url.rsplit("/", 1)[-1].replace("%2B", "+")
    path = cache / name
    if path.exists():
        return path.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "rbx-station-build"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 — https, фикс. адреса
        data = resp.read()
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _checked(url: str, sha: str, cache: Path) -> bytes:
    data = _fetch(url, cache)
    got = hashlib.sha256(data).hexdigest()
    if got != sha:
        raise SystemExit(f"sha256 не совпал: {url}\n  ждал {sha}\n  есть {got}")
    return data


def _pick(files: list) -> dict:
    """Файл релиза под колонку: колесо armv7 cp312 (manylinux ≤ 2_30), иначе чистое колесо, иначе sdist."""
    def glibc_ok(name: str) -> bool:
        import re  # noqa: PLC0415

        tags = re.findall(r"manylinux_(\d+)_(\d+)_armv7l|manylinux2014_armv7l", name)
        return bool(tags) and all(t == ("", "") or (int(t[0]), int(t[1])) <= (2, 30) for t in tags)

    wheels = [f for f in files if f["packagetype"] == "bdist_wheel"]
    arm = [f for f in wheels if "armv7l" in f["filename"] and any(t in f["filename"] for t in ARM_TAGS)
           and glibc_ok(f["filename"])]
    pure = [f for f in wheels if f["filename"].endswith(("-py3-none-any.whl", "-py2.py3-none-any.whl"))]
    sdist = [f for f in files if f["packagetype"] == "sdist"]
    for group in (arm, pure, sdist):
        if group:
            return sorted(group, key=lambda f: f["filename"])[0]
    raise SystemExit(f"нет файла под armv7/cp312: {[f['filename'] for f in files]}")


def make_lock() -> None:
    rows = [f"python {PYTHON[0]} {PYTHON[1]}"]
    for pin in PINS:
        name, version = pin.split("==")
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as resp:  # noqa: S310
            info = json.load(resp)
        f = _pick(info["urls"])
        rows.append(f"{pin} {f['url']} {f['digests']['sha256']}")
        print(f"{pin}: {f['filename']}")
    LOCK.write_text("# tools/build_runtime.py --lock: что входит в рунтайм колонки (URL + sha256)\n"
                    + "\n".join(rows) + "\n", encoding="utf-8")


def read_lock() -> list:
    return [line.split() for line in LOCK.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def _site_files(name: str, data: bytes) -> dict:
    """Колесо → {путь в site: (байты, режим)}; sdist PyYAML → только чистый Питон ``lib/yaml``."""
    out = {}
    if name.endswith(".whl"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for i in z.infolist():
                top = i.filename.split("/", 1)[0]
                if i.is_dir() or top.endswith(".data") or "/__pycache__/" in i.filename:
                    continue
                out[i.filename] = (z.read(i), 0o644)
        return out
    with tarfile.open(fileobj=io.BytesIO(data)) as s:
        for m in s:
            p = m.name.split("/", 1)[1] if "/" in m.name else ""
            if p.startswith("lib/yaml/") and m.isfile():
                out["yaml/" + p[len("lib/yaml/"):]] = (s.extractfile(m).read(), 0o644)
    if not out:
        raise SystemExit(f"в {name} нет чистого Питона (lib/yaml)")
    return out


def build(out: Path, cache: Path) -> dict:
    rows = read_lock()
    entries: dict = {}  # путь в архиве → (байты | None для каталога/ссылки, TarInfo-шаблон)
    py = next(r for r in rows if r[0] == "python")
    with tarfile.open(fileobj=io.BytesIO(_checked(py[1], py[2], cache))) as src:
        for m in src:
            if prune_python.DROP.match(m.name) or not (m.isfile() or m.issym() or m.isdir()):
                continue
            entries[m.name] = (src.extractfile(m).read() if m.isfile() else None, m)
    wheels = []
    for pin, url, sha in (r for r in rows if r[0] != "python"):
        name = url.rsplit("/", 1)[-1]
        for path, (data, mode) in _site_files(name, _checked(url, sha, cache)).items():
            info = tarfile.TarInfo("rbx/site/" + path)
            info.mode = mode
            entries[info.name] = (data, info)
        wheels.append(f"{pin} {name} {sha}")
    manifest = ("python " + py[1].rsplit("/", 1)[-1].replace("%2B", "+") + " " + py[2] + "\n" +
                "\n".join(wheels) + "\n").encode()
    entries["rbx/RUNTIME"] = (manifest, tarfile.TarInfo("rbx/RUNTIME"))
    with open(out, "wb") as fh, gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as dst:
        for name in sorted(entries):
            data, tmpl = entries[name]
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tmpl.type, tmpl.linkname
            info.mode = tmpl.mode if tmpl.mode else (0o755 if tmpl.isdir() else 0o644)
            info.mtime, info.uid, info.gid, info.uname, info.gname = EPOCH, 0, 0, "root", "root"
            if data is not None:
                info.size = len(data)
            dst.addfile(info, io.BytesIO(data) if data is not None else None)
    files = sum(1 for d, t in entries.values() if d is not None)
    raw = sum(len(d) for d, _ in entries.values() if d is not None)
    return {"files": files, "bytes": raw, "archive": out.stat().st_size,
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", type=Path)
    ap.add_argument("--cache", type=Path, default=Path("dl_runtime"))
    ap.add_argument("--lock", action="store_true", help="пересобрать tools/runtime.lock по PINS (PyPI)")
    args = ap.parse_args()
    if args.lock:
        make_lock()
        return 0
    if args.out is None:
        ap.error("укажи файл архива")
    info = build(args.out, args.cache)
    print(f"рунтайм: файлов {info['files']}, {info['bytes'] / 2**20:.1f} МБ → {args.out} "
          f"{info['archive'] / 2**20:.2f} МБ, sha256 {info['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
