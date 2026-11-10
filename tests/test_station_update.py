"""Обновление колонки: установка из архива тега, prev/ для отката, проверка места (без сети и без сторожа)."""
import io
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_update as su  # noqa: E402


def _tarball(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"krikz-rob_box_station-abc123/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _updater(tmp_path, monkeypatch, raw: bytes):
    monkeypatch.setattr(su, "_get", lambda url, token, accept="": raw)
    monkeypatch.setattr(su.Updater, "_watchdog", lambda self: None)
    monkeypatch.setattr(su, "OK_MARK", tmp_path / "ok")
    return su.Updater(tmp_path, lambda: "token", lambda msg: None)


def test_install_places_scripts_keeps_prev_and_user_files(tmp_path, monkeypatch):
    (tmp_path / "station_host.py").write_text("old", encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "persona.txt").write_text("моя персона", encoding="utf-8")
    raw = _tarball({"scripts/station_host.py": b"new", "assets/heard.raw": b"\x00\x01",
                    "prompts/persona.txt": b"repo persona", "README.md": b"x"})
    up = _updater(tmp_path, monkeypatch, raw)
    up.install("v0.2.0")
    assert (tmp_path / "station_host.py").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "prev" / "station_host.py").read_text(encoding="utf-8") == "old"
    assert (tmp_path / "assets" / "heard.raw").read_bytes() == b"\x00\x01"
    assert (tmp_path / "prompts" / "persona.txt").read_text(encoding="utf-8") == "моя персона"
    assert not (tmp_path / "README.md").exists()
    assert up.current() == "v0.2.0"


def test_install_refuses_without_space(tmp_path, monkeypatch):
    up = _updater(tmp_path, monkeypatch, _tarball({"scripts/station_host.py": b"new"}))
    monkeypatch.setattr(su.shutil, "disk_usage", lambda p: type("U", (), {"free": 1024})())
    phrase = up.install("v0.2.0")
    assert "Не хватает места" in phrase and up.current() == "без версии"


def test_install_does_not_arm_watchdog(tmp_path, monkeypatch):
    """Сторож взводится хостом в момент перезапуска, а не распаковкой: иначе озвучка съедает окно отката."""
    spawned = []
    monkeypatch.setattr(su.subprocess, "Popen", lambda *a, **kw: spawned.append(a))
    up = _updater(tmp_path, monkeypatch, _tarball({"scripts/station_host.py": b"new"}))

    up.install("v0.2.0")

    assert spawned == []


def test_arm_watchdog_spawns_rollback_timer(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(su.subprocess, "Popen", lambda *a, **kw: spawned.append(a))
    monkeypatch.setattr(su, "OK_MARK", tmp_path / "ok")

    su.Updater(tmp_path, lambda: "", lambda msg: None).arm_watchdog()

    assert len(spawned) == 1
    script = spawned[0][0][3]
    assert f"sleep {su.WATCH_S}" in script and "prev" in script and "S99zzzrbxhost restart" in script


def test_get_retries_anonymous_when_token_rejected(monkeypatch):
    """08.11: отозванный токен в поле давал 401 и обновление «не проверялось». Публичный репо читаем анонимно."""
    seen = []

    def fake_read(url, headers):
        seen.append(dict(headers))
        if "Authorization" in headers:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        return b"[]"

    monkeypatch.setattr(su, "_read", fake_read)

    assert su._get("https://api.github.com/x", "revoked-token") == b"[]"
    assert "Authorization" in seen[0] and "Authorization" not in seen[1]


def test_get_does_not_retry_without_token(monkeypatch):
    def fake_read(url, headers):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(su, "_read", fake_read)
    with pytest.raises(urllib.error.HTTPError):
        su._get("https://api.github.com/x", "")


def test_get_does_not_retry_on_server_error(monkeypatch):
    calls = []

    def fake_read(url, headers):
        calls.append(headers)
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    monkeypatch.setattr(su, "_read", fake_read)
    with pytest.raises(urllib.error.HTTPError):
        su._get("https://api.github.com/x", "token")
    assert len(calls) == 1


def test_latest_speaks_bad_token(tmp_path, monkeypatch):
    def boom(*_a, **_kw):
        raise urllib.error.HTTPError("https://api.github.com/x", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(su, "_get", boom)
    tag, phrase = su.Updater(tmp_path, lambda: "token", lambda msg: None).latest()

    assert tag is None and "не принял токен" in phrase


def test_update_command_words():
    assert su.update_command("проверь обновление") == "check"
    assert su.update_command("есть ли обновления") == "check"
    assert su.update_command("обновись") == "install"
    assert su.update_command("обнови прогноз погоды") == ""
