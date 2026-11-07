"""Обновление колонки: установка из архива тега, prev/ для отката, проверка места (без сети и без сторожа)."""
import io
import sys
import tarfile
from pathlib import Path

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
    (tmp_path / "prompts" / "master_prompt_compact.txt").write_text("мой промпт", encoding="utf-8")
    raw = _tarball({"scripts/station_host.py": b"new", "assets/heard.raw": b"\x00\x01",
                    "prompts/master_prompt_compact.txt": b"repo prompt", "README.md": b"x"})
    up = _updater(tmp_path, monkeypatch, raw)
    up.install("v0.2.0")
    assert (tmp_path / "station_host.py").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "prev" / "station_host.py").read_text(encoding="utf-8") == "old"
    assert (tmp_path / "assets" / "heard.raw").read_bytes() == b"\x00\x01"
    assert (tmp_path / "prompts" / "master_prompt_compact.txt").read_text(encoding="utf-8") == "мой промпт"
    assert not (tmp_path / "README.md").exists()
    assert up.current() == "v0.2.0"


def test_install_refuses_without_space(tmp_path, monkeypatch):
    up = _updater(tmp_path, monkeypatch, _tarball({"scripts/station_host.py": b"new"}))
    monkeypatch.setattr(su.shutil, "disk_usage", lambda p: type("U", (), {"free": 1024})())
    phrase = up.install("v0.2.0")
    assert "Не хватает места" in phrase and up.current() == "без версии"


def test_update_command_words():
    assert su.is_update_command("проверь обновление") and su.is_update_command("обновись")
    assert not su.is_update_command("обнови прогноз погоды")
