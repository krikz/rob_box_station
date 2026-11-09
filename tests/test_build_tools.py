"""Сборщики ассетов установки: ядро из коммита, рунтайм по lock, модель вейка (#4547, #4549, #4552 rob_box_project)."""
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_core as bc  # noqa: E402
import build_kws as bk  # noqa: E402
import build_runtime as br  # noqa: E402

ENTRIES = ["src/pkg/pkg/", "scripts/dialogue/chat.py", "migrations/"]


def test_wanted_filters_tests_cache_docs_env():
    assert bc.wanted("src/pkg/pkg/mod.py", ENTRIES)
    assert bc.wanted("src/pkg/pkg/data/phrases.yaml", ENTRIES)
    assert bc.wanted("scripts/dialogue/chat.py", ENTRIES)
    assert not bc.wanted("src/pkg/pkg/tests/test_x.py", ENTRIES)
    assert not bc.wanted("src/pkg/pkg/__pycache__/m.cpython-312.pyc", ENTRIES)
    assert not bc.wanted("src/pkg/pkg/README.md", ENTRIES)
    assert not bc.wanted("scripts/dialogue/.env", ENTRIES)
    assert not bc.wanted("scripts/dialogue/run.sh", ENTRIES)
    assert not bc.wanted("src/pkg/pkg2/x.py", ENTRIES)


def test_manifest_covers_what_host_imports():
    names = bc.manifest()
    assert "scripts/dialogue/chat.py" in names and "scripts/dialog/dry_stand.py" in names
    assert "src/rob_box_voice/prompts/master_prompt_compact.txt" in names
    assert all(f"src/{p}/{p}/" in names for p in ("rob_box_core", "rob_box_dialog", "rob_box_harness", "rob_box_llm",
                                                   "rob_box_voice", "rob_box_mcp_tools"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "rbp"
    for rel, text in (("src/pkg/pkg/mod.py", "x = 1\n"), ("src/pkg/pkg/tests/test_mod.py", "t\n"),
                      ("scripts/dialogue/chat.py", "chat\n"), ("scripts/dialogue/.env", "KEY=secret\n"),
                      ("migrations/001_init.sql", "create;\n"), ("other/big.bin", "zz\n")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode())
    _git(root.parent, "init", "-q", str(root))
    _git(root, "-c", "core.autocrlf=false", "add", "-A", "-f")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return root


def test_core_bundle_from_commit_is_deterministic(repo, tmp_path):
    a, b = tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"
    info = bc.build(repo, a, "HEAD", ENTRIES)
    bc.build(repo, b, "HEAD", ENTRIES)

    assert a.read_bytes() == b.read_bytes()
    with tarfile.open(a) as t:
        names = t.getnames()
        source = t.extractfile("rbx/core/SOURCE").read().decode()
    assert names == sorted(n for n in names if n != "rbx/core/SOURCE") + ["rbx/core/SOURCE"]
    assert set(names) == {"rbx/core/migrations/001_init.sql", "rbx/core/scripts/dialogue/chat.py",
                          "rbx/core/src/pkg/pkg/mod.py", "rbx/core/SOURCE"}
    assert f"commit {info['commit']}" in source and info["files"] == 3


def test_core_bundle_takes_commit_not_worktree(repo, tmp_path):
    """Правка в рабочем дереве (как CRLF-копия на колонке) в бандл не попадает — только то, что в коммите."""
    (repo / "src/pkg/pkg/mod.py").write_text("x = 2  # не закоммичено\n", encoding="utf-8")
    out = tmp_path / "c.tar.gz"
    bc.build(repo, out, "HEAD", ENTRIES)
    with tarfile.open(out) as t:
        assert t.extractfile("rbx/core/src/pkg/pkg/mod.py").read() == b"x = 1\n"


def _file(name: str, kind: str = "bdist_wheel") -> dict:
    return {"filename": name, "packagetype": kind}


def test_pick_prefers_armv7_cp312_within_station_glibc():
    files = [_file("jiter-1-cp312-cp312-manylinux_2_34_armv7l.whl"),
             _file("jiter-1-cp312-cp312-manylinux2014_armv7l.manylinux_2_17_armv7l.whl"),
             _file("jiter-1-cp312-cp312-manylinux2014_aarch64.whl"), _file("jiter-1.tar.gz", "sdist")]

    assert br._pick(files)["filename"] == "jiter-1-cp312-cp312-manylinux2014_armv7l.manylinux_2_17_armv7l.whl"


def test_pick_rejects_too_new_glibc_and_falls_back():
    """glibc колонки 2.30: колесо manylinux_2_31 не загрузится — лучше чистое колесо или sdist."""
    files = [_file("x-1-cp312-cp312-manylinux_2_31_armv7l.whl"), _file("x-1.tar.gz", "sdist")]
    assert br._pick(files)["filename"] == "x-1.tar.gz"
    files.append(_file("x-1-py3-none-any.whl"))
    assert br._pick(files)["filename"] == "x-1-py3-none-any.whl"


def test_lock_pins_python_and_every_package():
    rows = br.read_lock()
    assert rows[0][0] == "python" and rows[0][2] == br.PYTHON[1]
    pinned = {r[0] for r in rows[1:]}
    assert pinned == set(br.PINS)
    assert all(len(r[2]) == 64 for r in rows)


def test_kws_bundle_rejects_non_arm(tmp_path):
    fake = tmp_path / "kws_run"
    fake.write_bytes(b"\x7fELF" + b"\x00" * 14 + bytes([62]) + b"\x00" * 40)  # x86-64
    with pytest.raises(SystemExit):
        bk.build(fake, tmp_path / "k.tar.gz")


def test_kws_bundle_is_deterministic_and_executable(tmp_path):
    arm = tmp_path / "kws_run"
    arm.write_bytes(b"\x7fELF" + b"\x00" * 14 + bytes([40]) + b"\x00" * 40)
    model = tmp_path / "m.bin"
    model.write_bytes(b"weights")
    a, b = tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"
    assert bk.build(arm, a, model) == bk.build(arm, b, model)
    with tarfile.open(a) as t:
        assert t.getmember("rbx/kws/kws_run").mode == 0o755
        assert t.extractfile("rbx/kws/kws_model.bin").read() == b"weights"


def test_committed_model_is_v3():
    """Модель в репозитории — v3 08.11 (на отложенных живых записях 100 % вейка против 58 % у v2)."""
    import hashlib

    assert hashlib.md5(bk.MODEL.read_bytes()).hexdigest() == "6504b4e8dea597f9e7f6e73995d0e8e2"  # noqa: S324
