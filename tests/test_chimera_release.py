"""Выпуск CHIMERA приезжает обновлятором v0.2.4: комплект, сохранность, откат (#5153).

Архив тега собирается здесь из дерева релиза. Обновлятор — настоящий ``station_update.install``: он берёт из
архива только ``scripts/`` и ``assets/``. Мутация пакета (убрали файл, испортили хеш, сломали откат) обязана
уронить соответствующий тест.
"""

import hashlib
import io
import subprocess
import sys
import tarfile
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import station_chimera as sc  # noqa: E402
import station_update as su  # noqa: E402

TAG = "v0.2.5"
RELEASE_FILES = (
    "scripts/station_host.py",
    "scripts/station_chimera.py",
    "scripts/station_update.py",
    "assets/chimera.json",
    "assets/heard.raw",
    "assets/chimera/bird.wav",
    "assets/chimera/seal.wav",
    "assets/chimera/elephant.wav",
    "assets/chimera/alligator.wav",
    "assets/chimera/growl.wav",
)
# Куда обновлятор v0.2.4 кладёт файл: scripts/*.py — в корень установки, assets/ — как есть.
LAID = {
    "scripts/station_host.py": "station_host.py",
    "scripts/station_chimera.py": "station_chimera.py",
    "scripts/station_update.py": "station_update.py",
    "assets/chimera.json": "assets/chimera.json",
    "assets/heard.raw": "assets/heard.raw",
    "assets/chimera/bird.wav": "assets/chimera/bird.wav",
    "assets/chimera/seal.wav": "assets/chimera/seal.wav",
    "assets/chimera/elephant.wav": "assets/chimera/elephant.wav",
    "assets/chimera/alligator.wav": "assets/chimera/alligator.wav",
    "assets/chimera/growl.wav": "assets/chimera/growl.wav",
}


def release_bytes() -> dict:
    return {name: (ROOT / name).read_bytes() for name in RELEASE_FILES}


def manifest_of(files: dict) -> dict:
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def tarball(files: dict, prefix: str = "krikz-rob_box_station-chimera") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def installed_v024(tmp_path: Path) -> None:
    """Колонка на v0.2.4: код хоста старый, CHIMERA нет, настройки и память свои."""
    (tmp_path / "station_host.py").write_text("host-v0.2.4", encoding="utf-8")
    (tmp_path / "station_update.py").write_bytes((ROOT / "scripts" / "station_update.py").read_bytes())
    (tmp_path / "VERSION").write_text("v0.2.4\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text('{"RBX_VOLUME": "200"}\n', encoding="utf-8")
    (tmp_path / "memory.db").write_bytes(b"memory-rows-v1\x00\xff")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "persona.txt").write_text("своя персона\n", encoding="utf-8")


def updater(tmp_path, monkeypatch, raw: bytes):
    monkeypatch.setattr(su, "_get", lambda url, token, accept="": raw)
    monkeypatch.setattr(su.Updater, "_watchdog", lambda self: None)
    monkeypatch.setattr(su, "OK_MARK", tmp_path / "ok")
    return su.Updater(tmp_path, lambda: "", lambda msg: None)


def preserved(tmp_path: Path) -> None:
    assert (tmp_path / "settings.json").read_text(encoding="utf-8") == '{"RBX_VOLUME": "200"}\n'
    assert (tmp_path / "memory.db").read_bytes() == b"memory-rows-v1\x00\xff"
    assert (tmp_path / "prompts" / "persona.txt").read_text(encoding="utf-8") == "своя персона\n"


def test_release_files_match_profile_hashes():
    """Каждый WAV выпуска — моно 16 кГц, и его SHA256 записан в профиль."""
    files = release_bytes()
    profile = sc.load_profile(ROOT)
    for name, meta in profile["files"].items():
        data = files[f"assets/chimera/{name}"]
        assert hashlib.sha256(data).hexdigest() == meta["sha256"]
        with wave.open(io.BytesIO(data)) as wav:
            assert wav.getnchannels() == 1 and wav.getsampwidth() == 2 and wav.getframerate() == 16000
    assert profile["commands"] == {"изобрази птицу химерой": "bird.wav"}


def test_update_from_v024_lays_claimed_set(tmp_path, monkeypatch):
    """Одно обновление с v0.2.4 кладёт код хоста, профиль и каждый WAV; хеши сходятся с манифестом."""
    installed_v024(tmp_path)
    files = release_bytes()
    expected = manifest_of(files)
    up = updater(tmp_path, monkeypatch, tarball(files))

    phrase = up.install(TAG)

    assert phrase.startswith("Обновляюсь")
    for src, laid in LAID.items():
        got = (tmp_path / laid).read_bytes()
        assert hashlib.sha256(got).hexdigest() == expected[src], laid
    assert sc.verify_installed(tmp_path) == []
    assert up.current() == TAG
    preserved(tmp_path)
    # Старый хост уехал в prev/, а настроек и памяти там нет: их обновлятор не копирует.
    assert (tmp_path / "prev" / "station_host.py").read_text(encoding="utf-8") == "host-v0.2.4"
    assert not (tmp_path / "prev" / "settings.json").exists()
    assert not (tmp_path / "prev" / "memory.db").exists()


def test_incomplete_package_is_not_available(tmp_path, monkeypatch):
    """Убрали клип из пакета — CHIMERA не объявляется, настройки и память целы."""
    installed_v024(tmp_path)
    files = release_bytes()
    del files["assets/chimera/bird.wav"]
    updater(tmp_path, monkeypatch, tarball(files)).install(TAG)

    reasons = sc.verify_installed(tmp_path)
    assert reasons, "неполный пакет не должен проходить проверку"
    assert any("bird.wav" in reason for reason in reasons)
    preserved(tmp_path)


def test_corrupt_hash_is_not_available(tmp_path, monkeypatch):
    """Байт клипа не сходится с хешем в профиле — объявлять CHIMERA нельзя.

    Эталон берётся из профиля, не из файла: порча байта в дереве выпуска обязана уронить этот тест,
    а не пройти как «ожидаемый отказ».
    """
    installed_v024(tmp_path)
    files = release_bytes()
    claimed = sc.load_profile(ROOT)["files"]["bird.wav"]["sha256"]
    assert hashlib.sha256(files["assets/chimera/bird.wav"]).hexdigest() == claimed
    blown = bytearray(files["assets/chimera/bird.wav"])
    blown[44] ^= 0xFF  # первый сэмпл, заголовок WAV остаётся читаемым
    files["assets/chimera/bird.wav"] = bytes(blown)
    updater(tmp_path, monkeypatch, tarball(files)).install(TAG)

    reasons = sc.verify_installed(tmp_path)
    assert any("хеш не совпал" in reason for reason in reasons)
    with pytest.raises(ValueError):
        sc.clip_for(tmp_path, "изобрази птицу Химерой")
    preserved(tmp_path)


def rollback_script(base: Path) -> str:
    """Текст сторожа v0.2.4, ``station_update.py``: метки здоровья нет — вернуть ``prev/`` и перезапустить."""
    return (
        f"sleep {su.WATCH_S}; [ -f {su.OK_MARK} ] && exit 0; "
        f"cp -a {base}/prev/. {base}/ && echo rollback >> /tmp/station.log; "
        "/etc/init.d/S99zzzrbxhost restart"
    )


def apply_rollback(base: Path) -> None:
    """Ровно шаг сторожа v0.2.4: ``cp -a prev/. base/``. Новые файлы выпуска он не удаляет."""
    script = rollback_script(base)
    copy = f"cp -a {base}/prev/. {base}/"
    assert copy in script and "S99zzzrbxhost restart" in script
    subprocess.run(["sh", "-c", copy], check=True)


def test_failed_start_rolls_back_and_answers(tmp_path, monkeypatch):
    """Неудачный старт: сторож v0.2.4 возвращает prev/, старый хост снова на месте, память цела."""
    installed_v024(tmp_path)
    (tmp_path / "station_host.py").write_text("def answer():\n    return 'v0.2.4 отвечает'\n", encoding="utf-8")
    before_settings = (tmp_path / "settings.json").read_bytes()
    before_memory = (tmp_path / "memory.db").read_bytes()
    up = updater(tmp_path, monkeypatch, tarball(release_bytes()))
    assert up.install(TAG).startswith("Обновляюсь")
    assert sc.verify_installed(tmp_path) == []

    apply_rollback(tmp_path)

    rolled = {}
    exec((tmp_path / "station_host.py").read_text(encoding="utf-8"), rolled)  # noqa: S102 — код теста, не колонки
    assert rolled["answer"]() == "v0.2.4 отвечает"
    assert (tmp_path / "VERSION").read_text(encoding="utf-8") == "v0.2.4\n"
    assert (tmp_path / "settings.json").read_bytes() == before_settings
    assert (tmp_path / "memory.db").read_bytes() == before_memory
    # cp -a не удаляет файлы, которых в prev/ не было: клип остаётся на диске. Объявить его нельзя —
    # код команды откатился вместе с хостом, а проверка читает VERSION.
    assert "station_chimera" not in (tmp_path / "station_host.py").read_text(encoding="utf-8")
    assert sc.chimera_available(tmp_path) is False


def test_updater_keeps_three_times_archive(tmp_path, monkeypatch):
    """Место считается как в v0.2.4: 3× архив + 2 МБ, иначе отказ и старые файлы на месте."""
    installed_v024(tmp_path)
    raw = tarball(release_bytes())
    up = updater(tmp_path, monkeypatch, raw)
    need = len(raw) * 3 + 2 * 2**20
    monkeypatch.setattr(su.shutil, "disk_usage", lambda p: type("U", (), {"free": need - 1})())

    phrase = up.install(TAG)

    assert "Не хватает места" in phrase
    assert (tmp_path / "station_host.py").read_text(encoding="utf-8") == "host-v0.2.4"
    assert not (tmp_path / "assets" / "chimera").exists()
    preserved(tmp_path)
    assert need == len(raw) * 3 + 2 * 2**20
