"""Инъекция фразы в хост колонки через fifo (#5027).

Живой микрофон держит ``arecord`` хоста эксклюзивно, и штатно отпустить его было нечем: ``muted`` только
выбрасывает кадры. ``RBX_MIC_FIFO`` — явный вход: хост отпускает свой ``arecord`` и кормит строку из fifo
в ту же ветку, что ``heard.text`` после STT. По умолчанию переменная пуста — живой хост не меняется.
"""

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_host as sh  # noqa: E402


class _Arecord:
    """Мок своего ``arecord``: настоящего не запускаем."""

    def __init__(self) -> None:
        self.signals: list = []
        self.alive = True
        self.stdout = self
        self.closed = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self) -> None:
        self.signals.append("TERM")
        self.alive = False

    def kill(self) -> None:
        self.signals.append("KILL")
        self.alive = False

    def wait(self, timeout=None) -> int:
        return 0

    def read(self, n: int) -> bytes:
        return b"\x00" * n

    def close(self) -> None:
        self.closed = True


def _mic(monkeypatch) -> "tuple[sh.Mic, list]":
    """``Mic`` с подменённым ``Popen``: поток качалки читает нули, настоящего ``arecord`` нет."""
    spawned: list = []

    def popen(cmd, **kw):
        proc = _Arecord()
        proc.cmd = cmd
        spawned.append(proc)
        return proc

    monkeypatch.setattr(sh.subprocess, "Popen", popen)
    mic = sh.Mic()
    return mic, spawned


def test_fifo_off_by_default(monkeypatch):
    """Без ``RBX_MIC_FIFO`` входа нет: путь пуст, fifo не создаётся, отпустить микрофон нельзя."""
    monkeypatch.delenv("RBX_MIC_FIFO", raising=False)

    assert sh.mic_fifo_path() is None
    assert sh.open_mic_fifo() is None


def test_fifo_rejects_path_outside_tmp(monkeypatch, tmp_path):
    """Путь не в ``/tmp`` — переменная задана, но игнорируется: fifo где попало не создаём."""
    monkeypatch.setenv("RBX_MIC_FIFO", str(tmp_path / "phrases"))

    assert sh.mic_fifo_path() is None


def test_released_refused_without_fifo(monkeypatch):
    """Без явного входа микрофон не отпускается: случайный флаг живой хост не трогает."""
    monkeypatch.delenv("RBX_MIC_FIFO", raising=False)
    mic, spawned = _mic(monkeypatch)

    with pytest.raises(RuntimeError):
        mic.released = True

    assert spawned[0].signals == [] and spawned[0].alive
    mic.proc.kill()


def test_release_sigterms_own_arecord_and_reopens(monkeypatch):
    """``released`` шлёт SIGTERM своему ``arecord`` и закрывает его stdout; снятие поднимает тот же ``Popen``."""
    monkeypatch.setenv("RBX_MIC_FIFO", "/tmp/rbx_mic_test_release")
    mic, spawned = _mic(monkeypatch)
    first = spawned[0]

    mic.released = True

    assert mic.released is True
    assert first.signals == ["TERM"] and not first.alive and first.closed
    assert mic.proc is None

    mic.released = False

    assert mic.released is False
    assert len(spawned) == 2 and spawned[1] is mic.proc and spawned[1].alive
    assert spawned[1].cmd[0] == "arecord" and MIC_DEVICE_OK(spawned[1].cmd)
    mic.proc.kill()


def MIC_DEVICE_OK(cmd: list) -> bool:
    return cmd[cmd.index("-D") + 1] == sh.MIC_DEVICE


def test_release_is_idempotent(monkeypatch):
    """Повторная установка того же флага второй ``arecord`` не трогает."""
    monkeypatch.setenv("RBX_MIC_FIFO", "/tmp/rbx_mic_test_idem")
    mic, spawned = _mic(monkeypatch)

    mic.released = True
    mic.released = True

    assert len(spawned) == 1 and spawned[0].signals == ["TERM"]


def _fifo_with(line: str) -> "tuple[int, Path]":
    """Fifo в /tmp с уже записанной строкой: чтение не блокируется."""
    path = Path("/tmp/rbx_mic_test_branch")
    if path.exists():
        path.unlink()
    os.mkfifo(path, 0o600)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    wr = os.open(path, os.O_WRONLY)
    os.write(wr, line.encode())
    os.close(wr)
    return fd, path


def test_fifo_phrase_reaches_heard_text_branch():
    """Фраза из fifo доходит до ``stand.hear`` — та же ветка, что ``heard.text`` после STT."""
    fd, path = _fifo_with("Игорёк, который час?\n")
    heard: list = []

    class _Stand:
        def hear(self, text, voice, source):
            heard.append((text, voice, source))
            return types.SimpleNamespace(addressed=True, said=["десять часов"], reason="")

    chat = types.SimpleNamespace(CONSOLE_VOICE="console", SOURCE_VOICE="voice")
    buttons = types.SimpleNamespace(address=lambda text: text)
    live = types.SimpleNamespace(drain=_async_none(), trouble=_async_none())

    class _Bt:
        async def handle(self, rest):
            return ""

    try:
        got = asyncio.run(sh.fifo_phrase(fd, buttons, live, None, None, _Bt(), None, None, _Stand(), chat))
    finally:
        os.close(fd)
        path.unlink()

    assert got is True
    assert heard == [("Игорёк, который час?", "console", "voice")]


def test_fifo_phrase_absent_does_not_touch_stand():
    """Без фразы (вход выключен или fifo пуст) ход не начинается: поведение прежнее."""
    called: list = []

    class _Stand:
        def hear(self, *a, **k):
            called.append(a)

    assert asyncio.run(sh.fifo_phrase(None, None, None, None, None, None, None, None, _Stand(), None)) is False

    fd, path = _fifo_with("")
    try:
        got = asyncio.run(sh.fifo_phrase(fd, None, None, None, None, None, None, None, _Stand(), None))
    finally:
        os.close(fd)
        path.unlink()

    assert got is False and called == []


def test_fifo_phrase_goes_through_address_and_code_command(monkeypatch):
    """Фраза проходит ``buttons.address``, а код-команда съедает её раньше хода — как ``heard.text``."""
    fd, path = _fifo_with("игорек обновись\n")
    seen: list = []

    async def code_command(text, *a):
        seen.append(text)
        return True

    monkeypatch.setattr(sh, "code_command", code_command)
    buttons = types.SimpleNamespace(address=lambda text: text.upper())

    class _Stand:
        def hear(self, *a, **k):
            raise AssertionError("код-команда должна была съесть фразу до хода")

    try:
        got = asyncio.run(sh.fifo_phrase(fd, buttons, None, None, None, None, None, None, _Stand(), None))
    finally:
        os.close(fd)
        path.unlink()

    assert got is True and seen == ["ИГОРЕК ОБНОВИСЬ"]


def _async_none():
    async def run(*a, **k):
        return None

    return run


def test_read_mic_fifo_skips_empty_and_drops_long():
    """Пустая строка — не фраза. Длиннее предела — отбрасывается целиком, следующая за ней не теряется."""
    path = Path("/tmp/rbx_mic_test_read")
    if path.exists():
        path.unlink()
    os.mkfifo(path, 0o600)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    wr = os.open(path, os.O_WRONLY)
    try:
        os.write(wr, b"\n")
        assert sh.read_mic_fifo(fd) == ""
        os.write(wr, b" \n")
        assert sh.read_mic_fifo(fd) == ""
        os.write(wr, ("я" * (sh.MIC_FIFO_MAX + 1) + "\nдальше\n").encode())
        assert sh.read_mic_fifo(fd) == ""
        assert sh.read_mic_fifo(fd) == "дальше"
    finally:
        os.close(fd)
        os.close(wr)
        path.unlink()


def test_read_mic_fifo_keeps_tail_between_calls():
    """Недописанная строка не теряется: хвост помнится до следующего захода, вторая фраза не съедается."""
    path = Path("/tmp/rbx_mic_test_tail")
    if path.exists():
        path.unlink()
    os.mkfifo(path, 0o600)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    wr = os.open(path, os.O_WRONLY)
    try:
        os.write(wr, "половина".encode())
        assert sh.read_mic_fifo(fd) == ""
        os.write(wr, " строки\nвторая\n".encode())
        assert sh.read_mic_fifo(fd) == "половина строки"
        assert sh.read_mic_fifo(fd) == "вторая"
    finally:
        os.close(fd)
        os.close(wr)
        path.unlink()
