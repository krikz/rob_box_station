"""Bluetooth-музыка (хендофф 09.11): пульт и сопряжение решает код, не модель.

Живого BlueZ здесь нет — проверяется разбор фразы, отказ длинной фразы без телефона и то, что хост
при старте зовёт постановку имени «Игорёк» (имя слетает с перезагрузкой колонки).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_bt as bt  # noqa: E402
import station_host as sh  # noqa: E402


def test_match_pair_play_and_pause():
    assert bt.match_command("включи блютуз") == "pair"
    assert bt.match_command("подключи телефон") == "pair"
    assert bt.match_command("найди bluetooth") == "pair"
    assert bt.match_command("поставь на паузу") == "pause"
    assert bt.match_command("выключи музыку") == "pause"
    assert bt.match_command("продолжи") == "play"
    assert bt.match_command("включи музыку") == "play"


def test_match_next_and_previous():
    assert bt.match_command("следующий") == "next"
    assert bt.match_command("дальше") == "next"
    assert bt.match_command("другой трек") == "next"
    assert bt.match_command("предыдущий") == "previous"
    assert bt.match_command("назад") == "previous"


def test_talk_is_not_a_player_command():
    assert bt.match_command("который час") is None
    assert bt.match_command("расскажи анекдот") is None
    assert bt.match_command("") is None


class _Bt(bt.BtMedia):
    """Без D-Bus: плеер есть или нет, ``control``/``pair`` пишут действие."""

    def __init__(self, has_player: bool = True) -> None:
        super().__init__(log=lambda _msg: None)
        self._has_player = has_player
        self.actions = []

    async def player(self):
        return "/org/bluez/hci0/dev_00/player0" if self._has_player else None

    async def control(self, action: str) -> str:
        self.actions.append(action)
        return {"next": "Следующий.", "previous": "Предыдущий.", "pause": "Пауза.", "play": "Продолжаю."}[action]

    async def pair(self) -> str:
        self.actions.append("pair")
        return "paired"


def test_long_talk_is_not_a_remote_even_if_a_word_matches():
    """«дальше» в длинной фразе — разговор, не пульт (граница в ``handle``, не в разборе)."""
    media = _Bt()
    assert bt.match_command("а дальше что было в этой истории") == "next"
    assert asyncio.run(media.handle("а дальше что было в этой истории")) is None
    assert media.actions == []
    assert asyncio.run(media.handle("дальше")) == "Следующий."
    assert media.actions == ["next"]


def test_short_command_without_phone_does_not_pretend_to_play():
    media = _Bt(has_player=False)
    assert asyncio.run(media.handle("дальше")) is None
    assert asyncio.run(media.handle("пауза")) is None
    assert media.actions == []
    assert asyncio.run(media.handle("включи блютуз")) == "paired"
    assert media.actions == ["pair"]


def test_yo_is_folded_before_match():
    assert bt.match_command("Включи Блютуз.") == "pair"
    assert bt.match_command("следующая песня") == "next"


def test_alias_is_igorek_and_host_sets_it_on_start():
    """09.11 после перезагрузки колонка снова звалась «Яндекс Лайт» — имя ставится при каждом старте."""
    assert bt.ALIAS == "Игорёк"
    source = Path(sh.__file__).read_text(encoding="utf-8")
    start = source.index("async def serve")
    end = source.index("\nasync def ", start + 1)
    body = source[start:end]
    assert "ensure_alias()" in body
    assert "watch_connections" in body
