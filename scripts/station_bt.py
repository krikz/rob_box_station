"""Bluetooth-музыка колонки: сопряжение с телефоном и пульт плеера (AVRCP) — команды решает код, не модель (ADR-0148).

На колонке от Алисы уже есть: ``bluealsa -p a2dp-sink`` (колонка — Bluetooth-динамик), ``bluealsa-aplay`` (звук
телефона → ``default``: ``dmix``, речь колонки смешивается с музыкой), ``just-works-bluez-agent`` (сопряжение без кода).
Здесь — видимость для телефона на время и управление плеером телефона через BlueZ ``org.bluez.MediaPlayer1``.

Без ``from __future__ import annotations``: dbus-next читает сигнатуры из аннотаций (в этом модуле их нет, но правило
одно с ``station_setup``).
"""
import asyncio
import re
from typing import Callable, Optional

PAIR_S = 180
SHORT_WORDS = 3
ALIAS = "Игорёк"

#: Команды колонке (после вейка) → действие. Порядок важен: первое совпадение.
COMMANDS = [
    ("pair", re.compile(r"\b(включи|подключи|найди)\w*\s+(блют[уа][сз]|bluetooth|телефон)|\bсопряжени")),
    ("next", re.compile(r"\b(следующ\w*|дальше|переключи\w*|другой\s+трек|другую\s+песню)\b")),
    ("previous", re.compile(r"\b(предыдущ\w*|прошл\w*\s+(трек|песн\w*)|назад)\b")),
    ("pause", re.compile(r"\b(пауза|поставь\s+на\s+паузу|останови\s+музыку|выключи\s+музыку)\b")),
    ("play", re.compile(r"\b(продолж\w*|сними\s+с\s+паузы|включи\s+музыку|играй)\b")),
]


def match_command(text: str) -> Optional[str]:
    low = text.lower().replace("ё", "е")
    for action, rx in COMMANDS:
        if rx.search(low):
            return action
    return None


class BtMedia:
    """Сопряжение и пульт плеера телефона через BlueZ (D-Bus); ``say`` — фраза колонки (строит код)."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self._bus = None

    async def _connect(self):
        if self._bus is None:
            from dbus_next import BusType  # noqa: PLC0415
            from dbus_next.aio import MessageBus  # noqa: PLC0415

            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return self._bus

    async def _props(self, path: str, iface: str):
        bus = await self._connect()
        intro = await bus.introspect("org.bluez", path)
        return bus.get_proxy_object("org.bluez", path, intro).get_interface("org.freedesktop.DBus.Properties"), iface

    async def _set(self, name: str, sig: str, value) -> None:
        from dbus_next import Variant  # noqa: PLC0415

        props, iface = await self._props("/org/bluez/hci0", "org.bluez.Adapter1")
        await props.call_set(iface, name, Variant(sig, value))

    async def watch_connections(self, on_change: Callable[[str, bool], None]) -> None:
        """Телефон подключился / отключился (``org.bluez.Device1.Connected``) → ``on_change(имя, подключён)``.

        Шифу 09.11: «когда подключаемся к Игорьку по блютузу, логично издать приятный звук». Отпускание Hands-Free
        во время звонка (``station_hfp``) соединение не рвёт — музыка остаётся, звука не будет.
        """
        from dbus_next import Message, MessageType  # noqa: PLC0415

        bus = await self._connect()
        rule = ("type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
                "member='PropertiesChanged',arg0='org.bluez.Device1'")
        await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                               interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[rule]))
        last: dict = {}

        def handler(msg) -> None:
            if (msg.message_type != MessageType.SIGNAL or msg.member != "PropertiesChanged" or not msg.body
                    or msg.body[0] != "org.bluez.Device1" or "Connected" not in msg.body[1]):
                return
            up = bool(msg.body[1]["Connected"].value)
            if last.get(msg.path) == up:
                return
            last[msg.path] = up
            asyncio.ensure_future(self._report(msg.path, up, on_change))

        bus.add_message_handler(handler)
        self.log("🎵 Bluetooth: слежу за подключением телефонов")

    async def _report(self, path: str, up: bool, on_change: Callable[[str, bool], None]) -> None:
        name = path.rsplit("/", 1)[-1]
        try:
            props, _ = await self._props(path, "org.bluez.Device1")
            name = (await props.call_get("org.bluez.Device1", "Alias")).value or name
        except Exception:  # noqa: BLE001 — устройство уже ушло: хватит адреса
            pass
        self.log(f"🎵 Bluetooth: {name} {'подключился' if up else 'отключился'}")
        on_change(name, up)

    async def ensure_alias(self) -> None:
        """Имя колонки для телефонов — при каждом старте хоста: BlueZ хранит его в ОЗУ, и 09.11 после перезагрузки
        колонка снова называлась «Яндекс Лайт», хотя телефон Шифу знал её как «Игорёк»."""
        try:
            props, iface = await self._props("/org/bluez/hci0", "org.bluez.Adapter1")
            current = (await props.call_get(iface, "Alias")).value
            if current != ALIAS:
                await self._set("Alias", "s", ALIAS)
                self.log(f"🎵 Bluetooth: имя «{current}» → «{ALIAS}»")
        except Exception as exc:  # noqa: BLE001 — без BlueZ колонка всё равно работает
            self.log(f"🎵 Bluetooth: имя не поставлено: {type(exc).__name__}: {exc}")

    async def pair(self) -> str:
        """Колонка видна телефону ``PAIR_S`` секунд и принимает сопряжение (агент «just works» — без кода)."""
        await self._set("Alias", "s", ALIAS)
        await self._set("PairableTimeout", "u", PAIR_S)
        await self._set("DiscoverableTimeout", "u", PAIR_S)
        await self._set("Pairable", "b", True)
        await self._set("Discoverable", "b", True)
        self.log(f"🎵 Bluetooth: видна телефонам {PAIR_S} с как «{ALIAS}»")
        return f"Блютуз включён. Найди в телефоне «{ALIAS}» и подключись, у тебя три минуты."

    async def player(self) -> Optional[str]:
        """Путь плеера подключённого телефона (``…/dev_XX/playerN``) или ``None``."""
        bus = await self._connect()
        intro = await bus.introspect("org.bluez", "/")
        om = bus.get_proxy_object("org.bluez", "/", intro).get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await om.call_get_managed_objects()
        for path, ifaces in objects.items():
            if "org.bluez.MediaPlayer1" in ifaces:
                return path
        return None

    async def control(self, action: str) -> str:
        path = await self.player()
        if path is None:
            return "Телефон с музыкой не подключён. Скажи «включи блютуз» и подключи телефон."
        bus = await self._connect()
        intro = await bus.introspect("org.bluez", path)
        mp = bus.get_proxy_object("org.bluez", path, intro).get_interface("org.bluez.MediaPlayer1")
        await getattr(mp, f"call_{action}")()
        self.log(f"🎵 плеер телефона: {action}")
        return {"next": "Следующий.", "previous": "Предыдущий.", "pause": "Пауза.", "play": "Продолжаю."}[action]

    async def handle(self, text: str) -> Optional[str]:
        """Команда музыки во фразе (вейк уже снят) → фраза колонке; не команда — ``None`` (дальше решает ход)."""
        action = match_command(text)
        if action is None:
            return None
        try:
            if action == "pair":
                return await self.pair()
            # «дальше», «назад» бывают и в разговоре: пульт — только короткой фразой и при подключённом телефоне
            if len(text.split()) > SHORT_WORDS or await self.player() is None:
                return None
            return await self.control(action)
        except Exception as exc:  # noqa: BLE001 — сбой Bluetooth не роняет хост
            self.log(f"🎵 Bluetooth: {type(exc).__name__}: {exc}")
            return "С блютузом не вышло, попробуй ещё раз."


async def _selftest() -> None:  # pragma: no cover — ручная проверка на колонке
    bt = BtMedia(print)
    print(await bt.player())


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_selftest())
