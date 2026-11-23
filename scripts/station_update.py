"""Обновление колонки с GitHub по тегу (Шифу 07.11: «обновление колонки с гита», голосом «проверь обновление»).

Тянется код колонки — релиз (тег) ``krikz/rob_box_station``: ``scripts/*.py``, ``assets/``. Промпт и настройки
пользователя (``prompts/``, ``settings.json``, ``.env``) не трогаются. Токен не нужен: репозиторий публичный, а
негодный токен не мешает — чтение повторяется анонимно (``_get``). Токен на чтение (``GITHUB_TOKEN`` в ``.env``)
нужен, только если репозиторий закроют.

Перед установкой — проверка места: свободно не меньше 3× архива + 2 МБ, иначе отказ словами.
Порядок: старые файлы → ``prev/``, новые — на место, ``VERSION`` = тег, перезапуск хоста. Сторож (отдельный процесс,
``setsid``) взводится хостом **в момент перезапуска** (``arm_watchdog``) и через ``WATCH_S`` проверяет метку
``/tmp/rbx_update_ok`` (хост ставит её, когда слушает и прожил 30 с); нет метки — возвращает ``prev/`` и
перезапускает хост. При старте сверяем ядро с пином тега и, если нужно, берём архив из релиза.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Callable, Optional, Tuple

REPO = os.environ.get("RBX_UPDATE_REPO", "krikz/rob_box_station")
OK_MARK = Path("/tmp/rbx_update_ok")
WATCH_S = 90
CORE_ASSET = "station-core.tar.gz"
CORE_ENV = Path("scripts/dialogue/.env")
CORE_STAGE_S = 30  # оставляем время на старт и 30 с проверки старому сторожу
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

    req = urllib.request.Request(url, headers={k: v for k, v in headers.items() if k != "Authorization"})
    if "Authorization" in headers:
        req.add_unredirected_header("Authorization", headers["Authorization"])
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as r:
        return r.read()


def _core_ref(core: Path) -> str:
    try:
        lines = (core / "SOURCE").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    return next((line.removeprefix("ref ") for line in lines if line.startswith("ref ")), "")


def _extract_core(raw: bytes, target: Path, ref: str) -> None:
    """Распаковать только обычные файлы бандла в отдельный каталог."""
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        members = tar.getmembers()
        if shutil.disk_usage(target.parent).free < sum(m.size for m in members) + 2 * 2**20:
            raise OSError("не хватает места для ядра")
        for member in members:
            rel = PurePosixPath(member.name)
            if (not member.isfile() or rel.parts[:2] != ("rbx", "core") or len(rel.parts) < 3
                    or ".." in rel.parts or "\\" in member.name or ":" in member.name or rel.name == ".env"):
                raise ValueError(f"недопустимый файл ядра: {member.name}")
            dest = target.joinpath(*rel.parts[2:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as incoming, dest.open("xb") as out:
                shutil.copyfileobj(incoming, out)
    if _core_ref(target) != ref or not (target / "scripts/dialogue/chat.py").is_file():
        raise ValueError(f"архив ядра не соответствует пину {ref}")


class Updater:
    def __init__(self, base: Path, token: Callable[[], str], log: Callable[[str], None]) -> None:
        self.base, self.token, self.log = base, token, log

    def current(self) -> str:
        try:
            return (self.base / "VERSION").read_text(encoding="utf-8").strip() or "без версии"
        except OSError:
            return "без версии"

    def sync_core(self) -> bool:
        """На первом старте после OTA поставить ядро именно из релиза запущенного хоста."""
        pin = self.base / "core_ref.txt"
        if not pin.is_file():  # установленный вручную стенд без пина
            return False
        ref = pin.read_text(encoding="utf-8").strip()
        if not ref:
            raise ValueError("пустой пин ядра")
        core, previous = self.base / "core", self.base / "core.prev"
        if not core.exists() and _core_ref(previous):
            previous.rename(core)  # прерывание между двумя rename
        if _core_ref(core) == ref:
            return False
        if _core_ref(previous) == ref:
            return self.restore_core()
        began = monotonic()
        version = self.current()
        tag = urllib.parse.quote(version, safe="")
        root = f"https://api.github.com/repos/{REPO}/releases"
        token = self.token()
        release = json.loads(_get(f"{root}/tags/{tag}", token))
        asset = next((asset for asset in release["assets"] if asset["name"] == CORE_ASSET), None)
        if asset is None:
            raise ValueError(f"в релизе {tag} нет {CORE_ASSET}")
        raw = _get(f"{root}/assets/{int(asset['id'])}", token, "application/octet-stream")
        if asset.get("digest") != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ValueError("SHA-256 архива ядра не совпадает с релизом")
        with tempfile.TemporaryDirectory(prefix=".core-stage-", dir=self.base) as stage:
            candidate = Path(stage) / "core"
            _extract_core(raw, candidate, ref)
            if monotonic() - began > CORE_STAGE_S or self.current() != version:
                raise TimeoutError("окно обновления ядра истекло")
            self._switch_core(candidate)
        self.log(f"⬆ ядро диалога: {ref} из {tag}")
        return True

    def restore_core(self) -> bool:
        previous = self.base / "core.prev"
        if not _core_ref(previous):
            return False
        self._switch_core(previous)
        self.log(f"⬆ ядро диалога: возвращено {_core_ref(self.base / 'core')}")
        return True

    def _switch_core(self, candidate: Path) -> None:
        core, previous = self.base / "core", self.base / "core.prev"
        env = (core if core.exists() else previous) / CORE_ENV
        if env.is_file() and env != candidate / CORE_ENV:
            shutil.copy2(env, candidate / CORE_ENV)
        with tempfile.TemporaryDirectory(prefix=".core-swap-", dir=self.base) as scratch:
            saved = Path(scratch) / "previous"
            if previous.exists():
                previous.rename(saved)
            try:
                if core.exists():
                    core.rename(previous)
                (saved if candidate == previous else candidate).rename(core)
            except OSError:
                if not core.exists() and previous.exists():
                    previous.rename(core)
                if saved.exists():
                    saved.rename(previous)
                raise

    def latest(self) -> Tuple[Optional[str], str]:
        """(новый тег или None, фраза) для опубликованного релиза с загруженным ядром."""
        token = self.token()
        try:
            releases = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases?per_page=20", token))
        except Exception as exc:  # noqa: BLE001
            self.log(f"⬆ проверка обновления: {type(exc).__name__}: {exc}")
            if not token and "404" in str(exc):  # закрытый репо без токена GitHub отвечает 404
                return None, "Репозиторий закрытый: для обновлений впиши токен гитхаба на странице настроек."
            if getattr(exc, "code", None) in (401, 403):
                return None, "Гитхаб не принял токен: очисти поле с токеном или впиши рабочий."
            return None, "Не смог проверить обновление: гитхаб не ответил."
        names = [r["tag_name"] for r in releases
                 if not (r.get("draft") or r.get("prerelease")) and r.get("tag_name", "").startswith("v")
                 and any(a.get("name") == CORE_ASSET and a.get("state") == "uploaded" and a.get("digest")
                         for a in r.get("assets", ()))]
        if not names:
            return None, "Опубликованных обновлений с ядром пока нет."

        newest = max(names, key=version_key)
        current = self.current()
        if not newer_than(newest, current):
            return None, f"Обновлений нет, у меня версия {current}."
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


def version_key(tag: str) -> list[int]:
    """Числа тега ``vX.Y.Z`` для сравнения. Нечисловая часть — ноль, не догадка о порядке."""
    return [int(part) if part.isdigit() else 0 for part in tag[1:].split(".")]


def newer_than(candidate: str, current: str) -> bool:
    """``True``, только если опубликованный тег новее установленного. Совпал или старше — не катим."""
    if not candidate.startswith("v") or not current.startswith("v"):
        return False
    return version_key(candidate) > version_key(current)


def offer_suffix(tag: "Optional[str]", command: str) -> str:
    """Хвост фразы «проверь»: код предлагает поставить, только если тег новее. Иначе пусто."""
    return " Скажи «Игорёк, обновись», чтобы поставить." if tag and command == "check" else ""


def install_phrase(installed: str) -> str:
    """Фраза после попытки поставить. Пустой результат установки — отказ, не «обновился»."""
    return installed or "Не смог скачать обновление, попробуй позже."


def should_restart(installed: str) -> bool:
    """Перезапуск только если установка сама сказала, что поставила. Отказ и пусто — хост живёт дальше."""
    return installed.startswith("Обновляюсь")


def update_command(text: str) -> str:
    """«проверь обновление» → ``check`` (сказать, что нового, не ставить); «обновись» → ``install``; иначе ``""``.
    Шифу 07.11: сначала Игорёк проверяет и рассказывает, что нового, ставит — по отдельной просьбе."""
    low = text.lower().replace("ё", "е")
    if "обнов" not in low:
        return ""
    if any(w in low for w in ("обновись", "обновляйся", "поставь обновлен", "установи обновлен", "обнови себя")):
        return "install"
    return "check" if any(w in low for w in ("провер", "есть ли", "какие", "что нового")) else ""
