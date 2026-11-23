"""Обновление колонки: установка из архива тега, prev/ для отката, проверка места (без сети и без сторожа)."""
import hashlib
import io
import json
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_update as su  # noqa: E402


def _tarball(files: dict, prefix: str = "krikz-rob_box_station-abc123") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
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


def _core_upgrade(tmp_path, monkeypatch, archive_ref="v0.7.8", digest=None):
    (tmp_path / "VERSION").write_text("v0.2.3\n", encoding="utf-8")
    (tmp_path / "core_ref.txt").write_text("v0.7.8\n", encoding="utf-8")
    core = tmp_path / "core"
    env = core / "scripts/dialogue/.env"
    env.parent.mkdir(parents=True)
    env.write_bytes(b"EXAMPLE=local-test-value\n")
    (core / "SOURCE").write_text("ref v0.7.2\n", encoding="utf-8")
    (core / "old-only").write_bytes(b"old")
    raw = _tarball({"core/SOURCE": f"ref {archive_ref}\n".encode(),
                    "core/scripts/dialogue/chat.py": b"# bundled chat"}, prefix="rbx")
    metadata = {"assets": [{"name": "station-core.tar.gz", "id": 123,
                            "digest": "sha256:" + (digest or hashlib.sha256(raw).hexdigest())}]}
    requests = []

    def get(url, token, accept=""):
        requests.append(url.rsplit("/", 1)[-1])
        return json.dumps(metadata).encode() if url.endswith("/releases/tags/v0.2.3") else raw

    monkeypatch.setattr(su, "_get", get)
    return su.Updater(tmp_path, lambda: "token", lambda msg: None), env, requests


def test_boot_syncs_core_from_current_release_after_legacy_update(tmp_path, monkeypatch):
    up, env, requests = _core_upgrade(tmp_path, monkeypatch)
    assert up.sync_core()
    assert (tmp_path / "core/SOURCE").read_text(encoding="utf-8") == "ref v0.7.8\n"
    assert env.read_bytes() == b"EXAMPLE=local-test-value\n"
    assert not (tmp_path / "core/old-only").exists()
    assert not up.sync_core()
    assert requests == ["v0.2.3", "123"]


@pytest.mark.parametrize("archive_ref,digest", [("v0.7.2", None), ("v0.7.8", "0" * 64)])
def test_bad_release_core_leaves_current_core_intact(tmp_path, monkeypatch, archive_ref, digest):
    up, env, _ = _core_upgrade(tmp_path, monkeypatch, archive_ref, digest)
    with pytest.raises(ValueError):
        up.sync_core()
    assert (tmp_path / "core/SOURCE").read_text(encoding="utf-8") == "ref v0.7.2\n"
    assert env.is_file()
    assert (tmp_path / "core/old-only").is_file()


def test_core_rollback_restores_previous_tree_without_network(tmp_path, monkeypatch):
    up, env, requests = _core_upgrade(tmp_path, monkeypatch)
    up.sync_core()
    (tmp_path / "core_ref.txt").write_text("v0.7.2\n", encoding="utf-8")
    assert up.sync_core()
    assert (tmp_path / "core/old-only").read_bytes() == b"old"
    assert not (tmp_path / "core/scripts/dialogue/chat.py").exists()
    assert env.is_file()
    assert requests == ["v0.2.3", "123"]


def test_latest_waits_for_published_core_asset(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("v0.2.2", encoding="utf-8")
    asset = {"name": "station-core.tar.gz", "state": "uploaded", "digest": "sha256:" + "0" * 64}
    releases = [
        {"tag_name": "v0.2.5", "draft": True, "assets": [asset]},
        {"tag_name": "v0.2.4", "prerelease": True, "assets": [asset]},
        {"tag_name": "v0.2.3", "assets": []},
        {"tag_name": "v0.2.2", "assets": [asset]},
    ]

    def get(url, token, accept=""):
        assert url.endswith("/releases?per_page=20")
        return json.dumps(releases).encode()

    monkeypatch.setattr(su, "_get", get)
    monkeypatch.setattr(su.Updater, "notes", lambda self, tag: "")
    up = su.Updater(tmp_path, lambda: "", lambda msg: None)
    assert up.latest()[0] is None

    releases[2]["assets"] = [asset]
    assert up.latest()[0] == "v0.2.3"


def test_slow_core_download_leaves_current_core_for_legacy_watchdog(tmp_path, monkeypatch):
    up, env, _ = _core_upgrade(tmp_path, monkeypatch)
    now = iter((0.0, 100.0))
    monkeypatch.setattr(su, "monotonic", lambda: next(now), raising=False)
    with pytest.raises(TimeoutError):
        up.sync_core()
    assert (tmp_path / "core/SOURCE").read_text(encoding="utf-8") == "ref v0.7.2\n"
    assert env.is_file()


def test_boot_recovers_old_core_if_interrupted_between_renames(tmp_path, monkeypatch):
    up, env, requests = _core_upgrade(tmp_path, monkeypatch)
    (tmp_path / "core").rename(tmp_path / "core.prev")
    (tmp_path / "core_ref.txt").write_text("v0.7.2\n", encoding="utf-8")
    assert not up.sync_core()
    assert (tmp_path / "core/old-only").read_bytes() == b"old"
    assert env.is_file()
    assert not requests


def test_same_version_is_not_newer():
    """Совпала — не катим. Старше тоже не катим. Новее — катим."""
    assert su.newer_than("v0.2.5", "v0.2.5") is False
    assert su.newer_than("v0.2.4", "v0.2.5") is False
    assert su.newer_than("v0.2.5", "v0.2.4") is True
    assert su.newer_than("v0.3.0", "v0.2.9") is True
    assert su.newer_than("без версии", "v0.2.4") is False
    assert su.newer_than("v0.2.5", "без версии") is False


def test_install_phrase_comes_from_result():
    """Фраза — из того, что вернула установка. Пусто — отказ, не выдуманный успех."""
    placed = "Обновляюсь до v0.2.5, через минуту вернусь."
    no_room = "Не хватает места для обновления: нужно 3 мегабайт, свободно 1."
    assert su.install_phrase(placed) == placed
    assert su.install_phrase("") == "Не смог скачать обновление, попробуй позже."
    assert su.install_phrase(no_room) == no_room
    assert su.should_restart(placed) is True
    assert su.should_restart("") is False
    assert su.should_restart(no_room) is False


def test_offer_suffix_only_when_check_has_newer_tag():
    assert su.offer_suffix("v0.2.5", "check") == " Скажи «Игорёк, обновись», чтобы поставить."
    assert su.offer_suffix(None, "check") == ""
    assert su.offer_suffix("v0.2.5", "install") == ""


def test_latest_same_version_does_not_offer_install(tmp_path, monkeypatch):
    """Опубликованный тег равен VERSION — latest не отдаёт тег, install звать не с чем."""
    (tmp_path / "VERSION").write_text("v0.2.5\n", encoding="utf-8")
    asset = {"name": "station-core.tar.gz", "state": "uploaded", "digest": "sha256:" + "ab" * 32}
    releases = [{"tag_name": "v0.2.5", "assets": [asset]}, {"tag_name": "v0.2.4", "assets": [asset]}]
    monkeypatch.setattr(su, "_get", lambda url, token, accept="": json.dumps(releases).encode())
    monkeypatch.setattr(su.Updater, "notes", lambda self, tag: "не должны читать")
    tag, phrase = su.Updater(tmp_path, lambda: "", lambda msg: None).latest()
    assert tag is None
    assert phrase == "Обновлений нет, у меня версия v0.2.5."
    assert su.newer_than(tag or "", "v0.2.5") is False
