"""Жалоба в GitHub с колонки: «Игорёк, пожалуйся, что…» → issue с логом (Шифу 08.11).

Решает код, а не модель (ADR-0148): фразу разбирает :func:`report_command`, факты собирает :class:`Reporter`.
Оформление (заголовок и текст) делает модель: получает ФАКТЫ и :data:`ISSUE_PROMPT`, возвращает строгий JSON,
код проверяет его (:func:`_draft_problem`) — выдуманные числа, потерянная жалоба и битый JSON отбрасываются.
Не прошло — уходит шаблон кодом, жалоба не теряется. Значения ключей маскируются (:func:`mask_secrets`),
содержимое ``.env`` в тело не попадает.

Токен отдельный от обновлений: ``GH_ISSUE_TOKEN`` с правом ``Issues: Read and write`` только на ``REPO``.
``GITHUB_TOKEN`` (обновления, [station_update](station_update.py)) колонке не нужен вовсе.
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

REPO = os.environ.get("RBX_REPORT_REPO", "krikz/rob_box_station")
#: Та же жалоба второй раз в это окно не уходит («пожалуйся, пожалуйся, пожалуйся» — один issue).
COOLDOWN_S = float(os.environ.get("RBX_REPORT_COOLDOWN_S", "600"))
#: Между разными жалобами — короткая пауза: 08.11 вторая, другая жалоба через 2 мин получила «подожди 8 мин» и
#: потерялась (#16). Спам в репозиторий это всё равно режет.
GAP_S = float(os.environ.get("RBX_REPORT_GAP_S", "45"))
#: Сколько значимых строк лога кладём в issue и сколько символов всего (issue не должен быть свалкой).
MAX_LINES, MAX_CHARS = 120, 12000
MASK = "***"

LOG = Path(os.environ.get("RBX_LOG", "/tmp/station.log"))
#: Порядок — от старого прогона к текущему: лог сдвигается в ``.1`` при каждом старте хоста.
LOG_PATHS = [LOG.with_name(LOG.name + ".1"), LOG]

#: Строки лога, по которым видно, что случилось; остальное (анимации кольца, Improv) в issue не нужно.
MARKERS = ("🎤", "ход ", "STT", "429", "Traceback", "Error", "Exception", "ошибк", "🔑", "🔊", "⬆", "🔘", "ядро:")

#: Правила оформления issue — единственное место, где живёт это знание (ADR-0148: факты и решение в коде,
#: формулировки — за моделью). Модель получает ФАКТЫ и возвращает строгий JSON, код его проверяет.
ISSUE_PROMPT = """Ты оформляешь issue в GitHub по жалобе, которую человек сказал голосовой колонке.
Тебе дают ФАКТЫ: дословную жалобу, версию, настройки и строки лога.

Правила:
1. Ничего не выдумывай. Любое число, версия, имя и цитата — только из ФАКТОВ.
2. Жалобу приводи дословно, блоком цитаты (> ...), без правок и без смягчения формулировок.
3. Заголовок (title): начинается с «колонка: », не длиннее 70 символов, обрезка по границе слова,
   без кавычек, эмодзи и точки в конце.
4. Тело (body) — markdown из трёх разделов:
   ## Что случилось
   ## Что видно в логе
   ## Устройство
   В «Что видно в логе» — от 3 до 8 самых показательных строк из ФАКТОВ, в блоке ```.
   В «Устройство» — версия и настройки из ФАКТОВ.
5. Причину поломки не угадывай и починку не предлагай: только что произошло и что видно.
6. Ключей, токенов и паролей в тексте быть не должно (в ФАКТАХ их уже нет).
7. Ответ — строго JSON и больше ничего: {"title": "...", "body": "..."} — без ``` и без пояснений.
"""

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASS|PASSWORD|SALT)[A-Z0-9_]*)\s*[=:]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
)

_TRIGGER_RE = re.compile(
    r"(?i)\b(?:пожалуй(?:ся|тесь)|пожаловаться|жалуюсь|"
    r"(?:передай|передайте|скажи|сообщи)\s+(?:это\s+)?разработчик(?:у|ам|ами)?|"
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


def _parse_draft(answer: str) -> Tuple[str, str]:
    """Достать ``title``/``body`` из ответа модели: ``` и болтовня вокруг — не повод падать."""
    text = (answer or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rstrip("`").rstrip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("в ответе нет JSON")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("ответ модели — не объект")
    return str(data.get("title") or "").strip(), str(data.get("body") or "").strip()


def _draft_problem(title: str, body: str, complaint: str, facts: str) -> str:
    """Чем ответ модели не годен (пусто — годен): то, что нельзя проверять глазами каждый раз."""
    if not 8 <= len(title) <= 90 or "\n" in title:
        return f"заголовок не по правилам ({len(title)} символов)"
    if not 120 <= len(body) <= 6000:
        return f"тело не по правилам ({len(body)} символов)"
    if " ".join(complaint.split()) not in " ".join(body.split()):
        return "жалобы нет в теле дословно"
    invented = sorted(set(re.findall(r"\d+", title + " " + body)) - set(re.findall(r"\d+", facts)))
    if invented:
        return "числа не из фактов: " + ", ".join(invented[:5])
    return ""


def deepseek_formatter(log: Callable[[str], None]) -> Optional[Callable[[str], str]]:
    """Оформление issue моделью: синхронный вызов DeepSeek ключом колонки (``DEEPSEEK_API_KEY``).

    ``None`` — ключа нет (или пакет ``rob_box_llm`` не в сборке): тогда Reporter оформит шаблоном кодом.
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return None

    def call(facts: str) -> str:
        import asyncio  # noqa: PLC0415

        from rob_box_llm.provider import LLMMessage, LLMSettings  # noqa: PLC0415
        from rob_box_llm.providers.deepseek import DeepSeekProvider  # noqa: PLC0415

        async def run() -> str:
            provider = DeepSeekProvider(api_key=key, timeout=25.0)
            try:
                response = await provider.complete(
                    [LLMMessage(role="system", content=ISSUE_PROMPT), LLMMessage(role="user", content=facts)],
                    settings=LLMSettings(temperature=0.2, max_tokens=900),
                )
            finally:
                await provider.aclose()
            return response.content

        log("📮 оформляю issue моделью")
        return asyncio.run(run())

    return call


class Reporter:
    """Завести issue по жалобе; фразу колонке строит код (не модель)."""

    def __init__(self, base: Path, token: Callable[[], str], log: Callable[[str], None],
                 state_path: Optional[Path] = None, log_paths: Optional[Sequence[Path]] = None,
                 format_with: Optional[Callable[[str], str]] = None) -> None:
        self.base, self.token, self.log = base, token, log
        self.state_path = state_path or base / "report_state.json"
        self.log_paths: List[Path] = list(log_paths or LOG_PATHS)
        #: Оформление моделью: получает ФАКТЫ, возвращает ответ (JSON). ``None`` — сразу шаблон кодом.
        self.format_with = format_with

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

    def facts(self, complaint: str, with_log: bool) -> str:
        """ФАКТЫ для модели: ровно то, что видно колонке, без ключей и без домыслов."""
        settings = ", ".join(f"{name}: {value}" for name, value in self.context().items())
        parts = ["ЖАЛОБА (дословно):", complaint, "", f"ВЕРСИЯ КОЛОНКИ: {self.version()}",
                 f"НАСТРОЙКИ: {settings}"]
        if with_log:
            parts += ["", "ЗНАЧИМЫЕ СТРОКИ ЛОГА (хвост; время — по часам хоста):", self.excerpt() or "лог пуст"]
        return "\n".join(parts)

    def draft(self, complaint: str, with_log: bool) -> Tuple[str, str]:
        """(title, body): от модели, если её ответ прошёл проверку кодом; иначе шаблон — жалоба не теряется."""
        facts = self.facts(complaint, with_log)
        if self.format_with is not None:
            try:
                title, body = _parse_draft(self.format_with(facts))
                title, body = mask_secrets(title), mask_secrets(body)
                problem = _draft_problem(title, body, complaint, facts)
                if not problem:
                    return title, body
                self.log(f"📮 оформление моделью не прошло: {problem} — беру шаблон кодом")
            except Exception as exc:  # noqa: BLE001 — ответ модели не должен ломать жалобу
                self.log(f"📮 оформление моделью не вышло: {type(exc).__name__} — беру шаблон кодом")
        return self.title(complaint), self.body(complaint, with_log)

    @staticmethod
    def title(complaint: str) -> str:
        head = " ".join(complaint.split())
        if len(head) > 70:
            cut = head[:70].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
            head = cut or head[:70]
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
        busy = self.too_soon(complaint)
        if busy:
            return busy
        title, body = self.draft(complaint, with_log)
        return self.post(complaint, title, body)

    def too_soon(self, complaint: str) -> str:
        """Фраза отказа, если жалобу сейчас слать нельзя (та же — в окне ``COOLDOWN_S``, любая — в ``GAP_S``)."""
        now, state = time.time(), self.state()
        since = now - float(state.get("at") or 0)
        if state.get("hash") == self.digest(complaint) and since < COOLDOWN_S:
            return f"Эту жалобу уже отправил, номер {state.get('number', '?')}."
        if since < GAP_S:
            return f"Только что отправлял жалобу, подожди {int(GAP_S - since) + 1} секунд."
        return ""

    def file_by_model(self, complaint: str, title: str = "", details: str = "", with_log: bool = True) -> dict:
        """Тул модели ``report_issue``: модель даёт жалобу словами человека, заголовок и суть, код — остальное.

        Второго вызова LLM нет (тул класса ``query`` ждут 15 с): тело собирает код — цитата, суть от модели,
        версия, настройки, лог. Возвращает данные для модели (``status``, ``say``, номер), а не фразу.
        """
        token = self.token().strip()
        complaint = " ".join((complaint or "").split())
        if not complaint:
            return {"status": "rejected", "reason": "no_complaint",
                    "say": "Скажи, на что жаловаться, — я заведу issue."}
        if not token:
            return {"status": "rejected", "reason": "no_token",
                    "say": "Жалобу отправлять нечем: впиши токен гитхаба GH_ISSUE_TOKEN на странице настроек."}
        busy = self.too_soon(complaint)
        if busy:
            return {"status": "rejected", "reason": "cooldown", "say": busy}
        head = mask_secrets(" ".join((title or "").split()))
        head = self.title(head[len("колонка:"):].strip() if head.lower().startswith("колонка:") else head or complaint)
        parts = ["## Что случилось", "", f"> {complaint}", ""]
        if details.strip():
            parts += [mask_secrets(details.strip()), ""]
        parts += ["## Устройство", "", f"- версия колонки: `{self.version()}`"]
        parts += [f"- {name}: {value}" for name, value in self.context().items()]
        if with_log:
            parts += ["", "## Что видно в логе", "", "```", self.excerpt() or "лог пуст", "```"]
        parts += ["", "_Заведено инструментом модели `report_issue` по просьбе человека голосом._"]
        say = self.post(complaint, head, "\n".join(parts) + "\n")
        if not say.startswith("Завёл"):
            return {"status": "rejected", "reason": "github_failed", "say": say}
        return {"status": "done", "issue_number": self.state().get("number"), "say": say}

    def post(self, complaint: str, title: str, body: str) -> str:
        """POST issue → фраза колонке; удача запоминается (номер, хэш, время) для пауз."""
        token, now = self.token().strip(), time.time()
        try:
            status, answer = _post(f"https://api.github.com/repos/{REPO}/issues", token,
                                   {"title": title, "body": body})
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
