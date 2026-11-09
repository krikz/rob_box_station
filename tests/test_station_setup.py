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
                   "RBX_VOICE": "Russian_ReliableMan", "RBX_RING_STYLE": "color"}


def test_ring_style_on_the_page():
    """09.11: кольцо разноцветное по умолчанию, «как у Алисы» — выбором; чужое значение не сохраняется."""
    base = {"wake": "игорёк", "volume": "230", "led": "60", "voice": ""}
    assert ss.check_settings({**base, "ring": "alice"})["RBX_RING_STYLE"] == "alice"
    with pytest.raises(ValueError):
        ss.check_settings({**base, "ring": "disco"})
    page = ss.render_page({"RBX_RING_STYLE": "alice"}, {})
    assert '<option value="alice" selected>' in page and "разноцветное" in page


def test_jovialman_on_the_page():
    """«Весельчак» (голос Саши у робота) выбирается на странице полным id каталога MiniMax."""
    out = ss.check_settings({"wake": "игорёк", "volume": "230", "led": "60", "voice": "English_Jovialman"})
    assert out["RBX_VOICE"] == "English_Jovialman"
    assert "Весельчак" in ss.render_page({"RBX_WAKE": "игорёк"}, {})


@pytest.mark.parametrize("form", [
    {"wake": "", "volume": "230", "led": "60", "voice": ""},
    {"wake": "игорёк", "volume": "999", "led": "60", "voice": ""},
    {"wake": "игорёк", "volume": "230", "led": "x", "voice": ""},
    {"wake": "игорёк", "volume": "230", "led": "60", "voice": "Hacker"},
    {"wake": "игорёк", "volume": "230", "led": "60", "voice": "Jovialman"},
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


def test_prompts_own_copy_and_reset(tmp_path):
    core, own = tmp_path / "core", tmp_path / "own"
    core.mkdir()
    (core / ss.Prompts.FILE).write_text("You are РОББОКС.\n", encoding="utf-8")
    p = ss.Prompts(core, own)
    assert p.base() == "You are РОББОКС.\n" and not p.is_own()
    p.save("You are Игорёк.\r\n", "Говори коротко.", reset=False)
    assert p.is_own() and p.base() == "You are Игорёк.\n"
    assert p.effective().endswith("Говори коротко.\n") and "Игорёк" in p.effective()
    assert (p.materialize(tmp_path / "out") / ss.Prompts.FILE).read_text(encoding="utf-8") == p.effective()
    p.save("anything", "", reset=True)
    assert not p.is_own() and p.effective() == "You are РОББОКС.\n"


def test_prompt_equal_to_core_keeps_no_copy(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / ss.Prompts.FILE).write_text("base\n", encoding="utf-8")
    p = ss.Prompts(core, tmp_path / "own")
    p.save("base\n", "", reset=False)
    assert not p.is_own()


def test_empty_base_without_reset_is_refused(tmp_path):
    with pytest.raises(ValueError):
        ss.Prompts(tmp_path, tmp_path / "own").save("  ", "", reset=False)


def test_page_has_issue_token_but_no_update_token():
    """08.11: токен для карточек клали в поле обновлений и ломали обновления — поля обновлений на странице нет."""
    page = ss.render_page({"RBX_WAKE": "игорёк"}, {})

    assert "GH_ISSUE_TOKEN" in page
    assert "GITHUB_TOKEN" not in page
    assert "GH_ISSUE_TOKEN" in ss.KEY_NAMES and "GITHUB_TOKEN" not in ss.KEY_NAMES


def test_scan_ssids_sorted_unique_no_hidden():
    text = ("bssid / frequency / signal level / flags / ssid\n"
            "aa\t2412\t-70\t[WPA2]\tROS2\nbb\t2437\t-40\t[WPA2]\tМастерская\ncc\t2412\t-50\t[WPA2]\tROS2\n"
            "dd\t2412\t-30\t[WPA2]\t\n")
    assert ss.scan_ssids(text) == ["Мастерская", "ROS2"]


def test_log_page_masks_secrets_and_shows_newest_first(tmp_path):
    """#17: лог на странице — свежее сверху, ключи скрыты, без ?all=1 — только значимое."""
    import station_setup as ss_mod

    log = tmp_path / "station.log"
    (tmp_path / "station.log.1").write_text("09:00:00 🎤 старый прогон\n", encoding="utf-8")
    log.write_text("10:00:00 🎤 первая\nкольцо кадр\n10:00:05 ход 1 с: MINIMAX_API_KEY=sk-abcdefghijklmnopqrstu\n",
                   encoding="utf-8")
    page = ss_mod.render_log(log)

    assert page.index("ход 1 с") < page.index("первая") < page.index("старый прогон")
    assert "sk-abcdefghijklmnopqrstu" not in page and "кольцо кадр" not in page
    assert "кольцо кадр" in ss_mod.render_log(log, everything=True)
