"""Обновление колонки с GitHub по тегу (Шифу 07.11: «обновление колонки с гита», голосом «проверь обновление»).

Тянется код колонки — релиз (тег) ``krikz/rob_box_station``: ``scripts/*.py``, ``assets/``. Промпт и настройки
пользователя (``prompts/``, ``settings.json``, ``.env``) не трогаются. Токен не нужен: репозиторий публичный, а
негодный токен не мешает — чтение повторяется анонимно (``_get``). Токен на чтение (``GITHUB_TOKEN`` в ``.env``)
нужен, только если репозиторий закроют.

Перед установкой — проверка места: свободно не меньше 3× архива + 2 МБ, иначе отказ словами.
Порядок: старые файлы → ``prev/``, новые — на место, ``VERSION`` = тег, перезапуск хоста. Сторож (отдельный процесс,
``setsid``) взводится хостом **в момент перезапуска** (``arm_watchdog``) и через ``WATCH_S`` проверяет метку
``/tmp/rbx_update_ok`` (хост ставит её, когда слушает и прожил 30 с); нет метки — возвращает ``prev/`` и
перезапускает хост. Ядро (``core/``) — пока отдельно (ADR-0167 Ш2).
"""
import io
import json
import os
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

REPO = os.environ.get("RBX_UPDATE_REPO", "krikz/rob_box_station")
OK_MARK = Path("/tmp/rbx_update_ok")
WATCH_S = 90
UPDATED = ("scripts/", "assets/")  # что берётся из релиза (scripts/*.py кладутся в корень установки)


def _get(url: str, token: str, accept: str = "application/vnd.github+json") -> bytes:
    """Прочитать URL с GitHub. Негодный токен не валит чтение: публичный репозиторий отдаётся и без него."""
    headers = {"Accept": accept, "User-Agent": "rbx-station"}
    if token:  # закрытый репо — токен на чтение; открытый — без токена (лимит GitHub 60 запросов в час)
        headers["Authorization"] = f"Bearer {token}"
    try:
        return _read(url, headers)
    except urllib.error.HTTPError as exc:
        # 08.11: в поле токена оказался отозванный токен — GitHub отвечал 401, и обновление «не проверялось».
        # Публичный репозиторий читается анонимно, поэтому повторяем без заголовка; закрытый отдаст 404.
        if not token or exc.code not in (401, 403):
            raise
        return _read(url, {"Accept": accept, "User-Agent": "rbx-station"})


def _read(url: str, headers: dict) -> bytes:
    import ssl  # noqa: PLC0415

    import certifi  # noqa: PLC0415

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as r:
        return r.read()


class Updater:
    def __init__(self, base: Path, token: Callable[[], str], log: Callable[[str], None]) -> None:
        self.base, self.token, self.log = base, token, log

    def current(self) -> str:
        try:
            return (self.base / "VERSION").read_text(encoding="utf-8").strip() or "без версии"
        except OSError:
            return "без версии"

    def latest(self) -> Tuple[Optional[str], str]:
        """(новый тег или None, фраза). Тег — последний по дате коммита среди тегов ``v*``."""
        token = self.token()
        try:
            tags = json.loads(_get(f"https://api.github.com/repos/{REPO}/tags?per_page=20", token))
        except Exception as exc:  # noqa: BLE001
            self.log(f"⬆ проверка обновления: {type(exc).__name__}: {exc}")
            if not token and "404" in str(exc):  # закрытый репо без токена GitHub отвечает 404
                return None, "Репозиторий закрытый: для обновлений впиши токен гитхаба на странице настроек."
            if getattr(exc, "code", None) in (401, 403):
                return None, "Гитхаб не принял токен: очисти поле с токеном или впиши рабочий."
            return None, "Не смог проверить обновление: гитхаб не ответил."
        names = [t["name"] for t in tags if str(t.get("name", "")).startswith("v")]
        if not names:
            return None, "Релизов пока нет."
        newest = max(names, key=lambda n: [int(x) if x.isdigit() else 0 for x in n[1:].split(".")])
        if newest == self.current():
            return None, f"Обновлений нет, у меня последняя версия {newest}."
        notes = self.notes(newest)
        return newest, (f"Есть новая версия {newest}, у меня {self.current()}."
                        + (f" Что нового: {notes}" if notes else ""))

    def notes(self, tag: str) -> str:
        """«Что нового» — описание аннотированного тега (пишется для людей, Шифу 07.11); нет — пусто."""
        try:
            ref = json.loads(_get(f"https://api.github.com/repos/{REPO}/git/ref/tags/{tag}", self.token()))
            if ref.get("object", {}).get("type") != "tag":
                return ""
            obj = json.loads(_get(ref["object"]["url"], self.token()))
            return " ".join(str(obj.get("message", "")).split())[:400]
        except Exception as exc:  # noqa: BLE001
            self.log(f"⬆ описание {tag} не прочитано: {type(exc).__name__}")
            return ""

    def install(self, tag: str) -> str:
        """Скачать тег, отложить старое в ``prev/``, поставить новое, взвести сторожа отката."""
        try:
            raw = _get(f"https://api.github.com/repos/{REPO}/tarball/{tag}", self.token(), "application/vnd.github.tar")
        except Exception as exc:  # noqa: BLE001 — сеть не роняет хост (07.11: тайм-аут codeload уронил хост)
            self.log(f"⬆ {tag} не скачана: {type(exc).__name__}: {exc}")
            return ""
        need = len(raw) * 3 + 2 * 2**20  # архив + старая копия (prev/) + новые файлы, с запасом
        free = shutil.disk_usage(self.base).free
        if free < need:  # Шифу 07.11: «надо проверять, что места хватает»
            self.log(f"⬆ {tag} не поставлена: свободно {free // 2**20} МБ, нужно {need // 2**20} МБ")
            return f"Не хватает места для обновления: нужно {need // 2**20} мегабайт, свободно {free // 2**20}."
        prev = self.base / "prev"
        shutil.rmtree(prev, ignore_errors=True)
        prev.mkdir()
        placed = []
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
            for m in tar.getmembers():
                rel = m.name.split("/", 1)[1] if "/" in m.name else ""
                if not m.isfile() or not rel.startswith(UPDATED) or ".." in rel:
                    continue
                dest = self.base / (rel[len("scripts/"):] if rel.startswith("scripts/") else rel)
                if dest.exists():
                    (prev / dest.relative_to(self.base)).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, prev / dest.relative_to(self.base))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(tar.extractfile(m).read())
                placed.append(str(dest.relative_to(self.base)))
        if (self.base / "VERSION").exists():
            shutil.copy2(self.base / "VERSION", prev / "VERSION")
        (self.base / "VERSION").write_text(tag + "\n", encoding="utf-8")
        OK_MARK.unlink(missing_ok=True)
        self.log(f"⬆ поставлена {tag}: {len(placed)} файлов, старое — в prev/")
        return f"Обновляюсь до {tag}, через минуту вернусь."

    def arm_watchdog(self) -> None:
        """Взвести сторожа отката. Зовёт хост в момент перезапуска, а не распаковка.

        08.11 на 10.1.1.141: сторож спал с момента распаковки и за 90 с не дождался метки — потому что в это же
        окно попала озвучка «Обновляюсь до …» (56 с на тормозящем MiniMax), перезапуск и 30 с до метки. Откат
        съел удачную установку. Теперь окно начинается там, где колонка уже всё сказала.
        """
        self._watchdog()

    def _watchdog(self) -> None:
        """Отдельный процесс: нет метки здоровья за ``WATCH_S`` от перезапуска — вернуть ``prev/`` и перезапустить."""
        script = (f"sleep {WATCH_S}; [ -f {OK_MARK} ] && exit 0; "
                  f"cp -a {self.base}/prev/. {self.base}/ && echo \"$(date) откат обновления\" >> /tmp/station.log; "
                  "/etc/init.d/S99zzzrbxhost restart")
        subprocess.Popen(["setsid", "sh", "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def update_command(text: str) -> str:
    """«проверь обновление» → ``check`` (сказать, что нового, не ставить); «обновись» → ``install``; иначе ``""``.
    Шифу 07.11: сначала Игорёк проверяет и рассказывает, что нового, ставит — по отдельной просьбе."""
    low = text.lower().replace("ё", "е")
    if "обнов" not in low:
        return ""
    if any(w in low for w in ("обновись", "обновляйся", "поставь обновлен", "установи обновлен", "обнови себя")):
        return "install"
    return "check" if any(w in low for w in ("провер", "есть ли", "какие", "что нового")) else ""
