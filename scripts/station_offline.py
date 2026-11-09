"""Офлайн-фразы колонки: синтезируются заранее, пока сеть есть, и звучат, когда облака нет (Шифу 09.11).

Без них колонка при сбое сети, STT или модели просто молчала, и было непонятно, работает она или нет. Фразы
синтезирует тот же MiniMax тем же голосом, что говорит колонка, и кладёт в ``BASE/offline/<ключ>.raw`` (s16le 24 кГц
моно); ``index.json`` помнит текст и голос каждого файла — сменился голос или текст, фраза пересинтезируется в фоне,
старая звучит до замены. Каталог не в ``assets/``: его не трогает обновление.

Тоны (:func:`tone`) считает код — они звучат даже на чистой колонке, где ни одной фразы ещё нет.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

RATE = 24000

#: Ключ → текст. Короткие: звучат, когда что-то не так, и не должны мешать.
PHRASES: Dict[str, str] = {
    "started": "Игорёк на связи, слушаю.",
    "heard": "Слушаю.",
    "not_heard": "Не расслышал, повтори, пожалуйста.",
    "no_net": "Нет интернета. Проверь Wi-Fi, а я подожду.",
    "stt_fail": "Не могу разобрать речь: распознавание не отвечает. Попробуй ещё раз.",
    "stt_limited": "Распознавание перегружено, повтори через пару секунд.",
    "llm_fail": "Не смог придумать ответ: модель не отвечает. Попробуй ещё раз.",
    "tts_fail": "Не получается говорить: синтез речи не отвечает.",
    "mic_off": "Микрофон выключен.",
    "mic_on": "Микрофон включён.",
    "restarting": "Перезапускаюсь, через минуту вернусь.",
}


def tone(kind: str) -> bytes:
    """Тон кодом (s16le 24 кГц): ``error`` — два нисходящих, ``ok`` — два восходящих, ``tick`` — короткий."""
    seq = {"error": ((660, 0.12), (0, 0.04), (440, 0.18)), "ok": ((520, 0.09), (0, 0.03), (780, 0.12)),
           "tick": ((880, 0.05),)}.get(kind, ((880, 0.05),))
    out = bytearray()
    for freq, sec in seq:
        n = int(RATE * sec)
        for i in range(n):
            if not freq:
                out += b"\x00\x00"
                continue
            env = min(1.0, i / (RATE * 0.008), (n - i) / (RATE * 0.02))  # без щелчков на краях
            out += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / RATE)))
    return bytes(out)


def _digest(text: str, voice: str) -> str:
    return hashlib.sha256(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:16]


class OfflinePhrases:
    """Кэш заранее синтезированных фраз: ``pcm(key)`` — звук или ``None``; ``refresh`` — досинтезировать недостающее."""

    def __init__(self, folder: Path, log: Callable[[str], None], phrases: Optional[Dict[str, str]] = None) -> None:
        self.folder, self.log = Path(folder), log
        self.phrases = dict(PHRASES if phrases is None else phrases)

    def _index(self) -> dict:
        try:
            return json.loads((self.folder / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_index(self, index: dict) -> None:
        tmp = self.folder / "index.json.tmp"
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.folder / "index.json")

    def text(self, key: str) -> str:
        return self.phrases.get(key, "")

    def key_for(self, text: str) -> str:
        """Ключ фразы по тексту (фраза кода совпала с офлайн-фразой) или пусто."""
        norm = " ".join((text or "").split())
        return next((k for k, v in self.phrases.items() if v == norm), "")

    def pcm(self, key: str) -> Optional[bytes]:
        """Звук фразы любым голосом, что есть (старый голос лучше тишины); нет файла — ``None``."""
        try:
            data = (self.folder / f"{key}.raw").read_bytes()
        except OSError:
            return None
        return data or None

    def fresh(self, key: str, voice: str) -> bool:
        entry = self._index().get(key) or {}
        return entry.get("sha") == _digest(self.phrases.get(key, ""), voice) and (self.folder / f"{key}.raw").exists()

    def missing(self, voice: str) -> list:
        return [k for k in self.phrases if not self.fresh(k, voice)]

    async def refresh(self, synth: Callable[[str, str], Awaitable[bytes]], voice: str) -> int:
        """Досинтезировать фразы, которых нет или которые другим голосом/текстом. Возвращает, сколько сделано.

        Сбой синтеза (сети нет) — не ошибка: останется старый файл, попытка — при следующем старте или смене голоса.
        """
        todo = self.missing(voice)
        if not todo:
            return 0
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log(f"📦 офлайн-фразы: нет каталога {self.folder}: {exc}")
            return 0
        done = 0
        for key in todo:
            text = self.phrases[key]
            try:
                pcm = await synth(text, voice)
            except Exception as exc:  # noqa: BLE001 — сети нет, ключа нет: просто позже
                self.log(f"📦 офлайн-фраза «{key}» не синтезирована: {type(exc).__name__}")
                break
            if not pcm:
                continue
            tmp = self.folder / f"{key}.raw.tmp"
            try:
                tmp.write_bytes(pcm)
                os.replace(tmp, self.folder / f"{key}.raw")
                index = self._index()
                index[key] = {"sha": _digest(text, voice), "voice": voice, "text": text, "bytes": len(pcm)}
                self._save_index(index)
                done += 1
            except OSError as exc:
                self.log(f"📦 офлайн-фраза «{key}» не записана: {exc}")
                break
        self.log(f"📦 офлайн-фразы: синтезировано {done} из {len(todo)} (голос {voice or 'по умолчанию'})")
        return done
