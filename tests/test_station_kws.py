"""Сбор живых примеров вейка: промахи облака — в ``miss``, «чужое» — только если следом не звали, у видов свои квоты.

09.11 на 10.1.1.13 сбор стоял с 08.11: общий кап 10 МБ съели «чужие» (264 против 14 «вейков»), а попытки, где
облако «Игорька» не разобрало, не сохранялись вовсе.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_kws as sk  # noqa: E402

WAKE = ["игорёк", "игорек"]
PCM = b"\x01\x00" * 16000 * 3  # 3 с


def _files(folder: Path, kind: str) -> list:
    return sorted((folder / kind).glob("*.wav")) if (folder / kind).exists() else []


def test_wake_position():
    assert sk.wake_position("Игорёк, который час?", WAKE) == "start"
    assert sk.wake_position("ну игорек давай", WAKE) == "inside"
    assert sk.wake_position("который час", WAKE) == "none"
    assert sk.wake_position("", WAKE) == "empty"


def test_pos_saved_with_index(tmp_path):
    kws = sk.KwsCollector(tmp_path, print)
    assert kws.observe(PCM, "Игорёк, ты здесь?", WAKE, "igorek", 0.97, "minimax", now=100) == "pos"

    assert len(_files(tmp_path, "pos")) == 1
    row = json.loads((tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "pos" and row["text"] == "Игорёк, ты здесь?" and row["kws"] == "igorek"


def test_failed_tries_before_wake_become_miss_not_neg(tmp_path):
    """«Горек, который час» → «» → «Игорёк, который час»: две первые — попытки позвать, а не «чужое»."""
    kws = sk.KwsCollector(tmp_path, print)
    kws.observe(PCM, "Горек, который час", WAKE, "_unknown_", 0.6, now=100)
    kws.observe(PCM, "", WAKE, "_unknown_", 0.3, now=104)
    kws.observe(PCM, "Игорёк, который час", WAKE, "igorek", 0.9, now=108)

    assert kws.promote_misses(now=108) == 2
    assert len(_files(tmp_path, "miss")) == 2
    assert _files(tmp_path, "neg") == []


def test_neg_only_after_window_without_wake(tmp_path):
    kws = sk.KwsCollector(tmp_path, print)
    assert kws.observe(PCM, "передай соль", WAKE, now=100) == "pending"
    assert _files(tmp_path, "neg") == []
    kws.observe(PCM, "и ещё хлеб", WAKE, now=100 + sk.MISS_WINDOW_S + 1)  # первая состарилась

    assert len(_files(tmp_path, "neg")) == 1


def test_empty_text_with_kws_wake_is_miss_immediately(tmp_path):
    kws = sk.KwsCollector(tmp_path, print)
    assert kws.observe(PCM, "", WAKE, "igorek", 0.8, now=100) == "miss"
    assert kws.promote_misses(now=105) == 0  # второй раз не сохраняется


def test_empty_noise_is_forgotten(tmp_path):
    kws = sk.KwsCollector(tmp_path, print)
    kws.observe(PCM, "", WAKE, "_unknown_", 0.9, now=100)
    kws.settle(now=200)

    assert not any(_files(tmp_path, k) for k in ("pos", "neg", "miss"))


def test_full_neg_does_not_block_pos(tmp_path, monkeypatch):
    """Свой потолок у каждого вида: забитый «чужими» каталог больше не останавливает сбор вейка."""
    monkeypatch.setenv("RBX_KWS_CAP_NEG_MB", "0")
    logs = []
    kws = sk.KwsCollector(tmp_path, logs.append)
    kws.observe(PCM, "чужая фраза", WAKE, now=100)
    kws.settle(now=200)
    kws.settle(now=300)
    assert kws.observe(PCM, "Игорёк, привет", WAKE, now=300) == "pos"

    assert _files(tmp_path, "neg") == [] and len(_files(tmp_path, "pos")) == 1
    assert sum("neg больше не копится" in line for line in logs) == 1


def test_counts_existing_files(tmp_path, monkeypatch):
    (tmp_path / "pos").mkdir()
    (tmp_path / "pos" / "real__1.wav").write_bytes(b"x" * 2000)
    monkeypatch.setenv("RBX_KWS_CAP_POS_MB", "0.001")  # ~1 КБ — уже полно

    assert sk.KwsCollector(tmp_path, print).observe(PCM, "Игорёк, да", WAKE) == ""
