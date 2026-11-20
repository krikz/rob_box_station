"""Чистые функции первой настройки колонки: пакеты Improv Wi-Fi, проверка формы, ``.env``, страница."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_setup as ss  # noqa: E402

#: Порты ядра движения, лица и камеры (ADR-0160 §8 К3) — в срезе колонки их нет.
MOTION_FACE_CAMERA_OWNERS = ("navigation", "mapping", "face", "people", "perception")


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
    p = ss.Prompts(tmp_path / "own", default_persona="You are Игорёк.\n\nХарактер.\n")
    assert p.persona() == "You are Игорёк.\n\nХарактер.\n" and not p.is_own()
    p.save("You are Колян.\r\n\r\nБолтливый.\r\n", "Говори коротко.", reset=False)
    assert p.is_own() and p.persona() == "You are Колян.\n\nБолтливый.\n"
    assert p.extra() == "Говори коротко."
    patched = p.system_patch(facts="- IP: 10.1.1.13.\n")("SYSTEM")
    assert patched.startswith("SYSTEM") and "STATION" in patched and "Говори коротко." in patched
    assert "DEVICE FACTS" in patched and "10.1.1.13" in patched
    p.save("anything", "", reset=True)
    assert not p.is_own() and p.persona() == "You are Игорёк.\n\nХарактер.\n"


def test_prompt_equal_to_factory_keeps_no_copy(tmp_path):
    p = ss.Prompts(tmp_path / "own", default_persona="base\n\ncharacter\n")
    p.save("base\n\ncharacter\n", "", reset=False)
    assert not p.is_own()


def test_empty_persona_without_reset_is_refused(tmp_path):
    with pytest.raises(ValueError):
        ss.Prompts(tmp_path / "own").save("  ", "", reset=False)


def test_profile_owners_omit_motion_face_camera():
    """ADR-0160 §8 К3: срез колонки — без движения, лица и камеры; новых владельцев не добавляем."""
    assert ss.PROFILE_OWNERS == ("speech", "memory", "clock", "web", "battery")
    assert not set(MOTION_FACE_CAMERA_OWNERS) & set(ss.PROFILE_OWNERS)


def test_write_profile_product_format(tmp_path):
    """Профиль колонки — формат ядра 4: assistant.yaml + таблица вейков и персоны рядом; одноблочная персона — ошибка."""
    p = ss.Prompts(tmp_path / "own")
    profile = p.write_profile(tmp_path / "prof", ["игорёк", "игорек"])
    assert profile.name == "assistant.yaml"
    yaml_text = profile.read_text(encoding="utf-8")
    assert "format: 4\n" in yaml_text and "name: Игорёк\n" in yaml_text
    for owner in ss.PROFILE_OWNERS:
        assert f"  - {owner}\n" in yaml_text
    for owner in MOTION_FACE_CAMERA_OWNERS:
        assert f"  - {owner}\n" not in yaml_text
    assert (tmp_path / "prof" / "wake_words.yaml").read_text(encoding="utf-8") == \
        "personality:\n  - игорёк\n  - игорек\noperator: []\n"
    assert "\n\n" in (tmp_path / "prof" / "persona.txt").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        ss.Prompts(tmp_path / "own2", default_persona="только один блок").write_profile(tmp_path / "prof2", [])


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


# ---------------------------------------------------------------- «Завести баг» (#30)


_BUG_LOG = (
    "18:47:12 🎤 4.2 с → STT yandex 1.42 с: «открой дверь пожалуйста»  🔑 kws igorek 0.99\n"
    "18:47:12 🔎 LLM REQUEST START provider=deepseek DEEPSEEK_API_KEY=sk-supersecretkeyvalue1\n"
    "18:49:03 🧭 [turn-v2] провайдер не ответил: timeout\n"
    "18:49:03 ⏱ [provider_latency] provider=deepseek status=error\n"
    "18:49:07 ход 3.53 с: ['ответ колонки']\n"
    "18:49:07 ядро: violation=bad_command\n"
    "18:49:12 🔊 синтез: «Что-то я долго думаю — повтори, пожалуйста.»\n"
    "18:49:20 chat_id=123456789 пользователь сказал «привет»\n"
    "кольцо кадр 5\n"
)


def _bug_log(tmp_path):
    log = tmp_path / "station.log"
    (tmp_path / "station.log.1").write_text("09:00:00 🎤 старый прогон: «старая реплика»\n", encoding="utf-8")
    log.write_text(_BUG_LOG, encoding="utf-8")
    return log


def test_log_page_has_bug_button():
    """#30: на /log кнопка «Завести баг» рядом с переключателем вида."""
    page = ss.render_log(Path("/no/such.log"))

    assert 'href="/bug"' in page and "Завести баг" in page
    assert page.index("Завести баг") > page.index("все строки")


def test_bug_preview_shows_exactly_what_will_be_sent(tmp_path):
    """Предпросмотр — ровно текст черновика, и сам по себе ничего не публикует (это GET-форма)."""
    log = _bug_log(tmp_path)
    title, body = ss.bug_draft(log, tmp_path)
    page = ss.render_bug(title, body, everything=False)

    assert "Завести баг" in page and 'method="post" action="/bug"' in page
    assert title in page and "версия ядра" in page
    assert "sk-supersecretkeyvalue1" not in page and "открой дверь пожалуйста" not in page
    assert "полный лог" in page


def test_bug_draft_strips_secrets_and_speech(tmp_path, monkeypatch):
    """Черновик по умолчанию: только служебные строки, ключи и реплики вырезаны, версии на месте."""
    log = _bug_log(tmp_path)
    (tmp_path / "VERSION").write_text("v0.7.10\n", encoding="utf-8")
    monkeypatch.setattr(ss, "station_version", lambda repo=None: "station-test")

    title, body = ss.bug_draft(log, tmp_path)

    assert title.startswith("Колонка: ")
    assert "v0.7.10" in body and "station-test" in body
    assert "sk-supersecretkeyvalue1" not in body and "DEEPSEEK_API_KEY=***" in body
    assert "открой дверь пожалуйста" not in body and "старая реплика" not in body
    assert "123456789" not in body  # строка с chat_id не служебная — вырезана целиком
    assert "🧭" in body and "ядро:" in body and "кольцо кадр" not in body
    assert "«…»" in body


def test_bug_draft_full_log_still_masked(tmp_path, monkeypatch):
    """Полный лог — только по явной галочке, и всё равно после маскировки."""
    log = _bug_log(tmp_path)
    monkeypatch.setattr(ss, "station_version", lambda repo=None: "x")

    _, body = ss.bug_draft(log, tmp_path, everything=True)

    assert "кольцо кадр" in body
    assert "sk-supersecretkeyvalue1" not in body and "открой дверь пожалуйста" not in body
    assert "123456789" not in body


def test_file_bug_posts_and_returns_link(monkeypatch):
    """Подтверждение создаёт issue и возвращает номер со ссылкой."""
    sent = {}

    def fake_post(url, token, payload, timeout=20):
        sent["url"], sent["token"], sent["payload"] = url, token, payload
        return 201, {"number": 31, "html_url": "https://github.com/krikz/rob_box_station/issues/31"}

    monkeypatch.setattr("station_report._post", fake_post)
    note, url = ss.file_bug("ghp_exampletokenvalue1234567890", "Колонка: тест", "тело")

    assert sent["url"].endswith("/repos/krikz/rob_box_station/issues")
    assert sent["payload"]["title"] == "Колонка: тест"
    assert note == "Завёл баг: issue номер 31"
    assert url == "https://github.com/krikz/rob_box_station/issues/31"


def test_file_bug_shows_error_instead_of_swallowing(monkeypatch):
    """Нет токена, сеть, 4xx — честная ошибка, не тишина."""
    note, url = ss.file_bug("", "t", "b")
    assert "нет токена" in note and url == ""

    def boom(url, token, payload, timeout=20):
        raise TimeoutError("timed out")

    monkeypatch.setattr("station_report._post", boom)
    note, url = ss.file_bug("token", "t", "b")
    assert note.startswith("не смог завести баг") and "TimeoutError" in note and url == ""

    monkeypatch.setattr("station_report._post", lambda *a, **k: (401, {}))
    note, url = ss.file_bug("token", "t", "b")
    assert "401" in note and url == ""


def test_file_bug_masks_what_it_sends(monkeypatch):
    """Даже правка текста руками не протащит ключ или реплику в issue."""
    sent = {}
    monkeypatch.setattr("station_report._post",
                        lambda url, token, payload, timeout=20: sent.update(payload) or (201, {"number": 1}))

    ss.file_bug("token", "Колонка: ключ", "DEEPSEEK_API_KEY=sk-leakvalue1234567890\nчеловек: «секретная реплика»")

    assert "sk-leakvalue1234567890" not in sent["body"] and "секретная реплика" not in sent["body"]


def test_bug_page_preview_publishes_nothing(tmp_path):
    """GET /bug собирает черновик и не трогает сеть."""
    import asyncio

    log = _bug_log(tmp_path)
    server = ss.SettingsServer(tmp_path / "s.json", tmp_path / ".env", ss.Auth(), lambda *a: None, lambda *a: None,
                               base=tmp_path, log_path=log, token=lambda: "should-not-be-used")

    status, page = asyncio.run(server._handle("GET", "/bug", b""))

    assert status == "200 OK" and "Завести баг" in page and 'method="post"' in page
    assert "sk-supersecretkeyvalue1" not in page and "should-not-be-used" not in page


def test_bug_page_shows_api_error(tmp_path, monkeypatch):
    """POST /bug: ошибка GitHub показывается на странице, а не глотается."""
    import asyncio

    monkeypatch.setattr("station_report._post", lambda *a, **k: (403, {}))
    server = ss.SettingsServer(tmp_path / "s.json", tmp_path / ".env", ss.Auth(), lambda *a: None, lambda *a: None,
                               base=tmp_path, log_path=_bug_log(tmp_path), token=lambda: "tok")

    status, page = asyncio.run(server._handle("POST", "/bug", "title=Колонка:+тест&body=текст+лога".encode()))

    assert status == "200 OK"
    assert "не смог завести баг" in page and "403" in page


def test_bug_lines_mask_user_dump_in_single_quotes(tmp_path):
    """#30: дамп запроса LLM ``[N] user: '…'`` — речь человека в одинарных кавычках; вырезается и в полном логе."""
    log = tmp_path / "station.log"
    log.write_text(
        "18:49:03 🔎 LLM REQUEST START\n"
        "  [1] user: '<system_context>\\n\\nсекретная реплика человека'\n"
        "  [2] assistant: 'фраза колонки остаётся'\n"
        "18:49:07 ход 4.01 с: ['ответ колонки']\n",
        encoding="utf-8")

    lines = ss.bug_lines(log, everything=True)

    text = "\n".join(lines)
    assert "секретная реплика человека" not in text
    assert "[1] user: '<…>'" in text
    assert "[2] assistant: 'фраза колонки остаётся'" in text
    assert "ход 4.01 с: ['ответ колонки']" in text


def test_file_bug_masks_user_dump(monkeypatch):
    """Тот же путь на отправке: правка текста руками не протащит дамп ``[N] user:`` в issue."""
    sent = {}
    monkeypatch.setattr("station_report._post",
                        lambda url, token, payload, timeout=20: sent.update(payload) or (201, {"number": 1}))

    ss.file_bug("token", "Колонка: тест", "  [3] user: '[говорит: голос без имени, запомнен] секретная реплика'")

    assert "секретная реплика" not in sent["body"]
    assert "[3] user: '<…>'" in sent["body"]
