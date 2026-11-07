"""Первая настройка колонки (ADR-0167 Ш6): Wi-Fi по Bluetooth (Improv Wi-Fi) и страница настроек в локальной сети.

* **Improv Wi-Fi по BLE** (https://www.improv-wifi.com/ble/): GATT-сервис через BlueZ D-Bus (``bluetoothd -E``,
  ``dbus-next``). С телефона на Android — Chrome, страница improv-wifi.com, кнопка «Connect». Колонка просит
  подтверждения любой кнопкой колонки (состояние «нужна авторизация»), потом принимает имя сети и пароль.
* **Смена сети без риска потерять колонку**: новая сеть добавляется к сохранённым (``wpa_cli add_network``) с
  приоритетом выше прочих; старые не выключаются. Не подключилась за ``WIFI_WAIT_S`` — сеть удаляется, колонка
  возвращается в прежнюю сама.
* **Страница настроек** ``http://<колонка>:8080/``: вейк, голос, громкость, яркость кольца, ключи LLM/TTS (видны
  только последние 4 знака), промпт (основа + дополнение колонки, свои копии — ``Prompts``), смена сети Wi-Fi (тем же
  безопасным ``connect_wifi``). Сохранение — только в окно после нажатия любой кнопки колонки (как Improv): чужой
  в сети не сменит.

Чистые функции (разбор пакетов Improv, проверка настроек, форма) — без D-Bus, их гоняет ``tests/test_station_setup.py``.
"""
import asyncio  # без ``from __future__ import annotations``: dbus-next читает сигнатуры D-Bus из аннотаций
import html
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

IMPROV_SERVICE = "00467768-6228-2272-4663-277478268000"
CHAR_STATE = "00467768-6228-2272-4663-277478268001"
CHAR_ERROR = "00467768-6228-2272-4663-277478268002"
CHAR_RPC = "00467768-6228-2272-4663-277478268003"
CHAR_RESULT = "00467768-6228-2272-4663-277478268004"
CHAR_CAPS = "00467768-6228-2272-4663-277478268005"
#: Состояния и ошибки Improv (спецификация BLE).
AUTH_REQUIRED, AUTHORIZED, PROVISIONING, PROVISIONED = 1, 2, 3, 4
ERR_NONE, ERR_INVALID, ERR_UNKNOWN_CMD, ERR_CONNECT, ERR_NOT_AUTHORIZED, ERR_UNKNOWN = 0, 1, 2, 3, 4, 0xFF
CMD_WIFI, CMD_IDENTIFY = 1, 2
CAP_IDENTIFY = 0x01
#: Окно после нажатия любой кнопки колонки, когда можно настраивать (Improv и страница).
AUTH_S = 60.0
WIFI_WAIT_S = 30.0
HTTP_PORT = int(os.environ.get("RBX_HTTP_PORT", "8080"))
#: Ключи, которые правятся со страницы (имена из ``scripts/dialogue/.env`` колонки).
#: GITHUB_TOKEN — обновления закрытого репо; YANDEX_API_KEY — распознавание и синтез Яндекса; JEV_API_KEY — чойсер.
KEY_NAMES = ("DEEPSEEK_API_KEY", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID", "YANDEX_API_KEY", "JEV_API_KEY", "GITHUB_TOKEN")
#: Голоса MiniMax для выбора на странице (id → как назвать); пусто — голос по умолчанию.
VOICES = {"": "по умолчанию", "Russian_ReliableMan": "Надёжный мужчина", "Russian_BrightHeroine": "Яркая героиня",
          "Russian_AttractiveGuy": "Обаятельный парень", "Russian_AmbitiousWoman": "Целеустремлённая женщина"}


# ---------------------------------------------------------------- Improv: пакеты


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def parse_rpc(buf: bytes) -> Optional[Tuple[int, bytes]]:
    """Пакет RPC ``[команда, длина, данные…, контрольная сумма]`` → (команда, данные); ``None`` — пакет ещё не весь
    (BLE режет длинную запись). Битая сумма — ``ValueError``."""
    if len(buf) < 2 or len(buf) < buf[1] + 3:
        return None
    size = buf[1]
    packet = buf[: size + 3]
    if checksum(packet[:-1]) != packet[-1]:
        raise ValueError("контрольная сумма")
    return packet[0], bytes(packet[2: 2 + size])


def parse_wifi(data: bytes) -> Tuple[str, str]:
    """Данные команды 1: ``[длина имени, имя, длина пароля, пароль]`` (UTF-8)."""
    n = data[0]
    ssid = data[1: 1 + n]
    m = data[1 + n]
    password = data[2 + n: 2 + n + m]
    if len(ssid) != n or len(password) != m or not ssid:
        raise ValueError("длины полей")
    return ssid.decode("utf-8"), password.decode("utf-8")


def build_result(command: int, strings: List[str]) -> bytes:
    """Ответ RPC: ``[команда, длина, (длина строки, строка)…, сумма]``."""
    body = b"".join(bytes([len(s.encode())]) + s.encode() for s in strings)
    packet = bytes([command, len(body)]) + body
    return packet + bytes([checksum(packet)])


# ---------------------------------------------------------------- Wi-Fi через wpa_cli


async def wpa(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec("wpa_cli", "-i", "wlan0", *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace").strip()


def wpa_status(text: str) -> Dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def psk_value(password: str) -> str:
    """Пароль для ``set_network psk``: WPA — 8–63 знака в кавычках; кавычку внутри wpa_cli не передать."""
    if not 8 <= len(password) <= 63 or '"' in password:
        raise ValueError("пароль WPA — 8–63 знака без кавычек")
    return f'"{password}"'


async def connect_wifi(ssid: str, password: str, log: Callable[[str], None]) -> Optional[str]:
    """Подключиться к сети, не теряя прежнюю: IP при успехе, ``None`` — сеть удалена, колонка вернулась в старую."""
    status = wpa_status(await wpa("status"))
    if status.get("ssid") == ssid and status.get("wpa_state") == "COMPLETED" and status.get("ip_address"):
        log(f"📶 уже в сети «{ssid}»")
        return status["ip_address"]
    prios = []
    for line in (await wpa("list_networks")).splitlines()[1:]:
        nid = line.split("\t")[0]
        if nid.isdigit():
            value = await wpa("get_network", nid, "priority")
            prios.append(int(value) if value.lstrip("-").isdigit() else 0)
    nid = await wpa("add_network")
    steps = [("ssid", ssid.encode("utf-8").hex()), ("priority", str(max(prios, default=0) + 1))]
    steps += [("psk", psk_value(password))] if password else [("key_mgmt", "NONE")]
    for name, value in steps:
        if await wpa("set_network", nid, name, value) != "OK":
            await wpa("remove_network", nid)
            raise ValueError(f"wpa_cli не принял {name}")
    await wpa("enable_network", nid)
    await wpa("reassociate")
    log(f"📶 подключаюсь к «{ssid}» (сеть {nid})")
    deadline = time.time() + WIFI_WAIT_S
    while time.time() < deadline:
        await asyncio.sleep(2)
        status = wpa_status(await wpa("status"))
        if status.get("ssid") == ssid and status.get("wpa_state") == "COMPLETED" and status.get("ip_address"):
            await wpa("save_config")
            log(f"📶 в сети «{ssid}», IP {status['ip_address']}, сохранено")
            return status["ip_address"]
    await wpa("remove_network", nid)
    await wpa("reassociate")
    log(f"📶 «{ssid}» не подключилась за {WIFI_WAIT_S:.0f} с — сеть удалена, возвращаюсь в прежнюю")
    return None


# ---------------------------------------------------------------- Авторизация кнопкой


class Auth:
    """Окно настройки после нажатия любой кнопки колонки: Improv и страница пишут только в него."""

    def __init__(self) -> None:
        self.until = 0.0
        self.listeners: List[Callable[[], None]] = []

    def grant(self) -> None:
        self.until = time.time() + AUTH_S
        for fn in self.listeners:
            fn()

    def ok(self) -> bool:
        return time.time() < self.until


# ---------------------------------------------------------------- Настройки


def load_settings(path: Path) -> Dict[str, str]:
    try:
        return {str(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def check_settings(form: Dict[str, str]) -> Dict[str, str]:
    """Поля формы → настройки (``RBX_*``); неверное значение — ``ValueError`` с понятной причиной."""
    out: Dict[str, str] = {}
    wake = [w.strip().lower() for w in form.get("wake", "").split(",") if w.strip()]
    if not wake or any(len(w) < 3 or len(w) > 20 for w in wake):
        raise ValueError("вейк: слова через запятую, каждое 3–20 букв")
    out["RBX_WAKE"] = ",".join(wake)
    for key, name, lo, hi in (("volume", "RBX_VOLUME", 60, 255), ("led", "RBX_LED", 0, 255)):
        value = form.get(key, "").strip()
        if not value.isdigit() or not lo <= int(value) <= hi:
            raise ValueError(f"{key}: число {lo}–{hi}")
        out[name] = value
    voice = form.get("voice", "")
    if voice not in VOICES:
        raise ValueError("голос: нет в списке")
    out["RBX_VOICE"] = voice
    return out


def read_env(path: Path) -> Dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.lstrip().startswith("#"))


def update_env(path: Path, changes: Dict[str, str]) -> List[str]:
    """Заменить или дописать ключи в ``.env`` (права 0600 сохраняются); пустое значение — ключ не трогать."""
    changes = {k: v.strip() for k, v in changes.items() if k in KEY_NAMES and v.strip()}
    if not changes:
        return []
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0]
        if name in changes:
            lines[i] = f"{name}={changes[name]}"
            seen.add(name)
    lines += [f"{k}={v}" for k, v in changes.items() if k not in seen]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return sorted(changes)


def mask(value: str) -> str:
    return "не задан" if not value else f"…{value[-4:]}"


class Prompts:
    """Промпт колонки: основа (свой файл или файл ядра) + дополнение колонки. Файлы ядра не правятся: свои копии —
    в ``own_dir``; ход собирается из ``materialize`` (каталог, который хост отдаёт стенду как ``PROMPT_DIR``)."""

    FILE, EXTRA = "master_prompt_compact.txt", "station_extra.txt"
    MAX_CHARS = 20000

    def __init__(self, core_dir: Path, own_dir: Path) -> None:
        self.core_dir, self.own_dir = core_dir, own_dir

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def core(self) -> str:
        return self._read(self.core_dir / self.FILE)

    def is_own(self) -> bool:
        return (self.own_dir / self.FILE).exists()

    def base(self) -> str:
        return self._read(self.own_dir / self.FILE) if self.is_own() else self.core()

    def extra(self) -> str:
        return self._read(self.own_dir / self.EXTRA)

    def effective(self) -> str:
        extra = self.extra().strip()
        return self.base().rstrip() + ("\n\n# STATION (колонка «Игорёк»)\n" + extra + "\n" if extra else "\n")

    def materialize(self, out_dir: Path, facts: str = "") -> Path:
        """Собрать промпт хода в ``out_dir``: основа + дополнение + ``facts`` (факты устройства строит код хоста)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        section = f"\n# DEVICE FACTS (built by code at start, true)\n{facts.strip()}\n" if facts else ""
        text = self.effective() + section
        (out_dir / self.FILE).write_text(text, encoding="utf-8")
        return out_dir

    def save(self, base: str, extra: str, reset: bool) -> str:
        """Записать основу (совпала с ядром или ``reset`` — своя копия удаляется) и дополнение; итог словами."""
        base, extra = base.replace("\r\n", "\n"), extra.replace("\r\n", "\n")
        if len(base) > self.MAX_CHARS or len(extra) > self.MAX_CHARS:
            raise ValueError(f"промпт длиннее {self.MAX_CHARS} знаков")
        if not reset and not base.strip():
            raise ValueError("основа промпта пустая — отметь «вернуть промпт ядра», если нужен он")
        self.own_dir.mkdir(parents=True, exist_ok=True)
        own = self.own_dir / self.FILE
        if reset or base.strip() == self.core().strip():
            own.unlink(missing_ok=True)
            kind = "основа — ядра"
        else:
            own.write_text(base, encoding="utf-8")
            kind = "основа — своя"
        (self.own_dir / self.EXTRA).write_text(extra, encoding="utf-8")
        return f"{kind}, дополнение {len(extra.strip())} знаков"


def scan_ssids(text: str) -> List[str]:
    """``wpa_cli scan_results`` → имена сетей по силе сигнала, без повторов и скрытых."""
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 5 and parts[4].strip() and parts[2].lstrip("-").isdigit():
            rows.append((int(parts[2]), parts[4]))
    seen: List[str] = []
    for _level, ssid in sorted(rows, reverse=True):
        if ssid not in seen:
            seen.append(ssid)
    return seen


STYLE = """body{font:16px system-ui;margin:0 auto;max-width:560px;padding:16px;background:#f6f6f4;color:#222}
label{display:block;margin:12px 0}input,select,textarea{display:block;width:100%;box-sizing:border-box;padding:8px;
font:inherit;margin-top:4px}textarea{font:13px ui-monospace,monospace;min-height:10em}button{padding:10px 16px;
font:inherit}.note{background:#fff3c4;padding:8px}small{color:#666}h2{margin-top:28px}
.check{display:flex;gap:8px;align-items:center}.check input{width:auto;display:inline}
@media(prefers-color-scheme:dark){body{background:#1b1b1b;color:#eee}.note{background:#4a4020}small{color:#aaa}}"""


def render_page(settings: Dict[str, str], env: Dict[str, str], note: str = "", prompts: Optional[Prompts] = None,
                wifi: Optional[Dict[str, object]] = None) -> str:
    def field(label: str, name: str, value: str, kind: str = "text") -> str:
        return f'<label>{label}<input name="{name}" type="{kind}" value="{html.escape(value)}"></label>'

    voices = "".join(f'<option value="{html.escape(k)}"{" selected" if k == settings.get("RBX_VOICE", "") else ""}>'
                     f"{html.escape(v)}</option>" for k, v in VOICES.items())
    keys = "".join(f'<label>{k} <small>сейчас {html.escape(mask(env.get(k, "")))}</small>'
                   f'<input name="{k}" type="password" placeholder="пусто — не менять" autocomplete="off"></label>'
                   for k in KEY_NAMES)
    msg = f'<p class="note">{html.escape(note)}</p>' if note else ""
    prompt_form = ""
    if prompts is not None:
        whose = "своя (правлена здесь)" if prompts.is_own() else "из ядра"
        prompt_form = f"""<h2>Промпт</h2><form method="post" action="/prompt">
<label>Дополнение для колонки <small>дописывается в конец системного промпта: имя, характер, правила колонки</small>
<textarea name="extra" rows="8">{html.escape(prompts.extra())}</textarea></label>
<label>Основа системного промпта <small>сейчас {whose}; код ядра дописывает к ней свои разделы</small>
<textarea name="base" rows="18">{html.escape(prompts.base())}</textarea></label>
<label class="check"><input type="checkbox" name="reset" value="1">вернуть основу ядра</label>
<button type="submit">Сохранить промпт</button> <small>колонка перезапустится (~10 с)</small></form>"""
    wifi_form = ""
    if wifi is not None:
        nets = "".join(f'<option value="{html.escape(s)}">' for s in wifi.get("nets", []))
        last = f"<p><small>последняя попытка: {html.escape(str(wifi['last']))}</small></p>" if wifi.get("last") else ""
        wifi_form = f"""<h2>Wi-Fi</h2><p>Сейчас: <b>{html.escape(str(wifi.get("ssid") or "нет сети"))}</b>
<small>{html.escape(str(wifi.get("ip") or ""))}</small></p>{last}<form method="post" action="/wifi">
<label>Сеть<input name="ssid" list="nets" autocomplete="off"><datalist id="nets">{nets}</datalist></label>
<label>Пароль<input name="password" type="password" autocomplete="off"></label>
<button type="submit">Подключить</button> <small>старая сеть не забывается: не вышло за 30 с — колонка вернётся в
неё; кольцо мигнёт зелёным (вышло) или красным (нет)</small></form>"""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Колонка Игорёк</title>
<style>{STYLE}</style></head><body><h1>Колонка «Игорёк»</h1>{msg}
<p>Чтобы сохранить что-либо: нажми любую кнопку на колонке, потом кнопку на странице (на это минута).</p>
<h2>Основное</h2><form method="post" action="/save">
{field("Вейк-слова (через запятую)", "wake", settings.get("RBX_WAKE", ""))}
<label>Голос<select name="voice">{voices}</select></label>
{field("Громкость (60–255)", "volume", settings.get("RBX_VOLUME", "230"), "number")}
{field("Яркость кольца (0–255)", "led", settings.get("RBX_LED", "255"), "number")}
<h3>Ключи</h3>{keys}
<button type="submit">Сохранить</button></form>{prompt_form}{wifi_form}</body></html>"""


# ---------------------------------------------------------------- HTTP


class SettingsServer:
    """Страница настроек: ``GET /`` — формы; ``POST /save`` (основное), ``/prompt`` (промпт), ``/wifi`` (сеть).
    Любая запись — только в окно после нажатия любой кнопки колонки (``Auth``)."""

    BODY_MAX = 262144  # промпт кириллицей в urlencoded раздувается ×6

    def __init__(self, settings_path: Path, env_path: Path, auth: Auth,
                 on_saved: Callable[[Dict[str, str], List[str]], None], log, prompts: Optional[Prompts] = None,
                 on_prompt: Optional[Callable[[], None]] = None,
                 on_wifi: Optional[Callable[[bool], None]] = None) -> None:
        self.settings_path, self.env_path, self.auth = settings_path, env_path, auth
        self.on_saved, self.log = on_saved, log
        self.prompts, self.on_prompt, self.on_wifi = prompts, on_prompt, on_wifi
        self.wifi_last = ""

    async def start(self) -> None:
        await asyncio.start_server(self._client, "0.0.0.0", HTTP_PORT)
        self.log(f"⚙ страница настроек: порт {HTTP_PORT}")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            method, target = lines[0].split(" ")[:2]
            headers = {k.lower(): v.strip() for k, v in (h.split(":", 1) for h in lines[1:] if ":" in h)}
            body = b""
            size = int(headers.get("content-length", "0") or 0)
            if 0 < size <= self.BODY_MAX:
                body = await asyncio.wait_for(reader.readexactly(size), 20)
            status, page = await self._handle(method, target, body)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError, asyncio.LimitOverrunError):
            status, page = "400 Bad Request", "bad request"
        data = page.encode("utf-8")
        writer.write(f"HTTP/1.0 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                     f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        try:
            await writer.drain()
        finally:
            writer.close()

    async def _wifi_info(self) -> Dict[str, object]:
        status = wpa_status(await wpa("status"))
        nets = scan_ssids(await wpa("scan_results"))
        asyncio.ensure_future(wpa("scan"))  # к следующему открытию страницы список свежее
        return {"ssid": status.get("ssid", ""), "ip": status.get("ip_address", ""), "nets": nets,
                "last": self.wifi_last}

    async def _page(self, note: str = "") -> str:
        current = {k: os.environ.get(k, "") for k in ("RBX_WAKE", "RBX_VOLUME", "RBX_LED", "RBX_VOICE")}
        settings = {**current, **load_settings(self.settings_path)}
        return render_page(settings, read_env(self.env_path), note, self.prompts, await self._wifi_info())

    async def _handle(self, method: str, target: str, body: bytes) -> Tuple[str, str]:
        if method == "GET" and target.split("?")[0] == "/":
            return "200 OK", await self._page()
        if method != "POST" or target not in ("/save", "/prompt", "/wifi"):
            return "404 Not Found", "not found"
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
        if not self.auth.ok():
            return "403 Forbidden", await self._page("Не сохранено: нажми любую кнопку на колонке и повтори.")
        try:
            note = {"/save": self._save, "/prompt": self._prompt, "/wifi": self._wifi}[target](form)
        except ValueError as exc:
            return "400 Bad Request", await self._page(f"Не сохранено: {exc}")
        return "200 OK", await self._page(note)

    def _save(self, form: Dict[str, str]) -> str:
        new = check_settings(form)
        self.settings_path.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
        keys = update_env(self.env_path, form)
        self.log(f"⚙ настройки сохранены со страницы; ключи: {', '.join(keys) or 'без изменений'}")
        self.on_saved(new, keys)
        return "Сохранено." + (" Ключи сменились — перезапускаюсь." if keys else "")

    def _prompt(self, form: Dict[str, str]) -> str:
        if self.prompts is None:
            raise ValueError("промпт здесь не настраивается")
        done = self.prompts.save(form.get("base", ""), form.get("extra", ""), form.get("reset") == "1")
        self.log(f"⚙ промпт сохранён со страницы: {done}")
        if self.on_prompt:
            self.on_prompt()
        return f"Промпт сохранён ({done}). Колонка перезапускается — обнови страницу через 15 с."

    def _wifi(self, form: Dict[str, str]) -> str:
        ssid, password = form.get("ssid", "").strip(), form.get("password", "")
        if not ssid:
            raise ValueError("не указана сеть")
        if password:
            psk_value(password)  # проверка до подключения
        asyncio.ensure_future(self._connect(ssid, password))
        return f"Подключаюсь к «{ssid}». Если выйдет — страница будет по новому адресу (его скажет лог и роутер)."

    async def _connect(self, ssid: str, password: str) -> None:
        try:
            ip = await connect_wifi(ssid, password, self.log)
        except ValueError as exc:
            self.log(f"📶 {exc}")
            ip = None
        self.wifi_last = f"«{ssid}»: " + (f"подключилась, {ip}" if ip else "не вышло, осталась в прежней сети")
        if self.on_wifi:
            self.on_wifi(ip is not None)


# ---------------------------------------------------------------- Improv по BLE (BlueZ D-Bus)


class Improv:
    """GATT-сервис Improv Wi-Fi + LE-реклама. ``identify`` — щелчок и мигание кольцом."""

    APP, ADV = "/rbx/improv", "/rbx/improv/adv0"

    def __init__(self, auth: Auth, identify: Callable[[], None], log) -> None:
        self.auth, self.identify, self.log = auth, identify, log
        self.state = AUTH_REQUIRED
        self.error = ERR_NONE
        self.result = b""
        self._buf = b""
        self._chars: Dict[str, object] = {}
        auth.listeners.append(self._on_grant)

    # --- состояние

    def _set(self, uuid: str, value: bytes) -> None:
        char = self._chars.get(uuid)
        if char is not None:
            char.set(value)

    def _set_state(self, state: int) -> None:
        self.state = state
        self._set(CHAR_STATE, bytes([state]))

    def _set_error(self, error: int) -> None:
        self.error = error
        self._set(CHAR_ERROR, bytes([error]))

    def _on_grant(self) -> None:
        if self.state == AUTH_REQUIRED:
            self._set_state(AUTHORIZED)
            asyncio.get_running_loop().call_later(AUTH_S, self._expire)

    def _expire(self) -> None:
        if self.state == AUTHORIZED and not self.auth.ok():
            self._set_state(AUTH_REQUIRED)

    # --- RPC

    def on_write(self, data: bytes, offset: int) -> None:
        self._buf = (b"" if offset == 0 else self._buf) + bytes(data)  # длинную запись BlueZ отдаёт кусками
        try:
            rpc = parse_rpc(self._buf)
        except ValueError:
            self._buf = b""
            self._set_error(ERR_INVALID)
            return
        if rpc is None:
            return
        self._buf = b""
        asyncio.ensure_future(self._run(*rpc))

    async def _run(self, command: int, data: bytes) -> None:
        self._set_error(ERR_NONE)
        if command == CMD_IDENTIFY:
            self.identify()
            return
        if command != CMD_WIFI:
            self._set_error(ERR_UNKNOWN_CMD)
            return
        if self.state != AUTHORIZED or not self.auth.ok():
            self._set_error(ERR_NOT_AUTHORIZED)
            return
        try:
            ssid, password = parse_wifi(data)
        except (ValueError, IndexError, UnicodeDecodeError):
            self._set_error(ERR_INVALID)
            return
        self._set_state(PROVISIONING)
        try:
            ip = await connect_wifi(ssid, password, self.log)
        except ValueError as exc:
            self.log(f"📶 Improv: {exc}")
            ip = None
        if ip is None:
            self._set_state(AUTHORIZED)
            self._set_error(ERR_CONNECT)
            return
        self.result = build_result(CMD_WIFI, [f"http://{ip}:{HTTP_PORT}/"])
        self._set(CHAR_RESULT, self.result)
        self._set_state(PROVISIONED)

    # --- BlueZ

    async def start(self) -> None:
        from dbus_next import BusType, Variant  # noqa: PLC0415
        from dbus_next.aio import MessageBus  # noqa: PLC0415

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        ifaces = _gatt_interfaces(self, Variant)
        for path, iface in ifaces["objects"]:
            bus.export(path, iface)
        bus.export(self.APP, ifaces["manager"])
        bus.export(self.ADV, ifaces["advert"])
        intro = await bus.introspect("org.bluez", "/org/bluez/hci0")
        hci = bus.get_proxy_object("org.bluez", "/org/bluez/hci0", intro)
        await hci.get_interface("org.bluez.GattManager1").call_register_application(self.APP, {})
        await hci.get_interface("org.bluez.LEAdvertisingManager1").call_register_advertisement(self.ADV, {})
        self._bus = bus
        self.log("📶 Improv Wi-Fi по Bluetooth: сервис и реклама зарегистрированы")


def _gatt_interfaces(improv: Improv, Variant) -> dict:
    """Объекты D-Bus для BlueZ: сервис, пять характеристик, ObjectManager приложения, реклама."""
    from dbus_next.service import PropertyAccess, ServiceInterface, dbus_property, method  # noqa: PLC0415

    service_path = Improv.APP + "/service0"

    class Service(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.GattService1")

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":  # noqa: N802,F821,F722
            return IMPROV_SERVICE

        @dbus_property(access=PropertyAccess.READ)
        def Primary(self) -> "b":  # noqa: N802,F821,F722
            return True

    class Char(ServiceInterface):
        def __init__(self, index: int, uuid: str, flags: List[str], value: bytes) -> None:
            super().__init__("org.bluez.GattCharacteristic1")
            self.path, self.uuid, self.flags, self.value = f"{service_path}/char{index}", uuid, flags, value

        def set(self, value: bytes) -> None:
            self.value = value
            self.emit_properties_changed({"Value": value})

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":  # noqa: N802,F821,F722
            return self.uuid

        @dbus_property(access=PropertyAccess.READ)
        def Service(self) -> "o":  # noqa: N802,F821,F722
            return service_path

        @dbus_property(access=PropertyAccess.READ)
        def Flags(self) -> "as":  # noqa: N802,F821,F722
            return self.flags

        @dbus_property(access=PropertyAccess.READ)
        def Value(self) -> "ay":  # noqa: N802,F821,F722
            return self.value

        @method()
        def ReadValue(self, options: "a{sv}") -> "ay":  # noqa: N802,F821,F722
            return self.value

        @method()
        def WriteValue(self, value: "ay", options: "a{sv}"):  # noqa: N802,F821,F722
            offset = options.get("offset")
            if self.uuid == CHAR_RPC:
                improv.on_write(bytes(value), int(offset.value) if offset is not None else 0)

        @method()
        def StartNotify(self):  # noqa: N802
            return

        @method()
        def StopNotify(self):  # noqa: N802
            return

    chars = [Char(0, CHAR_STATE, ["read", "notify"], bytes([improv.state])),
             Char(1, CHAR_ERROR, ["read", "notify"], bytes([improv.error])),
             Char(2, CHAR_RPC, ["write"], b""),
             Char(3, CHAR_RESULT, ["read", "notify"], b""),
             Char(4, CHAR_CAPS, ["read"], bytes([CAP_IDENTIFY]))]
    improv._chars = {c.uuid: c for c in chars}
    service = Service()

    class Manager(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.freedesktop.DBus.ObjectManager")

        @method()
        def GetManagedObjects(self) -> "a{oa{sa{sv}}}":  # noqa: N802,F821,F722
            out = {service_path: {"org.bluez.GattService1": {
                "UUID": Variant("s", IMPROV_SERVICE), "Primary": Variant("b", True)}}}
            for c in chars:
                out[c.path] = {"org.bluez.GattCharacteristic1": {
                    "UUID": Variant("s", c.uuid), "Service": Variant("o", service_path),
                    "Flags": Variant("as", c.flags), "Value": Variant("ay", c.value)}}
            return out

    class Advert(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.LEAdvertisement1")

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> "s":  # noqa: N802,F821,F722
            return "peripheral"

        @dbus_property(access=PropertyAccess.READ)
        def ServiceUUIDs(self) -> "as":  # noqa: N802,F821,F722
            return [IMPROV_SERVICE]

        @dbus_property(access=PropertyAccess.READ)
        def LocalName(self) -> "s":  # noqa: N802,F821,F722
            return "Igorek"

        @method()
        def Release(self):  # noqa: N802
            return

    return {"objects": [(service_path, service)] + [(c.path, c) for c in chars],
            "manager": Manager(), "advert": Advert()}
