"""Жалоба в GitHub из колонки: разбор фразы, тело issue без секретов, пауза между жалобами (без сети)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_report as sr  # noqa: E402


@pytest.fixture
def posted(monkeypatch):
    """Подмена сети: запоминает запросы и отвечает как GitHub на созданную issue."""
    calls = []

    def fake_post(url, token, payload, _timeout=20):
        calls.append({"url": url, "token": token, "payload": payload})
        return 201, {"number": len(calls), "html_url": f"https://github.com/krikz/rob_box_station/issues/{len(calls)}"}

    monkeypatch.setattr(sr, "_post", fake_post)
    return calls


def _reporter(tmp_path, token="gh-token", log_paths=(), logs=None):
    (tmp_path / "VERSION").write_text("v0.1.9\n", encoding="utf-8")
    logs = [] if logs is None else logs
    return sr.Reporter(tmp_path, lambda: token, logs.append, log_paths=list(log_paths))


def test_command_words():
    """Команду жалобы узнаёт код по словам, остальное — обычный ход модели."""
    assert sr.report_command("пожалуйся, что не слышишь меня") == ("file", "что не слышишь меня")
    assert sr.report_command("заведи баг: музыка обрывается") == ("file", "музыка обрывается")
    assert sr.report_command("передай разработчику: кольцо залипло") == ("file", "кольцо залипло")
    assert sr.report_command("пожалуйся без лога, что долго думаешь") == ("bare", "что долго думаешь")
    assert sr.report_command("расскажи сказку") == ("", "")
    assert sr.report_command("") == ("", "")


def test_command_without_complaint_keeps_empty_text():
    """«Пожалуйся» без слов — жалобы нет: колонка спросит, на что жаловаться."""
    assert sr.report_command("пожалуйся") == ("file", "")


def test_mask_secrets_keeps_names_hides_values():
    assert sr.mask_secrets("MINIMAX_API_KEY=abc123") == "MINIMAX_API_KEY=***"
    assert sr.mask_secrets("GH_ISSUE_TOKEN: ghp_abcdefghijklmnopqrstuvwx") == "GH_ISSUE_TOKEN=***"
    assert sr.mask_secrets("Authorization: Bearer abcdef") == "Authorization: ***"
    assert sr.mask_secrets("ключ sk-abcdefghijklmnopqrst") == "ключ ***"
    assert sr.mask_secrets("🎤 1.1 с → STT 1.58 с: «Игорёк, ты здесь?»").endswith("«Игорёк, ты здесь?»")


def test_log_lines_take_both_files_in_order_and_skip_noise(tmp_path):
    old = tmp_path / "station.log.1"
    cur = tmp_path / "station.log"
    old.write_text("🎤 1.1 с → STT 1.58 с: «Игорёк, ты здесь?»\nкольцо: анимации Алисы\n", encoding="utf-8")
    cur.write_text("ход 3.20 с: []\nSTT 429 — пауза 20 с\n⚙ страница настроек: порт 8080\n"
                   "ход 2.10 с: MINIMAX_API_KEY=zzz\n", encoding="utf-8")

    lines = _reporter(tmp_path, log_paths=[old, cur]).log_lines()

    assert lines[0].startswith("🎤 1.1 с")
    assert "кольцо: анимации" not in "\n".join(lines)
    assert "⚙ страница настроек" not in "\n".join(lines)
    assert lines[-1] == "ход 2.10 с: MINIMAX_API_KEY=***"


def test_body_has_complaint_version_and_settings_without_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("RBX_VOICE", "English_Jovialman")
    monkeypatch.setenv("RBX_VOLUME", "230")
    monkeypatch.setenv("TZ", "CET-1")
    body = _reporter(tmp_path).body("не слышишь меня", with_log=False)

    assert "> не слышишь меня" in body and "v0.1.9" in body
    assert "голос: English_Jovialman" in body and "часовой пояс лога: CET-1" in body
    assert "```" not in body


def test_body_with_log_has_fence(tmp_path):
    cur = tmp_path / "station.log"
    cur.write_text("ход 3.20 с: []\n", encoding="utf-8")

    assert "```" in _reporter(tmp_path, log_paths=[cur]).body("долго думаешь", with_log=True)


def test_file_posts_issue_and_speaks_number(tmp_path, posted):
    """Голосом — только номер: ссылку MiniMax читает по буквам, ей место в логе."""
    logs = []
    phrase = _reporter(tmp_path, logs=logs).file("не слышишь меня")

    assert "номер 1" in phrase and "https" not in phrase
    assert posted[0]["url"] == "https://api.github.com/repos/krikz/rob_box_station/issues"
    assert posted[0]["token"] == "gh-token"
    assert posted[0]["payload"]["title"] == "колонка: не слышишь меня"
    assert "> не слышишь меня" in posted[0]["payload"]["body"]
    assert any("issues/1" in line for line in logs)


def test_same_complaint_not_filed_twice(tmp_path, posted):
    """Та же жалоба в окне паузы второй раз не уходит — и колонка называет номер первой."""
    reporter = _reporter(tmp_path)
    reporter.file("не слышишь меня")
    phrase = reporter.file("не слышишь меня")

    assert len(posted) == 1
    assert "уже отправил" in phrase and "номер 1" in phrase


def test_cooldown_blocks_other_complaint(tmp_path, posted):
    reporter = _reporter(tmp_path)
    reporter.file("не слышишь меня")

    assert "подожди" in reporter.file("музыка обрывается") and len(posted) == 1


def test_report_repo_is_station_by_default(tmp_path, posted):
    """По умолчанию жалоба уходит в rob_box_station (Шифу 08.11) — колонка сама репозиторий не выбирает."""
    assert sr.REPO == "krikz/rob_box_station"
    _reporter(tmp_path).file("долго думаешь")
    assert "/repos/krikz/rob_box_station/issues" in posted[0]["url"]


def test_no_token_speaks_about_settings_page(tmp_path, posted):
    phrase = _reporter(tmp_path, token="").file("не слышишь меня")

    assert "странице настроек" in phrase and posted == []


def test_empty_complaint_asks_what_to_report(tmp_path, posted):
    phrase = _reporter(tmp_path).file("  ")

    assert "на что жаловаться" in phrase and posted == []


def test_http_error_does_not_save_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_post", lambda *a, **kw: (401, {}))
    reporter = _reporter(tmp_path)

    assert "401" in reporter.file("не слышишь меня")
    assert reporter.state() == {}


def test_network_failure_does_not_raise(tmp_path, monkeypatch):
    """Сеть легла — хост колонки не падает, человек слышит честный отказ."""

    def boom(*_a, **_kw):
        raise TimeoutError("codeload не ответил")

    monkeypatch.setattr(sr, "_post", boom)

    assert "не ответил" in _reporter(tmp_path).file("не слышишь меня")


def test_state_file_survives_new_process(tmp_path, posted):
    """Пауза переживает перезапуск хоста: состояние лежит в файле рядом с VERSION."""
    _reporter(tmp_path).file("не слышишь меня")
    state = json.loads((tmp_path / "report_state.json").read_text(encoding="utf-8"))

    assert state["hash"] and state["at"] > 0 and state["issue"].endswith("/issues/1")
    assert "уже отправил" in _reporter(tmp_path).file("не слышишь меня")
    assert len(posted) == 1
