"""Колонка — Bluetooth-гарнитура для звонков с телефона (HFP Hands-Free, #16): «набери Кира», «прими», «сбрось».

Шифу 08.11: «нет возможности по Bluetooth звонить как гарнитурой». Разведка 09.11 на 10.1.1.13:

* ``bluealsa`` v4.1.1 Алисы запущен с ``-p hfp-ag`` (колонка — шлюз, как телефон): телефону гарнитура не видна.
  Тот же ``bluealsa`` умеет ``hfp-hf`` — с ним Redmi Note 12S подключается как ``HFP-HF``, появляются PCM
  ``…/hfphf/{sink,source}`` (CVSD 8 кГц) и ``…/rfcomm`` (``org.bluealsa.RFCOMM1.Open() → fd``: AT-канал к телефону —
  ответы и незнакомые ``bluealsa`` строки (``RING``, ``+CLIP``, ``+CPBR``) приходят в этот fd).
* ``hsp-hs`` вместе с ``hfp-hf`` не включать: телефон выбрал HSP, а в нём нет набора номера.
* Книга контактов — ``AT+CPBS="ME"`` / ``AT+CPBR`` (Android: ``AtPhonebook``), нужна галочка «доступ к контактам» в
  настройках Bluetooth телефона (без неё ``+CPBR`` → ``ERROR``). Кодировка — ``AT+CSCS="UTF-8"``.

Решает код (ADR-0148): фразу разбирает :func:`call_command`, контакт ищет :func:`find_contacts`, звонок — AT-команды.
Звук звонка — см. :data:`CALL_AUDIO`: на этом чипе SCO через HCI вешает Bluetooth, поэтому по умолчанию разговор
идёт в телефоне, колонка только управляет звонком.
"""
import asyncio
import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

CHUNK = 25
#: Шлюз Hands-Free на телефоне (``Handsfree Audio Gateway`` в ``bluetoothctl info``).
HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
#: Звук звонка через колонку. ВЫКЛ по умолчанию: 09.11 на 10.1.1.13 (Bluetooth Amlogic W1 по UART) голос звонка
#: в HCI не выходит — SCO принято 0 пакетов, отправка встаёт на 43, и чип намертво зависает (дважды, лечится только
#: перезагрузкой). Отдельной PCM-шины под Bluetooth у звуковой карты нет (TDM — усилитель, PDM — микрофоны).
#: Без звука колонка набирает, принимает и сбрасывает голосом, а разговор идёт в телефоне. ``RBX_HFP_AUDIO=1`` —
#: попытка звука на свой риск (для другой ревизии чипа).
CALL_AUDIO = os.environ.get("RBX_HFP_AUDIO", "0").strip().lower() in ("1", "on", "true", "yes")
#: Без звука через колонку телефон всё равно ведёт разговор «в гарнитуру» — и тишина даже при выборе «телефон»
#: (Шифу 09.11). Поэтому через секунду после начала звонка колонка ОТПУСКАЕТ Hands-Free (``DisconnectProfile``):
#: разговор остаётся в телефоне, музыка (A2DP) — подключена. Обратно Hands-Free — по команде звонка или через
#: ``RELEASE_HOLD_S``; тогда, если звонок ещё идёт (``AT+CLCC``), — снова отпустить.
RELEASE_HOLD_S = float(os.environ.get("RBX_HFP_RELEASE_HOLD_S", "600"))
CALL_LOG = Path(os.environ.get("RBX_CALL_LOG", "/tmp/rbx_call.log"))
#: Во время разговора: раз в столько секунд ``AT+CLCC`` — звонков у телефона нет, значит, разговор кончился, даже
#: если ``+CIEV`` об этом не дошёл (09.11: хост 3 минуты держал микрофон в «разговоре», которого уже не было).
CLCC_EVERY_S = 4.0
PROFILE_ARGS =["-p", "a2dp-sink", "-p", "a2dp-source", "-p", "hfp-hf", "--sbc-quality=high"]
BLUEALSA = "/usr/bin/bluealsa"

#: Команды звонка (после вейка) → действие и остаток (кому звонить).
_DIAL_RE = re.compile(r"(?i)\b(?:набери|позвони|звони|вызови|набрать|позвонить)\s+(?:мне\s+)?(?P<who>.+)$")
_ANSWER_RE = re.compile(r"(?i)\b(?:прими|ответь|возьми\s+трубку|подними\s+трубку|принять)\b")
_HANGUP_RE = re.compile(r"(?i)\b(?:сбрось|положи\s+трубку|отбой|заверши\s+звонок|повесь\s+трубку|отклони|сбросить)\b")
_REDIAL_RE = re.compile(r"(?i)\b(?:перезвони|повтори\s+(?:звонок|номер))\b")
_NUMBER_RE = re.compile(r"^[+\d][\d\s\-()]{4,}$")


def call_command(text: str) -> Tuple[str, str]:
    """Фраза после вейка → (``dial``/``answer``/``hangup``/``redial``/``""``, кому)."""
    text = " ".join((text or "").split()).strip(" .,!?")
    if _HANGUP_RE.search(text):
        return "hangup", ""
    if _ANSWER_RE.search(text):
        return "answer", ""
    if _REDIAL_RE.search(text):
        return "redial", ""
    m = _DIAL_RE.search(text)
    if m:
        who = re.sub(r"(?i)^(?:на\s+номер|номер|контакту?|по\s+номеру)\s+", "", m.group("who")).strip(" .,!?")
        return ("dial", who) if who else ("", "")
    return "", ""


def _stem(word: str) -> str:
    """«Киру», «Кира», «Киром» → «кир»: падеж русского имени срезается грубо (окончание 1–3 буквы)."""
    w = word.lower().replace("ё", "е")
    for end in ("ому", "ему", "ой", "ей", "ом", "ем", "ую", "ю", "у", "е", "а", "я", "ы", "и", "ь"):
        if len(w) - len(end) >= 3 and w.endswith(end):
            return w[: -len(end)]
    return w


def parse_cpbr(line: str) -> Optional[Tuple[str, str]]:
    """``+CPBR: 12,"+79161234567",145,"Кир Петров"`` → (номер, имя)."""
    m = re.match(r'\+CPBR:\s*\d+\s*,\s*"([^"]*)"\s*,\s*\d+\s*,\s*"([^"]*)"', line.strip())
    if not m:
        return None
    # Android дописывает тип номера: «Кир/M», «Мама/H» (09.11 колонка зачитывала «Кир слэш M»)
    return m.group(1), re.sub(r"/[A-Z]$", "", m.group(2).strip())


def find_contacts(who: str, book: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Контакты, чьё имя совпало со словами просьбы по основам («Киру» → «Кир»); нет точных — по началу слова
    («Кир» → «Кирилл»)."""
    want = [_stem(w) for w in re.findall(r"\w+", who) if len(w) >= 2]
    if not want:
        return []
    exact, prefix = [], []
    for number, name in book:
        words = [_stem(w) for w in re.findall(r"\w+", name)]
        if all(any(n == q for n in words) for q in want):
            exact.append((number, name))
        elif all(any(n.startswith(q) for n in words) for q in want):
            prefix.append((number, name))
    seen, out = set(), []
    # точное имя есть — только оно: 09.11 «позвони Киру» переспрашивало «Кир, …Кириченко, Кирилл …»
    for number, name in (exact or prefix):
        key = re.sub(r"\D", "", number)[-10:]
        if key not in seen:
            seen.add(key)
            out.append((number, name))
    return out


def dial_string(number: str) -> str:
    return "ATD" + re.sub(r"[^\d+*#]", "", number) + ";"


class AtChannel:
    """AT-канал к телефону через fd ``RFCOMM1.Open()``: команды с ожиданием ``OK``/``ERROR``, остальное — в ``events``."""

    def __init__(self, fd: int, log: Callable[[str], None]) -> None:
        self.fd, self.log = fd, log
        os.set_blocking(fd, False)
        self._buf = b""
        self._waiters: List[Tuple[asyncio.Future, List[str]]] = []
        self.events: "asyncio.Queue[str]" = asyncio.Queue()
        self._lock = asyncio.Lock()
        asyncio.get_running_loop().add_reader(fd, self._on_read)

    def close(self) -> None:
        try:
            asyncio.get_running_loop().remove_reader(self.fd)
        except (RuntimeError, ValueError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _on_read(self) -> None:
        try:
            data = os.read(self.fd, 8192)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:  # телефон отключился
            self.close()
            self.events.put_nowait("DISCONNECTED")
            return
        self._buf += data
        *lines, self._buf = self._buf.replace(b"\r", b"\n").split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip().lstrip(":")  # bluealsa отдаёт «:OK»
            if line:
                self._line(line)

    def _line(self, line: str) -> None:
        if self._waiters:
            fut, got = self._waiters[0]
            if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
                self._waiters.pop(0)
                if not fut.done():
                    fut.set_result((line == "OK", got))
                return
            if not line.startswith(("RING", "+CLIP", "+CIEV", "NO CARRIER", "BUSY")):
                got.append(line)
                return
        self.events.put_nowait(line)

    async def ask(self, cmd: str, timeout: float = 6.0) -> Tuple[bool, List[str]]:
        async with self._lock:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append((fut, []))
            try:
                os.write(self.fd, (cmd + "\r").encode("utf-8"))
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:  # первым: с 3.11 это встроенный TimeoutError — наследник OSError
                # запоздавший «OK» этой команды не должен закрыть следующую: ловим его сами и выбрасываем
                late = asyncio.get_running_loop().create_future()
                self._waiters = [(late, []) if w[0] is fut else w for w in self._waiters]
                try:
                    await asyncio.wait_for(late, 1.5)
                except asyncio.TimeoutError:
                    self._waiters = [w for w in self._waiters if w[0] is not late]
                return False, ["timeout"]
            except OSError as exc:  # телефон отключился посреди команды
                self._waiters = [w for w in self._waiters if w[0] is not fut]
                return False, [f"io: {exc}"]


class CallAudio:
    """Звук звонка: микрофон колонки (кадры хоста) → SCO телефону, SCO телефона → динамик колонки.

    ``bluealsa-aplay`` Алисы запущен без ``--profile-sco`` и звонок не играет — его ведём сами. ``plug:`` пересчитывает
    16 кГц хоста в 8 кГц CVSD (или 16 кГц mSBC). Открытие PCM само поднимает SCO-канал в ``bluealsa``.
    """

    def __init__(self, device: str, log: Callable[[str], None]) -> None:
        self.device, self.log = device, log
        self.up: Optional[subprocess.Popen] = None  # микрофон → телефону
        self.down: Optional[subprocess.Popen] = None  # телефон → динамик (arecord | aplay)

    def pcm(self) -> str:
        return f"plug:'bluealsa:DEV={self.device},PROFILE=sco'"

    def start(self) -> None:
        if self.up is not None and self.up.poll() is None:
            return
        fmt = ["-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"]
        err = open(CALL_LOG, "ab")  # почему умер звук звонка — в отдельный лог (09.11: «телефон → динамик» умер молча)
        self.up = subprocess.Popen(["aplay", "-q", "-D", self.pcm()] + fmt, stdin=subprocess.PIPE, stderr=err)
        self.down = subprocess.Popen(
            # имя PCM — через shlex.quote: без него оболочка съедала кавычки, и ALSA отвечала
            # «Unknown PCM plug:bluealsa:DEV=…» (09.11, звонок без звука собеседника)
            ["sh", "-c", f"arecord -q -D {shlex.quote(self.pcm())} {' '.join(fmt)} | aplay -q -D default "
                         f"{' '.join(fmt)}"],
            stderr=err, start_new_session=True)  # своя группа: убить и arecord, и aplay
        err.close()
        self.log("📞 звук звонка: микрофон → телефону, телефон → динамик")

    def check(self) -> str:
        """Что из звука звонка умерло раньше времени (пусто — всё живо) + хвост ``CALL_LOG``."""
        dead = [name for name, proc in (("микрофон → телефону", self.up), ("телефон → динамик", self.down))
                if proc is not None and proc.poll() is not None]
        if not dead:
            return ""
        try:
            tail = CALL_LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-2:]
        except OSError:
            tail = []
        return ", ".join(dead) + (": " + " | ".join(tail) if tail else "")

    def feed(self, frame: bytes) -> None:
        if self.up is not None and self.up.poll() is None:
            try:
                self.up.stdin.write(frame)
            except (BrokenPipeError, OSError):
                self.stop()

    def stop(self) -> None:
        if self.up is not None:
            try:
                self.up.kill()
            except OSError:
                pass
        if self.down is not None:
            try:
                os.killpg(self.down.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                pass
        self.up = self.down = None


class Hfp:
    """Звонки через телефон: ``bluealsa`` в роли гарнитуры, AT-канал, книга контактов, звук звонка.

    ``on_ring(кто)`` — входящий; ``on_call(True/False)`` — разговор начался/кончился (хост глушит вейк и VAD).
    """

    def __init__(self, log: Callable[[str], None], on_ring: Callable[[str], None] = lambda who: None,
                 on_call: Callable[[bool], None] = lambda up: None) -> None:
        self.log, self.on_ring, self.on_call = log, on_ring, on_call
        self._bus = None
        self.at: Optional[AtChannel] = None
        self.device = ""  # «CC:42:10:02:44:57»
        self.book: List[Tuple[str, str]] = []
        self.book_at = 0.0
        self.ringing = ""
        self.in_call = False
        self.call_active, self.setup = False, 0  # индикаторы телефона: call и callsetup (``AT+CIND=?`` у Redmi)
        self.audio: Optional[CallAudio] = None
        self._task: Optional[asyncio.Task] = None
        self._asked: dict = {}  # устройство → когда просили Hands-Free
        self.released_until = 0.0  # отпустили Hands-Free на время звонка: до этого момента обратно не просим
        self._device_path = ""  # /org/bluez/hci0/dev_… телефона на HFP

    # ------------------------------------------------------------------ bluealsa

    @staticmethod
    def ensure_profile(log: Callable[[str], None]) -> bool:
        """``bluealsa`` с ``hfp-hf``: Алиса запускает его шлюзом (``hfp-ag``) — перезапуск с нашими профилями.

        Только процесс, не init-скрипт: выключил ``RBX_HFP`` — после перезагрузки снова как у Алисы.
        """
        try:
            out = subprocess.run(["ps", "w"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        line = next((l for l in out.splitlines() if BLUEALSA + " " in l), "")
        if "hfp-hf" in line and "hsp-hs" not in line:
            return True
        log("📞 bluealsa → роль гарнитуры (hfp-hf)")
        subprocess.run(["start-stop-daemon", "-K", "-x", BLUEALSA, "-n", "bluealsa", "-o"], check=False)
        time.sleep(1)
        subprocess.run(["start-stop-daemon", "-S", "-b", "-x", BLUEALSA, "-n", "bluealsa", "--"] + PROFILE_ARGS,
                       check=False)
        return True

    async def _connect(self):
        if self._bus is None:
            from dbus_next import BusType  # noqa: PLC0415
            from dbus_next.aio import MessageBus  # noqa: PLC0415

            self._bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        return self._bus

    async def _objects(self) -> dict:
        bus = await self._connect()
        intro = await bus.introspect("org.bluealsa", "/org/bluealsa")
        om = bus.get_proxy_object("org.bluealsa", "/org/bluealsa", intro).get_interface(
            "org.freedesktop.DBus.ObjectManager")
        return await om.call_get_managed_objects()

    async def attach(self) -> bool:
        """Найти телефон по HFP и открыть AT-канал; уже открыт — ``True``."""
        if self.at is not None:
            return True
        objects = await self._objects()
        path = next((p for p, ifaces in objects.items() if "org.bluealsa.RFCOMM1" in ifaces
                     and ifaces["org.bluealsa.RFCOMM1"].get("Transport") and
                     ifaces["org.bluealsa.RFCOMM1"]["Transport"].value == "HFP-HF"), None)
        if path is None:
            return False
        bus = await self._connect()
        intro = await bus.introspect("org.bluealsa", path)
        fd = await bus.get_proxy_object("org.bluealsa", path, intro).get_interface("org.bluealsa.RFCOMM1").call_open()
        self.device = path.split("/")[-2].replace("dev_", "").replace("_", ":")
        self._device_path = "/org/bluez/hci0/" + path.split("/")[-2]
        self.at = AtChannel(fd, self.log)
        await self.at.ask("AT+CLIP=1")  # номер входящего в «+CLIP»
        await self.at.ask('AT+CSCS="UTF-8"')
        self.audio = CallAudio(self.device, self.log)
        self._task = asyncio.ensure_future(self._events())
        self.log(f"📞 телефон {self.device} на связи (HFP)")
        if not CALL_AUDIO:
            ok, lines = await self.at.ask("AT+CLCC", timeout=3)
            if ok and any(l.startswith("+CLCC") for l in lines):  # звонок ещё идёт — звук оставить телефону
                self.log("📞 звонок ещё идёт — снова отпускаю Hands-Free")
                asyncio.ensure_future(self.release())
        return True

    async def _events(self) -> None:
        while self.at is not None:
            line = await self.at.events.get()
            if line == "DISCONNECTED":
                released = time.time() < self.released_until
                self.log("📞 AT-канал закрыт: Hands-Free отпущен, разговор в телефоне" if released
                         else "📞 телефон отключился")
                self.at = None
                self._set_call(False, "Hands-Free отпущен" if released else "")
                return
            if line.startswith(("RING", "+CLIP", "+CIEV: 1,", "+CIEV: 2,", "NO CARRIER", "BUSY")):
                self.log(f"📞 телефон: {re.sub(r'[0-9]{4,}', '…', line)}")  # номер в лог не пишем
            if line == "RING" and not self.ringing:
                self.ringing = "кто-то"
                asyncio.get_running_loop().call_later(0.6, lambda: self.on_ring(self.ringing))  # +CLIP идёт следом
            elif line.startswith("+CLIP"):
                q = re.findall(r'"([^"]*)"', line)
                number = q[0] if q else ""
                names = [n for num, n in self.book if re.sub(r"\D", "", num)[-10:] == re.sub(r"\D", "", number)[-10:]]
                self.ringing = names[0] if names else (number or "кто-то")
            elif line.startswith("+CIEV"):
                self._ciev(line)
            elif line in ("NO CARRIER", "BUSY"):
                self._set_call(False)

    def _ciev(self, line: str) -> None:
        """``+CIEV: <инд>,<знач>``: у Android 1 — call (0/1), 2 — callsetup (0 — нет, 1 — входящий, 2/3 — исходящий)."""
        m = re.match(r"\+CIEV:\s*(\d+)\s*,\s*(\d+)", line)
        if not m:
            return
        ind, val = int(m.group(1)), int(m.group(2))
        if ind == 1:
            self.call_active = val == 1
        elif ind == 2:
            self.setup = val
            if val == 0 and not self.call_active:
                self.ringing = ""
        else:
            return
        # исходящий — разговор уже с гудков (звук нужен сразу), входящий — когда приняли
        self._set_call(self.call_active or self.setup in (2, 3))

    def _set_call(self, up: bool, why: str = "") -> None:
        if up == self.in_call:
            return
        self.in_call, self.ringing = up, ""
        if not up:
            self.call_active, self.setup = False, 0
        if self.audio is not None and (CALL_AUDIO or not up):
            (self.audio.start if up else self.audio.stop)()
        self.log(f"📞 разговор {'начался' if up else 'закончился'}" + (f" ({why})" if why else ""))
        if up:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._watch_call())
                if not CALL_AUDIO:
                    loop.call_later(1.0, lambda: asyncio.ensure_future(self.release()))
            except RuntimeError:  # без цикла (проверка индикаторов в тестах) — без сторожа
                pass
        self.on_call(up)

    async def release(self) -> None:
        """Отпустить Hands-Free у телефона: разговор остаётся в телефоне (см. :data:`RELEASE_HOLD_S`)."""
        self.released_until = time.time() + RELEASE_HOLD_S
        path = self._device_path
        if not path:
            return
        try:
            bus = await self._connect()
            intro = await bus.introspect("org.bluez", path)
            device = bus.get_proxy_object("org.bluez", path, intro).get_interface("org.bluez.Device1")
            await device.call_disconnect_profile(HFP_AG_UUID)
            self.log(f"📞 отпустил Hands-Free: разговор — в телефоне (обратно через {RELEASE_HOLD_S / 60:.0f} мин "
                     "или по команде звонка)")
        except Exception as exc:  # noqa: BLE001
            self.log(f"📞 Hands-Free не отпустился: {type(exc).__name__}: {exc}")

    async def _watch_call(self) -> None:
        """Сторож разговора: ``AT+CLCC`` без звонков дважды подряд или пропал AT-канал — разговор окончен."""
        empty = 0
        reported = ""
        while self.in_call:
            await asyncio.sleep(CLCC_EVERY_S)
            if not self.in_call:
                return
            if self.at is None:
                self._set_call(False, "телефон отключился")
                return
            ok, lines = await self.at.ask("AT+CLCC", timeout=3)
            empty = empty + 1 if ok and not any(l.startswith("+CLCC") for l in lines) else 0
            if empty >= 2:
                self._set_call(False, "AT+CLCC: звонков нет")
                return
            problem = self.audio.check() if self.audio is not None else ""
            if problem and problem != reported:
                self.log(f"📞 звук звонка: умер {problem}")
                reported = problem

    # ------------------------------------------------------------------ книга и команды

    async def keep_attached(self, every: float = 15.0) -> None:
        """Фоном: телефон подключился — открыть AT-канал и подтянуть книгу (первый «набери» без ожидания)."""
        while True:
            try:
                if self.at is None and time.time() >= self.released_until and not await self.attach():
                    await self.ask_hfp()
                if self.at is not None and not self.book:
                    await self.load_book()
            except Exception as exc:  # noqa: BLE001 — нет bluealsa/D-Bus: попробуем позже
                self.log(f"📞 телефон: {type(exc).__name__}: {exc}")
            await asyncio.sleep(every)

    async def ask_hfp(self) -> None:
        """Телефон подключён только музыкой (A2DP) — попросить у него Hands-Free (``Device1.ConnectProfile``).

        09.11: после переподключения Redmi поднял один A2DP, и звонков не было, пока не позвали профиль руками.
        """
        objects = await self._objects()
        devices = {ifaces["org.bluealsa.PCM1"]["Device"].value for p, ifaces in objects.items()
                   if "org.bluealsa.PCM1" in ifaces and "Device" in ifaces["org.bluealsa.PCM1"]}
        for dev in sorted(devices):
            if time.time() - self._asked.get(dev, 0) < 300:  # отказал — не дёргать телефон каждые 15 с
                continue
            self._asked[dev] = time.time()
            bus = await self._connect()
            intro = await bus.introspect("org.bluez", dev)
            device = bus.get_proxy_object("org.bluez", dev, intro).get_interface("org.bluez.Device1")
            try:
                await device.call_connect_profile(HFP_AG_UUID)
                self.log(f"📞 попросил у телефона Hands-Free: {dev.rsplit('/', 1)[-1]}")
            except Exception as exc:  # noqa: BLE001 — телефон не умеет или отказал: музыка остаётся
                self.log(f"📞 Hands-Free не поднялся: {type(exc).__name__}: {exc}")

    async def press(self) -> bool:
        """Кнопка колонки: звонит — принять, разговор — сбросить. ``False`` — звонка нет, кнопка не про звонок."""
        if self.at is None or not (self.ringing or self.in_call):
            return False
        await self.at.ask("AT+CHUP" if self.in_call else "ATA")
        return True

    async def load_book(self, max_age: float = 3600) -> int:
        """Контакты телефона (``AT+CPBR`` кусками по 100); свежие — из памяти. ``-1`` — телефон не дал."""
        if self.book and time.time() - self.book_at < max_age:
            return len(self.book)
        if self.at is None:
            return -1
        ok, lines = await self.at.ask('AT+CPBS="ME"')
        ok, lines = await self.at.ask("AT+CPBS?")
        m = re.search(r'\+CPBS:\s*"\w+",(\d+)', " ".join(lines))
        total = int(m.group(1)) if ok and m else 0
        if not total:
            return -1
        book, bad = [], 0
        t0 = time.time()
        # кусками по CHUNK: 09.11 Redmi отвечал на 1..40 за 0,2 с, а на 1..100 молчал (битая запись в куске) —
        # кусок не прошёл, значит, поштучно, битую запись пропускаем
        for start in range(1, total + 1, CHUNK):
            end = min(start + CHUNK - 1, total)
            ok, lines = await self.at.ask(f"AT+CPBR={start},{end}", timeout=4)
            if not ok:
                if self.at is None:
                    return -1
                lines = []
                for i in range(start, end + 1):
                    one_ok, one = await self.at.ask(f"AT+CPBR={i}", timeout=2)
                    lines += one if one_ok else []
                    bad += 0 if one_ok else 1
            book += [c for c in (parse_cpbr(l) for l in lines) if c]
        if not book:
            return -1
        self.book, self.book_at = book, time.time()
        self.log(f"📞 книга телефона: {len(book)} контактов за {time.time() - t0:.1f} с"
                 + (f", не прочитались {bad}" if bad else ""))
        return len(book)

    async def handle(self, text: str) -> Optional[str]:
        """Команда звонка во фразе (вейк уже снят) → фраза колонке (``""`` — сделано, сказать нечего);
        не команда — ``None``."""
        action, who = call_command(text)
        if not action:
            return None
        try:
            if not await self.attach():
                # телефон подключён одной музыкой — попросить Hands-Free сейчас, а не ждать фоновой проверки
                # (09.11 первое «позвони Киру» после перезагрузки ответило «не подключён», HFP встал через 20 с)
                self._asked.clear()
                self.released_until = 0.0  # человек сам просит звонок — Hands-Free нужен сейчас
                await self.ask_hfp()
                for _ in range(8):
                    await asyncio.sleep(1)
                    if await self.attach():
                        break
                else:
                    return "Телефон не подключён как гарнитура. Подключи его к колонке по блютузу."
                if action == "dial" and not _NUMBER_RE.match(who):
                    await self.load_book()
            if action == "hangup":
                ok, _ = await self.at.ask("AT+CHUP")
                return "Сбросил." if ok else "Не получилось сбросить."
            if action == "answer":
                ok, _ = await self.at.ask("ATA")
                return "" if ok else "Не получилось принять звонок."
            if action == "redial":
                ok, _ = await self.at.ask("AT+BLDN")
                return "Перезваниваю." if ok else "Телефон не дал перезвонить."
            if _NUMBER_RE.match(who):
                ok, _ = await self.at.ask(dial_string(who))
                return "Звоню." if ok else "Телефон не дал набрать номер."
            if await self.load_book() < 0:
                return ("Телефон не даёт контакты. В настройках блютуза телефона у колонки включи доступ к "
                        "контактам.")
            found = find_contacts(who, self.book)
            if not found:
                return f"Не нашёл «{who}» в контактах."
            if len(found) > 1 and found[0][1].lower() != found[1][1].lower():
                names = ", ".join(n for _, n in found[:3])
                return f"Нашёл несколько: {names}. Скажи точнее."
            number, name = found[0]
            ok, _ = await self.at.ask(dial_string(number))
            self.log(f"📞 набираю {name}")
            return f"Звоню: {name}." if ok else "Телефон не дал набрать номер."
        except Exception as exc:  # noqa: BLE001 — сбой Bluetooth не роняет хост
            self.log(f"📞 звонок: {type(exc).__name__}: {exc}")
            return "Со звонком не вышло, попробуй ещё раз."
