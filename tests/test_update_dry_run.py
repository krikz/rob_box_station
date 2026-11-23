"""Сухой прогон обновления: решение печатается, установка не зовётся (#5153)."""
import json
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))
import update_dry_run as dry  # noqa: E402


def test_decide_stays_when_versions_match(monkeypatch):
    monkeypatch.setattr(dry.su.Updater, "latest", lambda self: (None, "Обновлений нет, у меня версия v0.2.5."))
    called = []
    monkeypatch.setattr(dry.su.Updater, "install", lambda self, tag: called.append(tag))
    decision = dry.decide("v0.2.5")
    assert decision["roll"] is False and decision["command"] == "stay" and called == []
    assert decision["phrase"] == "Обновлений нет, у меня версия v0.2.5."


def test_decide_rolls_only_when_tag_is_newer(monkeypatch):
    monkeypatch.setattr(dry.su.Updater, "latest", lambda self: ("v0.2.5", "Есть новая версия v0.2.5, у меня v0.2.4."))
    called = []
    monkeypatch.setattr(dry.su.Updater, "install", lambda self, tag: called.append(tag))
    decision = dry.decide("v0.2.4")
    assert decision["roll"] is True and decision["tag"] == "v0.2.5" and called == []
    assert decision["phrase"].startswith("Есть новая версия v0.2.5")


def test_main_reads_version_file_and_prints_json(tmp_path, monkeypatch, capsys):
    version = tmp_path / "VERSION"
    version.write_text("v0.2.5\n", encoding="utf-8")
    monkeypatch.setattr(dry.su.Updater, "latest", lambda self: (None, "Обновлений нет, у меня версия v0.2.5."))
    code = dry.main(["--version-file", str(version), "--json"])
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["roll"] is False and parsed["current"] == "v0.2.5"


def test_script_has_main_guard():
    """Файл запускается как скрипт, а не только как модуль теста."""
    source = (ROOT / "tools" / "update_dry_run.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    with pytest.raises(SystemExit):
        runpy.run_path(str(ROOT / "tools" / "update_dry_run.py"), run_name="__main__")
