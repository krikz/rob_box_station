"""install.sh: глушение Алисы и старт хоста по ssh не должны умирать вместе с ssh-сессией
(SIGHUP, krikz/rob_box_project#4551): небезопасный фоновый запуск запрещён, глушение — с
проверкой ps и повтором, старт хоста — отсоединённым (setsid/nohup) с перенаправлением в /dev/null."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
YANDEXOFF = (ROOT / "scripts" / "S99zzyandexoff").read_text(encoding="utf-8")


def _line_with(text: str, haystack: str = INSTALL) -> str:
    hits = [ln for ln in haystack.splitlines() if text in ln]
    assert hits, f"не нашёл «{text}»"
    return hits[0]


def test_no_background_yandexoff_start_over_ssh():
    # именно этот запуск умирал с сессией ssh и оставлял Алису с занятым микрофоном (#4551)
    assert "S99zzyandexoff start" not in INSTALL
    now = _line_with("S99zzyandexoff now")
    assert "SSHN" in now  # одиночный вызов — только ssh -n (stdin — остаток скрипта)


def test_suppression_checks_ps_and_retries():
    now = _line_with("S99zzyandexoff now")
    assert "||" in now  # не заглушилась — второй заход
    body = YANDEXOFF.split("now)", 1)[1]
    assert "ps" in body and "quasar_launcher2" in body and "maind" in body  # критерий «ушла» по ps
    assert "-eq 0" in body  # Алиса жива — неудача по коду возврата


def test_host_start_detached_from_ssh_session():
    # тот же класс бага: фоновый цикл хоста — потомок ssh-сессии и умирает с ней по SIGHUP
    line = _line_with("S99zzzrbxhost restart")
    assert "setsid" in line or "nohup" in line, "старт хоста не отсоединён от ssh-сессии"
    assert "</dev/null" in line and ">/dev/null" in line and "2>&1" in line


def test_shell_syntax():
    for f in ("install.sh", "scripts/S99zzyandexoff", "scripts/S99zzzrbxhost"):
        assert subprocess.run(["sh", "-n", str(ROOT / f)]).returncode == 0, f
