#!/usr/bin/env python3
"""Сухой прогон «Игорёк, обновись»: сравнить VERSION колонки с опубликованным релизом.

Ничего не ставит и на колонку не ходит. Печатает решение кода: катить или нет, и фразу,
которую хост скажет из этого решения (ADR-0148). Живой деплой — только с разрешением.

Примеры::

    python3 -I tools/update_dry_run.py --version v0.2.4
    python3 -I tools/update_dry_run.py --version-file /path/VERSION --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_update as su  # noqa: E402


def decide(current: str, token: str = "") -> dict:
    """Решение по опубликованным релизам. ``install`` зовётся только если тег новее."""
    updater = su.Updater(Path("."), lambda: token, lambda msg: None)
    updater.current = lambda: current  # type: ignore[method-assign]
    tag, phrase = updater.latest()
    roll = bool(tag) and su.newer_than(tag, current)
    return {
        "current": current,
        "tag": tag,
        "roll": roll,
        "phrase": phrase,
        "command": "install" if roll else "stay",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--version", default="", help="текущая VERSION, например v0.2.4")
    parser.add_argument("--version-file", default="", help="файл VERSION с колонки (только чтение)")
    parser.add_argument("--token", default="", help="GITHUB_TOKEN, если репозиторий закрыт")
    parser.add_argument("--json", action="store_true", help="отдать решение JSON-ом")
    args = parser.parse_args(argv)
    current = args.version.strip()
    if args.version_file:
        current = Path(args.version_file).read_text(encoding="utf-8").strip()
    if not current:
        parser.error("нужна --version или --version-file")
    decision = decide(current, args.token)
    if args.json:
        print(json.dumps(decision, ensure_ascii=False))
    else:
        print(f"текущая: {decision['current']}")
        print(f"тег: {decision['tag'] or '—'}")
        print(f"катить: {'да' if decision['roll'] else 'нет'}")
        print(f"фраза: {decision['phrase']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
