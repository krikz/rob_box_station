"""Хост колонки: повтор на 429 от STT и кольцо не залипает в «думаю».

08.11 два бага, найденные на живой второй колонке:
1. при одновременной речи двух колонок на одном ключе MiniMax одна получала HTTP 429, хост сдавался и
   молчал 20 с — теперь несколько попыток с паузой;
2. адресованный ход с пустой репликой (`ход 0.05 с: []`) оставлял кольцо в `think` навсегда: в очередь
   озвучки ничего не попадало, `_play` не просыпался и `ring.idle()` не вызывался.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_host as sh  # noqa: E402


class MiniMaxSTTError(Exception):
    """Подмена исключений провайдера: пакета `rob_box_voice` в окружении тестов нет."""


class MiniMaxSTTRateLimitError(MiniMaxSTTError):
    """HTTP 429."""


@pytest.fixture
def stt_module(monkeypatch):
    fake = types.ModuleType("rob_box_voice.stt_providers.minimax_provider")
    fake.MiniMaxSTTError = MiniMaxSTTError
    fake.MiniMaxSTTRateLimitError = MiniMaxSTTRateLimitError
    monkeypatch.setitem(sys.modules, "rob_box_voice.stt_providers.minimax_provider", fake)
    monkeypatch.setattr(sh.time, "sleep", lambda _s: None)  # паузы повтора в тесте не ждём
    return fake


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _Stt:
    def __init__(self, fails: int, text: str = "Игорёк, ты здесь?") -> None:
        self.fails, self.text, self.calls = fails, text, 0

    def transcribe(self, _wav: bytes) -> _Response:
        self.calls += 1
        if self.calls <= self.fails:
            raise MiniMaxSTTRateLimitError("minimax STT: rate-limited (HTTP 429)")
        return _Response(self.text)


def _voice(fails: int, text: str = "Игорёк, ты здесь?") -> tuple:
    voice = object.__new__(sh.Voice)
    voice.stt, voice.limited = _Stt(fails, text), False
    return voice, voice.stt


def test_429_retried_and_text_arrives(stt_module):
    """Один-два 429 подряд — не приговор: текст доходит, паузы 20 с нет."""
    voice, stt = _voice(fails=2)

    assert voice.hear(b"\x00\x00" * 160) == "Игорёк, ты здесь?"
    assert stt.calls == 3
    assert voice.stt_limited() is False


def test_429_exhausted_marks_limited(stt_module):
    """Отбились все попытки — только тогда лимит, и хост уходит в паузу."""
    voice, stt = _voice(fails=99)

    assert voice.hear(b"\x00\x00" * 160) == ""
    assert stt.calls == sh.STT_429_TRIES
    assert voice.stt_limited() is True


def test_other_stt_error_not_retried(stt_module):
    voice, stt = _voice(fails=0)

    def boom(_wav):
        stt.calls += 1
        raise MiniMaxSTTError("сеть легла")

    stt.transcribe = boom
    assert voice.hear(b"\x00\x00" * 160) == ""
    assert stt.calls == 1
    assert voice.stt_limited() is False


class _Ring:
    def __init__(self) -> None:
        self.states: list = []

    def set(self, state: str) -> None:
        self.states.append(state)

    def idle(self) -> None:
        self.states.append("idle")


def test_drain_idles_ring_without_speech():
    """Ход без реплики: очередь пуста — кольцо возвращается в покой, а не висит в `think`."""
    live = object.__new__(sh.LiveOut)
    live._ring, live._queue = _Ring(), asyncio.Queue()

    asyncio.run(live.drain())

    assert live._ring.states == ["idle"]


class _Result:
    def __init__(self, addressed: bool, said: list) -> None:
        self.addressed, self.said = addressed, said


def test_wordless_turn_keeps_ring_listening():
    """Голое «Игорёк» (ход без слов): кольцо показывает «слушаю», а не гаснет молча."""
    ring = _Ring()

    async def run() -> bool:
        return sh.wordless_tail(ring, _Result(addressed=True, said=[]))

    assert asyncio.run(run()) is True
    assert ring.states == ["listen"]


def test_turn_with_speech_does_not_touch_ring():
    """Обычный ответ кольцом управляет озвучка, а не этот хелпер."""
    ring = _Ring()

    async def run() -> bool:
        return sh.wordless_tail(ring, _Result(addressed=True, said=["У меня всё отлично."]))

    assert asyncio.run(run()) is False
    assert ring.states == []


def test_kws_wake_shadow_by_default(monkeypatch):
    """По умолчанию модель только пишет в лог: уверенный вейк не решает за облако."""
    monkeypatch.delenv("RBX_KWS_DECIDE", raising=False)

    assert sh.kws_wake("igorek", 0.99) is False


def test_kws_wake_respects_threshold(monkeypatch):
    monkeypatch.setenv("RBX_KWS_DECIDE", "1")
    monkeypatch.setenv("RBX_KWS_THRESHOLD", "0.8")

    assert sh.kws_wake("igorek", 0.81) is True
    assert sh.kws_wake("igorek", 0.79) is False
    assert sh.kws_wake("_unknown_", 0.99) is False


def test_kws_wake_text_prefixes_missing_wake(monkeypatch):
    """STT не разобрал вейк, модель уверена — код подставляет вейк, иначе ядро скажет no_wake."""
    monkeypatch.setenv("RBX_KWS_DECIDE", "1")
    wake = sh.WAKE_WORDS[0]

    assert sh.kws_wake_text("Горек, который час?", "igorek", 0.95) == f"{wake}, Горек, который час?"
    assert sh.kws_wake_text("который час?", "igorek", 0.95) == f"{wake}, который час?"


def test_kws_wake_text_keeps_existing_wake(monkeypatch):
    """Вейк уже в тексте — не дублируем; режим тени — текст не трогаем."""
    monkeypatch.setenv("RBX_KWS_DECIDE", "1")
    text = f"{sh.WAKE_WORDS[0]}, как дела"

    assert sh.kws_wake_text(text, "igorek", 0.95) == text
    monkeypatch.setenv("RBX_KWS_DECIDE", "0")
    assert sh.kws_wake_text("который час?", "igorek", 0.95) == "который час?"
