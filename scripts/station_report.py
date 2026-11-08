"""Жалоба в GitHub с колонки: «Игорёк, пожалуйся, что…» → issue с логом (Шифу 08.11).

Решает код, а не модель (ADR-0148): фразу разбирает :func:`report_command`, тело issue собирает :class:`Reporter`.
В issue уходят только факты — дословная жалоба, версия колонки, настройки и значимые строки её лога. Значения ключей
маскируются (:func:`mask_secrets`), содержимое ``.env`` в тело не попадает.

Токен отдельный от обновлений: ``GH_ISSUE_TOKEN`` с правом ``Issues: Read and write`` только на ``REPO``.
``GITHUB_TOKEN`` (обновления, [station_update](station_update.py)) остаётся read-only — токен на запись в домашней
колонке это риск, расширять его нельзя.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

REPO = os.environ.get("RBX_REPORT_REPO", "krikz/rob_box_project")
#: Пауза между жалобами: защита от «пожалуйся, пожалуйся, пожалуйся» и от спама в репозиторий.
COOLDOWN_S = float(os.environ.get("RBX_REPORT_COOLDOWN_S", "600"))
#: Сколько значимых строк лога кладём в issue и сколько символов всего (issue не должен быть свалкой).
MAX_LINES, MAX_CHARS = 120, 12000
MASK = "***"

LOG = Path(os.environ.get("RBX_LOG", "/tmp/station.log"))
#: Порядок — от старого прогона к текущему: лог сдвигается в ``.1`` при каждом старте хоста.
LOG_PATHS = [LOG.with_name(LOG.name + ".1"), LOG]

#: Строки лога, по которым видно, что случилось; остальное (анимации кольца, Improv) в issue не нужно.
MARKERS = ("🎤", "ход ", "STT", "429", "Traceback", "Error", "Exception", "ошибк", "🔑", "🔊", "⬆", "🔘", "ядро:")

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASS|PASSWORD|SALT)[A-Z0-9_]*)\s*[=:]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
)

_TRIGGER_RE = re.compile(
    r"(?i)\b(?:пожалуй(?:ся|тесь)|пожаловаться|передай\s+разработчику|"
    r"(?:заведи|заведите|завести|создай|создайте|оставь|оставьте|запости|отправь|напиши)\s+"
    r"(?:баг|багу|задачу|ишью|issue))\b"
)
_NO_LOG_RE = re.compile(r"(?i)\bбез\s+лога\b")
_WITH_LOG_RE = re.compile(r"(?i)\b(?:с\s+логом|приложи\s+лог)\b")


def mask_secrets(text: str) -> str:
    """Спрятать значения ключей, токенов, паролей в строке лога (имя ключа остаётся)."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}={MASK}" if m.groups() else MASK, text)
    return text


def report_command(text: str) -> Tuple[str, str]:
    """Фраза после вейка → (``file`` | ``bare`` | ``""``, жалоба словами пользователя).

    «пожалуйся», «заведи баг», «передай разработчику» — завести issue с логом; «без лога» — без строк лога;
    ничего из этого — ``("", "")``, дальше решает обычный ход.
    """
    match = _TRIGGER_RE.search(text or "")
    if match is None:
        return "", ""
    action = "bare" if _NO_LOG_RE.search(text) else "file"
    rest = text[:match.start()] + " " + text[match.end():]
    rest = _WITH_LOG_RE.sub(" ", _NO_LOG_RE.sub(" ", rest))
    return action, " ".join(rest.split()).strip(" .,;:—–-!?")


def _post(url: str, token: str, payload: dict, timeout: float = 20) -> Tuple[int, dict]:
    """POST в API GitHub → (код, разобранный ответ). Сеть и таймаут — исключением наружу."""
    import ssl  # noqa: PLC0415

    import certifi  # noqa: PLC0415

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "Authorization": f"Bearer {token}", "User-Agent": "rbx-station"},
    )
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:  # 401/403/422 — это ответ, а не сбой сети
        return exc.code, {}


class Reporter:
    """Завести issue по жалобе; фразу колонке строит код (не модель)."""

    def __init__(self, base: Path, token: Callable[[], str], log: Callable[[str], None],
                 state_path: Optional[Path] = None, log_paths: Optional[Sequence[Path]] = None) -> None:
        self.base, self.token, self.log = base, token, log
        self.state_path = state_path or base / "report_state.json"
        self.log_paths: List[Path] = list(log_paths or LOG_PATHS)

    def version(self) -> str:
        try:
            return (self.base / "VERSION").read_text(encoding="utf-8-sig").strip() or "без версии"
        except OSError:
            return "без версии"

    def context(self) -> dict:
        """Настройки для тела issue: подписи, а не ключи; значения из ``.env`` сюда не читаются."""
        return {
            "голос": os.environ.get("RBX_VOICE") or "по умолчанию",
            "громкость": os.environ.get("RBX_VOLUME", "—"),
            "своя модель вейка решает": os.environ.get("RBX_KWS_DECIDE", "0"),
            "часовой пояс лога": os.environ.get("TZ") or "не задан",
        }

    def log_lines(self) -> List[str]:
        """Значимые строки обоих файлов лога (старый прогон, потом текущий), секреты замаскированы."""
        lines: List[str] = []
        for path in self.log_paths:
            try:
                lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
        marked = [mask_secrets(line.rstrip()) for line in lines if any(k in line for k in MARKERS)]
        return marked[-MAX_LINES:]

    def excerpt(self) -> str:
        text = "\n".join(self.log_lines())
        return "…\n" + text[-MAX_CHARS:] if len(text) > MAX_CHARS else text

    def body(self, complaint: str, with_log: bool) -> str:
        settings = ", ".join(f"{name}: {value}" for name, value in self.context().items())
        parts = ["Жалоба с колонки, голосом (дословно, без правок):", "", f"> {complaint}", "",
                 f"- версия колонки: `{self.version()}`", f"- {settings}"]
        if with_log:
            parts += ["", "Значимые строки лога (хвост; время — по часам хоста):", "", "```",
                      self.excerpt() or "лог пуст", "```"]
        return "\n".join(parts) + "\n"

    @staticmethod
    def title(complaint: str) -> str:
        head = " ".join(complaint.split())[:70]
        return f"колонка: {head}" if head else "колонка: жалоба без слов"

    @staticmethod
    def digest(complaint: str) -> str:
        norm = " ".join(complaint.lower().split())
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, state: dict) -> None:
        try:
            self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self.log(f"📮 состояние жалоб не сохранено: {exc}")

    def file(self, complaint: str, with_log: bool = True) -> str:
        """Завести issue по жалобе. Возвращает фразу колонке.

        Без жалобы и без токена — говорит, чего не хватает; пауза ``COOLDOWN_S`` не даёт залить репозиторий
        одинаковыми issue, а та же жалоба в это окно второй раз не уходит.
        """
        token = self.token().strip()
        complaint = " ".join((complaint or "").split())
        if not complaint:
            return "Скажи, на что жаловаться: «Игорёк, пожалуйся, что не слышишь меня»."
        if not token:
            return "Жалобу отправлять нечем: впиши токен гитхаба на странице настроек."
        now, state = time.time(), self.state()
        wait = COOLDOWN_S - (now - float(state.get("at") or 0))
        if wait > 0:
            if state.get("hash") == self.digest(complaint):
                return f"Эту жалобу уже отправил, номер {state.get('number', '?')}."
            return f"Только что отправлял жалобу, подожди {int(wait // 60) + 1} мин."
        try:
            status, answer = _post(f"https://api.github.com/repos/{REPO}/issues", token,
                                   {"title": self.title(complaint), "body": self.body(complaint, with_log)})
        except Exception as exc:  # noqa: BLE001 — сеть не роняет хост (07.11: тайм-аут уже ронял)
            self.log(f"📮 жалоба не ушла: {type(exc).__name__}: {exc}")
            return "Жалоба не ушла: гитхаб не ответил."
        if status not in (200, 201):
            self.log(f"📮 жалоба не ушла: HTTP {status}")
            return f"Жалоба не ушла: гитхаб ответил {status}."
        number, issue = answer.get("number"), str(answer.get("html_url") or "")
        self.save({"at": now, "hash": self.digest(complaint), "number": number, "issue": issue})
        self.log(f"📮 жалоба заведена: {issue}")
        # ссылку голосом не читаем (MiniMax произносит её по буквам) — она в логе и в состоянии жалоб
        return f"Завёл issue номер {number}." if number else "Завёл issue в проекте."


def _selftest() -> None:  # pragma: no cover — ручная проверка на колонке
    reporter = Reporter(Path(os.environ.get("RBX_BASE", "/data/opt/rbx")), lambda: os.environ.get("GH_ISSUE_TOKEN", ""),
                        print)
    print(report_command("пожалуйся, что не слышишь меня"))
    print(reporter.body("не слышишь меня", True))


if __name__ == "__main__":  # pragma: no cover
    _selftest()
