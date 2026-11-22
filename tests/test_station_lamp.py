"""Адаптер лампы mylamp: что ушло на лампу и что колонка имеет право сказать. Сети нет — транспорт фейковый.

Эндпоинты сняты с исходников GOODWORKRINKZ/mylamp (22.11.2026):
``src/web/LampWebServer.cpp:178,185-186,193``, ``src/live/LiveRequestJson.cpp:36-61``,
``include/AppConfig.h:34-35``, ``frontend/src/main.ts:570-584,2472-2475``.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_lamp as lamp  # noqa: E402

BASE = "http://10.1.1.50"


class _Net:
    """Запоминает запрос и отвечает тем, что задали. ``boom`` — обрыв сети."""

    def __init__(self, status: int = 200, body: bytes = b"{}", boom: Exception = None) -> None:
        self.status, self.body, self.boom = status, body, boom
        self.calls = []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append((method, url, body, dict(headers), timeout))
        if self.boom is not None:
            raise self.boom
        return self.status, self.body


def _lamp(net: _Net) -> lamp.Lamp:
    return lamp.Lamp(BASE, net, timeout=2.5)


def test_set_brightness_posts_form_and_returns_confirmed_value():
    net = _Net(body=b'{"brightness":40,"maxBrightness":96}')
    assert _lamp(net).set_brightness(40) == 40

    method, url, body, headers, timeout = net.calls[0]
    assert (method, url, timeout) == ("POST", f"{BASE}/api/settings/display", 2.5)
    assert body == b"brightness=40"
    assert headers["Content-Type"] == lamp.FORM


def test_brightness_outside_firmware_range_is_not_sent():
    """Прошивка зажимает <1 в 1 и >96 в 96. Слать такое — соврать, что лампа стала темнее или ярче."""
    net = _Net()
    with pytest.raises(lamp.LampError) as low:
        _lamp(net).set_brightness(0)
    with pytest.raises(lamp.LampError) as high:
        _lamp(net).set_brightness(97)
    assert low.value.kind == high.value.kind == "rejected"
    assert net.calls == []


def test_turn_off_refuses_instead_of_inventing_an_endpoint():
    net = _Net()
    with pytest.raises(lamp.LampError) as caught:
        _lamp(net).turn_off()
    assert caught.value.kind == "rejected" and "выключател" in caught.value.detail
    assert net.calls == []


def test_brightness_read_from_display_settings():
    net = _Net(body=b'{"brightness":32,"maxBrightness":96}')
    assert _lamp(net).brightness() == 32
    assert net.calls[0][:2] == ("GET", f"{BASE}/api/settings/display")


def test_run_posts_lux_source_and_accepts_only_ok_true():
    source = 'effect "night"\nlayer fill {\n  color rgb(0, 0, 40)\n}'
    net = _Net(body=b'{"ok":true,"errors":[]}')
    _lamp(net).run(source)

    method, url, body, headers, _timeout = net.calls[0]
    assert (method, url) == ("POST", f"{BASE}/api/live/run")
    assert json.loads(body.decode("utf-8")) == {"source": source}
    assert headers["Content-Type"] == lamp.JSON


def test_empty_effect_is_not_sent():
    net = _Net()
    with pytest.raises(lamp.LampError):
        _lamp(net).run("   ")
    assert net.calls == []


def test_lamp_rejection_is_not_success():
    """Lux не скомпилировался — прошивка отвечает 400 и ``ok:false``. Это отказ, не «включено»."""
    raw = '{"ok":false,"errors":[{"line":1,"column":1,"message":"ожидался effect"}]}'.encode("utf-8")
    net = _Net(status=400, body=raw)
    with pytest.raises(lamp.LampError) as caught:
        _lamp(net).run('effect "x"')
    assert caught.value.kind == "rejected"
    assert "ожидался effect" in caught.value.detail


def test_display_error_body_is_a_rejection():
    net = _Net(status=400, body=b'{"error":"brightness is required"}')
    with pytest.raises(lamp.LampError) as caught:
        _lamp(net).set_brightness(10)
    assert caught.value.kind == "rejected" and "brightness is required" in caught.value.detail


def test_confirmed_brightness_must_match_what_was_asked():
    net = _Net(body=b'{"brightness":1,"maxBrightness":96}')
    with pytest.raises(lamp.LampError) as caught:
        _lamp(net).set_brightness(40)
    assert caught.value.kind == "rejected" and "40" in caught.value.detail and "1" in caught.value.detail


def test_network_failure_is_offline_not_done():
    net = _Net(boom=TimeoutError("timed out"))
    with pytest.raises(lamp.LampError) as caught:
        _lamp(net).status()
    assert caught.value.kind == "offline"
    assert net.calls and net.calls[0][1] == f"{BASE}/api/status"


def test_garbage_body_is_an_error_not_success():
    net = _Net(body=b"<html>captive</html>")
    with pytest.raises(lamp.LampError) as caught:
        _lamp(net).brightness()
    assert caught.value.kind == "error"


def test_base_url_trailing_slash_is_folded():
    net = _Net(body=b'{"version":"1.0","activeEffect":"pulse"}')
    got = lamp.Lamp(BASE + "/", net).status()
    assert got["activeEffect"] == "pulse"
    assert net.calls[0][1] == f"{BASE}/api/status"


def test_empty_base_url_is_refused():
    with pytest.raises(ValueError):
        lamp.Lamp("  ", _Net())
