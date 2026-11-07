"""Чистые функции первой настройки колонки: пакеты Improv Wi-Fi, проверка формы, ``.env``, страница."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_setup as ss  # noqa: E402


def _rpc(command: int, data: bytes) -> bytes:
    packet = bytes([command, len(data)]) + data
    return packet + bytes([ss.checksum(packet)])


def _wifi(ssid: str, password: str) -> bytes:
    s, p = ssid.encode(), password.encode()
    return bytes([len(s)]) + s + bytes([len(p)]) + p


def test_wifi_packet_roundtrip():
    cmd, data = ss.parse_rpc(_rpc(ss.CMD_WIFI, _wifi("Мастерская", "secret123")))
    assert cmd == ss.CMD_WIFI and ss.parse_wifi(data) == ("Мастерская", "secret123")


def test_partial_packet_waits_for_the_rest():
    packet = _rpc(ss.CMD_WIFI, _wifi("ROS2", "password1"))
    assert ss.parse_rpc(packet[:5]) is None
    assert ss.parse_rpc(packet) is not None


def test_bad_checksum_is_rejected():
    packet = bytearray(_rpc(ss.CMD_IDENTIFY, b""))
    packet[-1] ^= 0xFF
    with pytest.raises(ValueError):
        ss.parse_rpc(bytes(packet))


def test_result_packet_matches_spec():
    res = ss.build_result(ss.CMD_WIFI, ["http://10.1.1.13:8080/"])
    assert res[0] == 1 and res[1] == len(res) - 3 and res[2] == len("http://10.1.1.13:8080/")
    assert res[-1] == ss.checksum(res[:-1])


@pytest.mark.parametrize("password", ["short", "x" * 64, 'with"quote'])
def test_psk_rules(password):
    with pytest.raises(ValueError):
        ss.psk_value(password)


def test_psk_ok():
    assert ss.psk_value("good pass 1") == '"good pass 1"'


def test_check_settings_ok():
    out = ss.check_settings({"wake": "Игорёк, игорек", "volume": "230", "led": "60", "voice": "Russian_ReliableMan"})
    assert out == {"RBX_WAKE": "игорёк,игорек", "RBX_VOLUME": "230", "RBX_LED": "60",
                   "RBX_VOICE": "Russian_ReliableMan"}


@pytest.mark.parametrize("form", [
    {"wake": "", "volume": "230", "led": "60", "voice": ""},
    {"wake": "игорёк", "volume": "999", "led": "60", "voice": ""},
    {"wake": "игорёк", "volume": "230", "led": "x", "voice": ""},
    {"wake": "игорёк", "volume": "230", "led": "60", "voice": "Hacker"},
])
def test_check_settings_rejects(form):
    with pytest.raises(ValueError):
        ss.check_settings(form)


def test_update_env_keeps_other_keys_and_skips_blank(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MINIMAX_API_KEY=old\nJEV_MODEL=x\n", encoding="utf-8")
    changed = ss.update_env(env, {"MINIMAX_API_KEY": "new", "DEEPSEEK_API_KEY": " ", "EVIL": "1"})
    assert changed == ["MINIMAX_API_KEY"]
    assert ss.read_env(env) == {"MINIMAX_API_KEY": "new", "JEV_MODEL": "x"}


def test_page_never_shows_full_keys():
    page = ss.render_page({"RBX_WAKE": "игорёк"}, {"DEEPSEEK_API_KEY": "sk-abcdef123456"})
    assert "sk-abcdef123456" not in page and "…3456" in page
