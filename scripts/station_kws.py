"""Живые примеры для своей модели вейка (``training/kws``): что колонка сохраняет и как узнаёт промахи облака.

09.11 на ``10.1.1.13`` сбор стоял с 08.11 09:37: общий кап 10 МБ съели «чужие» фразы (264 файла против 14 «вейков»),
а попытки, где облачный STT «Игорька» не разобрал (пустой текст, «Горек, …», «И говорю: …»), не сохранялись вовсе —
хотя именно на них модель и должна учиться: человек звал по нескольку раз.

Теперь:

* ``pos`` / ``neg`` / ``miss`` — у каждого свой потолок (``RBX_KWS_CAP_POS_MB`` 5, ``…_NEG_MB`` 10, ``…_MISS_MB`` 4);
  «чужие» больше не забирают место у вейка. Файлы не удаляются никогда — забирает их ``pull_real.sh``.
* ``miss`` — вероятный промах: фраза без вейка (или пустая), за которой в ``MISS_WINDOW_S`` секунд пришло обращение
  с вейком; или пустой текст, а своя модель слышит «Игорька» (``igorek`` ≥ ``MISS_KWS_P``). Их размечает человек:
  это сырьё, в обучение напрямую не идёт.
* ``index.jsonl`` — строка на файл: вид, текст STT, ответ своей модели, провайдер STT — чтобы размечать не вслепую.
* На ``/data`` всегда остаётся ``RBX_KWS_FREE_MB`` (8) свободных: обновлению и офлайн-фразам нужно место.
"""
from __future__ import annotations

import io
import json
import os
import time
import wave
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Optional

RATE = 16000
MISS_WINDOW_S = 15.0
MISS_KWS_P = 0.5
WAKE_LABEL = "igorek"


def _mb(name: str, default: float) -> int:
    try:
        return int(float(os.environ.get(name, str(default))) * 2**20)
    except ValueError:
        return int(default * 2**20)


def wav_bytes(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def words_of(text: str) -> list:
    return [w.strip(",.!?;:«»\"'—–-").lower() for w in (text or "").split() if w.strip(",.!?;:«»\"'—–-")]


def wake_position(text: str, wake_words: Iterable[str]) -> str:
    """``start`` — вейк первым словом, ``inside`` — где-то дальше, ``none`` — нет, ``empty`` — текста нет."""
    words = words_of(text)
    if not words:
        return "empty"
    wake = set(wake_words)
    if words[0] in wake:
        return "start"
    return "inside" if set(words) & wake else "none"


class KwsCollector:
    """Копит секунды фраз по видам; ``observe`` — на каждую фразу после STT, ``promote_misses`` — на обращение."""

    def __init__(self, folder: Path, log: Callable[[str], None], free_path: Optional[Path] = None) -> None:
        self.folder, self.log = Path(folder), log
        self.free_path = Path(free_path) if free_path else self.folder.parent
        self.caps = {"pos": _mb("RBX_KWS_CAP_POS_MB", 5), "neg": _mb("RBX_KWS_CAP_NEG_MB", 10),
                     "miss": _mb("RBX_KWS_CAP_MISS_MB", 4)}
        self.min_free = _mb("RBX_KWS_FREE_MB", 8)
        self.recent: deque = deque(maxlen=12)  # (время, pcm, текст, метка, p, провайдер) фраз без вейка, ждут окно промаха
        self._used: dict = {}
        self._full_logged: set = set()

    # ------------------------------------------------------------------ место

    def used(self, kind: str) -> int:
        if kind not in self._used:
            path = self.folder / kind
            self._used[kind] = sum(f.stat().st_size for f in path.glob("*.wav")) if path.exists() else 0
        return self._used[kind]

    def _free(self) -> int:
        try:
            st = os.statvfs(self.free_path)
            return st.f_bavail * st.f_frsize
        except (OSError, AttributeError):  # Windows (тесты) — не ограничиваем
            return 1 << 40

    def _room(self, kind: str, size: int) -> bool:
        if self.used(kind) + size > self.caps[kind]:
            reason = f"потолок {self.caps[kind] // 2**20} МБ"
        elif self._free() - size < self.min_free:
            reason = f"на диске меньше {self.min_free // 2**20} МБ"
        else:
            return True
        if kind not in self._full_logged:  # один раз за запуск, а не на каждую фразу
            self._full_logged.add(kind)
            self.log(f"🔑 kws: {kind} больше не копится — {reason}; забрать: training/kws/pull_real.sh")
        return False

    # ------------------------------------------------------------------ запись

    def save(self, kind: str, chunk: bytes, meta: dict) -> str:
        data = wav_bytes(chunk)
        if not self._room(kind, len(data)):
            return ""
        try:
            (self.folder / kind).mkdir(parents=True, exist_ok=True)
            name = f"real__{int(time.time() * 1000)}.wav"
            (self.folder / kind / name).write_bytes(data)
            self._used[kind] = self.used(kind) + len(data)
            with open(self.folder / "index.jsonl", "a", encoding="utf-8") as idx:
                idx.write(json.dumps({"file": f"{kind}/{name}", "kind": kind, "at": round(time.time(), 1), **meta},
                                     ensure_ascii=False) + "\n")
            return name
        except OSError as exc:
            self.log(f"kws: пример не сохранён: {exc}")
            return ""

    @staticmethod
    def _head(pcm: bytes, seconds: float = 2.0) -> bytes:
        """Начало фразы — там вейк (2 с: «Игорёк» с паузой перед ним длиннее секунды)."""
        return pcm[: int(RATE * seconds) * 2]

    @staticmethod
    def _middle(pcm: bytes, seconds: float = 1.2) -> bytes:
        sec = int(RATE * seconds) * 2
        start = max(0, (len(pcm) - sec) // 2 // 2 * 2)
        return pcm[start:start + sec]

    def observe(self, pcm: bytes, text: str, wake_words: Iterable[str], label: str = "", prob: float = 0.0,
                provider: str = "", now: Optional[float] = None) -> str:
        """Фраза после STT → ``pos``/``miss``/``pending`` (ждёт окно промаха)/пусто. ``neg`` пишет :meth:`settle`."""
        now = time.time() if now is None else now
        self.settle(now)
        where = wake_position(text, wake_words)
        meta = {"text": text, "kws": label, "p": round(prob, 3), "stt": provider}
        if where == "start":
            return "pos" if self.save("pos", self._head(pcm, 1.2), meta) else ""
        if where == "inside":
            return ""  # где вейк — без меток времени STT не знаем
        if where == "empty" and label == WAKE_LABEL and prob >= MISS_KWS_P:
            return "miss" if self.save("miss", self._head(pcm), {**meta, "why": "stt_empty_kws_wake"}) else ""
        # «чужое» ждёт окно промаха: пришло обращение — это была попытка позвать (miss), не пришло — neg
        self.recent.append((now, pcm, text, label, prob, provider))
        return "pending"

    def settle(self, now: Optional[float] = None, window: float = MISS_WINDOW_S) -> None:
        """Фразы старше окна промаха без обращения следом — «чужое» (``neg``); пустые — забываются."""
        now = time.time() if now is None else now
        while self.recent and now - self.recent[0][0] > window:
            _t, pcm, text, label, prob, provider = self.recent.popleft()
            if words_of(text):
                self.save("neg", self._middle(pcm), {"text": text, "kws": label, "p": round(prob, 3),
                                                     "stt": provider})

    def promote_misses(self, now: Optional[float] = None, window: float = MISS_WINDOW_S) -> int:
        """Пришло обращение с вейком: фразы без вейка за ``window`` секунд до него — вероятные промахи облака.

        Возвращает, сколько было попыток до удачной (для лога «вейк с N-й попытки»).
        """
        now = time.time() if now is None else now
        self.settle(now, window)
        tries = [r for r in self.recent if 0 <= now - r[0] <= window]
        self.recent.clear()
        for t, pcm, text, label, prob, provider in tries:
            self.save("miss", self._head(pcm), {"text": text, "kws": label, "p": round(prob, 3), "stt": provider,
                                                "why": "before_wake", "gap_s": round(now - t, 1)})
        return len(tries)
