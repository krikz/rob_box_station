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
import random
import struct
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

RATE = 24000

#: Ключ → варианты. Короткие: звучат, когда что-то не так, и не должны мешать. Шифу 09.11: «делай вариации, чтоб
#: было несколько, а не однотипно каждый раз» — колонка берёт случайный вариант, не повторяя прошлый подряд.
#: Нулевой вариант — прежний текст: его файл ``<ключ>.raw`` уже синтезирован и переиспользуется.
PHRASES: Dict[str, List[str]] = {
    "started": ["Игорёк на связи, слушаю.", "Я снова тут. Зовите, если что.", "Игорёк проснулся и готов помогать."],
    "heard": ["Слушаю.", "Да?", "Я весь внимание."],
    "not_heard": ["Не расслышал, повтори, пожалуйста.", "Прости, не разобрал. Ещё раз?",
                  "Что-то не уловил, скажи ещё раз."],
    "no_net": ["Нет интернета. Проверь Wi-Fi, а я подожду.",
               "Пропал интернет, без него я не отвечу. Проверь Wi-Fi.",
               "Связи с интернетом нет — подожду, пока появится."],
    "stt_fail": ["Не могу разобрать речь: распознавание не отвечает. Попробуй ещё раз.",
                 "Распознавание речи молчит. Повтори чуть позже.",
                 "Не получилось распознать — сервис не отвечает. Ещё раз?"],
    "stt_limited": ["Распознавание перегружено, повтори через пару секунд.",
                    "Слишком много запросов к распознаванию. Подожди секунду и повтори."],
    "llm_fail": ["Не смог придумать ответ: модель не отвечает. Попробуй ещё раз.",
                 "Мозги в облаке не отвечают. Спроси ещё раз чуть позже.",
                 "Ответа от модели нет, попробуй повторить."],
    "tts_fail": ["Не получается говорить: синтез речи не отвечает.", "Голос пропал: синтез речи не отвечает."],
    "mic_off": ["Микрофон выключен.", "Не слушаю, микрофон выключен.", "Всё, я ничего не слышу."],
    "mic_on": ["Микрофон включён.", "Снова слушаю.", "Микрофон включён, я тут."],
    "restarting": ["Перезапускаюсь, через минуту вернусь.", "Сейчас перезагружусь, минутку.",
                   "Перезапускаюсь, не теряйте."],
    "bt_on": ["Телефон подключён.", "Телефон на связи.", "Есть контакт с телефоном.",
              "Подключились, можно включать музыку."],
    "bt_off": ["Телефон отключён.", "Телефон отключился.", "Связь с телефоном пропала."],
}


def tone(kind: str) -> bytes:
    """Тон кодом (s16le 24 кГц): ``error`` — два нисходящих, ``ok`` — два восходящих, ``tick`` — короткий,
    ``connect`` / ``disconnect`` — телефон подключился / отключился по Bluetooth."""
    seq = {"error": ((660, 0.12), (0, 0.04), (440, 0.18)), "ok": ((520, 0.09), (0, 0.03), (780, 0.12)),
           "tick": ((880, 0.05),),
           # подключение телефона: мажорное трезвучие вверх (до–ми–соль–до), отключение — две ноты вниз
           "connect": ((523, 0.09), (659, 0.09), (784, 0.09), (1047, 0.22)),
           "disconnect": ((784, 0.12), (0, 0.03), (523, 0.2))}.get(kind, ((880, 0.05),))
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


def _variant_id(key: str, i: int) -> str:
    """Имя варианта: нулевой — сам ключ (файл прежней версии ``<ключ>.raw``), дальше ``<ключ>.<i>``."""
    return key if i == 0 else f"{key}.{i}"


class OfflinePhrases:
    """Кэш заранее синтезированных фраз с вариантами: ``pick`` — какой сказать, ``pcm`` — звук или ``None``,
    ``refresh`` — досинтезировать недостающее."""

    def __init__(self, folder: Path, log: Callable[[str], None], phrases: Optional[Dict[str, object]] = None) -> None:
        self.folder, self.log = Path(folder), log
        source = PHRASES if phrases is None else phrases
        #: ключ → список вариантов (строка — один вариант)
        self.phrases: Dict[str, List[str]] = {k: ([v] if isinstance(v, str) else list(v)) for k, v in source.items()}
        self._last: Dict[str, str] = {}  # ключ → id варианта, сказанного прошлым

    # ------------------------------------------------------------------ варианты

    def variants(self) -> Dict[str, str]:
        """id варианта → текст (все ключи)."""
        return {_variant_id(k, i): t for k, texts in self.phrases.items() for i, t in enumerate(texts)}

    def _ids(self, key: str) -> List[str]:
        return [_variant_id(key, i) for i in range(len(self.phrases.get(key, [])))]

    def pick(self, key: str) -> Tuple[str, str]:
        """(id варианта, текст): случайный из синтезированных, не тот же, что прошлый раз; синтезированных нет —
        любой (тогда прозвучит синтезом или тоном)."""
        ids = self._ids(key)
        if not ids:
            return "", ""
        ready = [v for v in ids if self._file(v).exists()] or ids
        choices = [v for v in ready if v != self._last.get(key)] or ready
        vid = random.choice(choices)
        self._last[key] = vid
        return vid, self.variants()[vid]

    def text(self, key: str) -> str:
        """Первый вариант ключа — для лога."""
        return (self.phrases.get(key) or [""])[0]

    def key_for(self, text: str) -> str:
        """id варианта по тексту (фраза кода совпала с офлайн-фразой) или пусто."""
        norm = " ".join((text or "").split())
        return next((vid for vid, t in self.variants().items() if t == norm), "")

    # ------------------------------------------------------------------ файлы

    def _file(self, vid: str) -> Path:
        return self.folder / f"{vid}.raw"

    def _index(self) -> dict:
        try:
            return json.loads((self.folder / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_index(self, index: dict) -> None:
        tmp = self.folder / "index.json.tmp"
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.folder / "index.json")

    def pcm(self, key_or_vid: str) -> Optional[bytes]:
        """Звук варианта (``started.2``) или случайного готового варианта ключа (``started``); старый голос лучше
        тишины. Нет ни одного файла — ``None``."""
        if key_or_vid in self.phrases:  # ключ — любой готовый вариант
            ready = [v for v in self._ids(key_or_vid) if self._file(v).exists()]
            if not ready:
                return None
            key_or_vid = random.choice(ready)
        try:
            data = self._file(key_or_vid).read_bytes()
        except OSError:
            return None
        return data or None

    def fresh(self, vid: str, voice: str) -> bool:
        entry = self._index().get(vid) or {}
        return entry.get("sha") == _digest(self.variants().get(vid, ""), voice) and self._file(vid).exists()

    def missing(self, voice: str) -> list:
        return [vid for vid in self.variants() if not self.fresh(vid, voice)]

    async def refresh(self, synth: Callable[[str, str], Awaitable[bytes]], voice: str) -> int:
        """Досинтезировать варианты, которых нет или которые другим голосом/текстом. Возвращает, сколько сделано.

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
        texts, done = self.variants(), 0
        for vid in todo:
            text = texts[vid]
            try:
                pcm = await synth(text, voice)
            except Exception as exc:  # noqa: BLE001 — сети нет, ключа нет: просто позже
                self.log(f"📦 офлайн-фраза «{vid}» не синтезирована: {type(exc).__name__}")
                break
            if not pcm:
                continue
            tmp = self.folder / f"{vid}.raw.tmp"
            try:
                tmp.write_bytes(pcm)
                os.replace(tmp, self._file(vid))
                index = self._index()
                index[vid] = {"sha": _digest(text, voice), "voice": voice, "text": text, "bytes": len(pcm)}
                self._save_index(index)
                done += 1
            except OSError as exc:
                self.log(f"📦 офлайн-фраза «{vid}» не записана: {exc}")
                break
        self.log(f"📦 офлайн-фразы: синтезировано {done} из {len(todo)} (голос {voice or 'по умолчанию'})")
        return done
