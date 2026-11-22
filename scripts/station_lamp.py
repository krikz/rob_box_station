"""Адаптер лампы mylamp (Lux) для колонки: что слать и что считать успехом решает код, не модель (ADR-0148).

Знание об HTTP-API живёт только здесь. Снято с исходников https://github.com/GOODWORKRINKZ/mylamp
(клон 22.11.2026, ``src/web/LampWebServer.cpp``, ``src/live/LiveRequestJson.cpp``,
``include/AppConfig.h``, ``frontend/src/main.ts``). Выключателя в прошивке нет: яркость 1..96,
ниже единицы прошивка поднимает до 1. «Выключить» — честный отказ, а не выдуманная ручка.

Транспорт инжектируется: ``(method, url, body, headers, timeout) -> (status, body)``. Сеть и
устройство этот модуль сам не открывает.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Callable, Optional, Tuple

# include/AppConfig.h: kMaxBrightness = 96, kDefaultBrightness = 32.
# include/settings/AppSettings.h: normalizeBrightness поднимает всё < 1 до 1.
BRIGHTNESS_MIN = 1
BRIGHTNESS_MAX = 96
BRIGHTNESS_DEFAULT = 32

FORM = "application/x-www-form-urlencoded; charset=UTF-8"
JSON = "application/json"

# Транспорт: метод, полный URL, тело (bytes или None), заголовки, таймаут → (статус, тело).
Transport = Callable[[str, str, Optional[bytes], dict, float], Tuple[int, bytes]]


class LampError(Exception):
    """Лампа не сделала то, что просили. ``kind``: ``offline`` (сеть), ``rejected`` (лампа отказала),
    ``error`` (ответ не разобрать). Фразу «сделано» из этого класса строить нельзя."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _form(fields: dict) -> bytes:
    return urllib.parse.urlencode(fields).encode("ascii")


def _loads(body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8") if body else b"{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise LampError("error", "ответ лампы не JSON") from exc
    if not isinstance(data, dict):
        raise LampError("error", "ответ лампы не объект")
    return data


class Lamp:
    """Одна лампа mylamp по базовому URL (``http://10.1.1.50``). Без сети: ходит только через ``transport``."""

    def __init__(self, base_url: str, transport: Transport, timeout: float = 4.0) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("пустой адрес лампы")
        self.base_url = base_url.strip().rstrip("/")
        self.transport = transport
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Optional[bytes], headers: dict) -> dict:
        url = _join(self.base_url, path)
        try:
            status, raw = self.transport(method, url, body, headers, self.timeout)
        except (OSError, TimeoutError) as exc:
            raise LampError("offline", f"{type(exc).__name__}: {exc}") from exc
        data = _loads(raw)
        if status != 200 or data.get("ok") is False:
            detail = str(data.get("error") or "")
            errors = data.get("errors")
            if not detail and isinstance(errors, list) and errors:
                first = errors[0]
                detail = str(first.get("message") if isinstance(first, dict) else first)
            raise LampError("rejected", f"HTTP {status} {detail}".strip())
        return data

    def status(self) -> dict:
        """``GET /api/status``: словарь как прислала лампа (``activeEffect``, ``fps``, …)."""
        return self._call("GET", "/api/status", None, {})

    def brightness(self) -> int:
        """``GET /api/settings/display`` → яркость, которую лампа подтвердила."""
        data = self._call("GET", "/api/settings/display", None, {})
        try:
            return int(data["brightness"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LampError("error", "в ответе нет яркости") from exc

    def set_brightness(self, value: int) -> int:
        """``POST /api/settings/display`` формой ``brightness=N``. Возвращает яркость из ответа лампы.

        Вне 1..96 не шлём: прошивка молча зажмёт, и колонка соврала бы, что поставила запрошенное.
        """
        if isinstance(value, bool) or not isinstance(value, int) or not BRIGHTNESS_MIN <= value <= BRIGHTNESS_MAX:
            raise LampError("rejected", f"яркость {value}: у лампы только {BRIGHTNESS_MIN}..{BRIGHTNESS_MAX}")
        data = self._call("POST", "/api/settings/display", _form({"brightness": str(value)}), {"Content-Type": FORM})
        try:
            confirmed = int(data["brightness"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LampError("error", "лампа не подтвердила яркость") from exc
        if confirmed != value:
            raise LampError("rejected", f"просила {value}, лампа подтвердила {confirmed}")
        return confirmed

    def run(self, source: str) -> None:
        """``POST /api/live/run`` телом ``{"source": "<Lux>"}``. Успех — только ``{"ok": true}``.

        Эффект временный: прошивка зовёт ``runTemporary`` и пресет не сохраняет.
        """
        text = (source or "").strip()
        if not text:
            raise LampError("rejected", "пустой эффект")
        self._call("POST", "/api/live/run", json.dumps({"source": text}).encode("utf-8"), {"Content-Type": JSON})

    def turn_off(self) -> None:
        """Выключателя в API mylamp нет. Не шлём яркость 0: прошивка поднимет её до 1 и лампа останется гореть."""
        raise LampError("rejected", "у лампы mylamp нет выключателя: яркость только 1..96")
