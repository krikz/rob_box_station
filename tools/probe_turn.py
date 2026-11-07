#!/usr/bin/env python3
"""Замер хода: сколько из него — запрос к LLM (сеть + модель), а сколько — ядро на своём процессоре.

Оборачивает ``AsyncCompletions.create`` openai-клиента и печатает по каждому запросу время, токены промпта/ответа,
попадание в кэш DeepSeek (``prompt_cache_hit_tokens``) и размер промпта в символах. Звука нет.

    # колонка (рядом со station_host.py):
    TZ=CET-1 /data/opt/python/bin/python3.12 -X utf8 probe_turn.py
    # ПК (из корня rob_box_project, ядро из чекаута):
    RBX_PC=1 python -X utf8 path/to/probe_turn.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("ROBOT_LLM_VERBOSE", "0")
PHRASES = sys.argv[1:] or ["Игорёк, как дела?", "Игорёк, который час?", "Игорёк, расскажи короткий анекдот."]

if os.environ.get("RBX_PC") == "1":
    root = Path.cwd()
    for sub in ("scripts/dialogue", "scripts/dialog"):
        sys.path.insert(0, str(root / sub))
    import chat  # noqa: E402

    chat.load_env_files([root / "scripts" / "dialogue" / ".env"])
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import station_host as sh  # noqa: E402  (кладёт пути ядра в sys.path)

    os.chdir(sh.CORE)
    sh.load_env()
    import chat  # noqa: E402

import openai.resources.chat.completions as occ  # noqa: E402

CALLS: list[dict] = []
_orig = occ.AsyncCompletions.create


async def _timed(self, *a, **kw):
    t = time.time()
    resp = await _orig(self, *a, **kw)
    u = resp.usage
    extra = (getattr(u, "model_extra", None) or {}) if u else {}
    CALLS.append({
        "s": time.time() - t, "model": kw.get("model"),
        "prompt": getattr(u, "prompt_tokens", None), "out": getattr(u, "completion_tokens", None),
        "cache_hit": extra.get("prompt_cache_hit_tokens"),
        "chars": sum(len(str(m.get("content") or "")) for m in kw.get("messages", [])),
        "tools_chars": len(str(kw.get("tools", ""))),
    })
    return resp


occ.AsyncCompletions.create = _timed

if os.environ.get("RBX_PC") != "1":
    sh.install_llm_hedge()  # RBX_HEDGE_S=0 — без хеджа


async def main() -> None:
    db = os.environ.get("RBX_DB", "")
    args = chat.build_arg_parser().parse_args(["--wake-word"] + (["--db", db] if db else []))
    t0 = time.time()
    stand = chat.build_stand(args, Path(tempfile.mkdtemp(prefix="probe_")))
    stand.wake = ["игорёк", "игорек"]
    print(f"стенд {time.time() - t0:.2f} с")
    try:
        for phrase in PHRASES:
            CALLS.clear()
            t = time.time()
            res = await stand.hear(phrase, chat.CONSOLE_VOICE, source=chat.SOURCE_VOICE)
            total = time.time() - t
            llm = sum(c["s"] for c in CALLS)
            print(f"«{phrase}» ход {total:.2f} с = LLM {llm:.2f} с + ядро {total - llm:.2f} с; "
                  f"запросов {len(CALLS)}; сказал {res.said}")
            for c in CALLS:
                print(f"   {c['s']:.2f} с {c['model']} prompt={c['prompt']} cache_hit={c['cache_hit']} "
                      f"out={c['out']} символов: сообщения {c['chars']}, тулы {c['tools_chars']}")
    finally:
        stand.close()


asyncio.run(main())
