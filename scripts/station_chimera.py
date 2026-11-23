"""CHIMERA на колонке: один локальный клип на явную команду, без синтеза и без сети.

Клипы приезжают обновлением в ``assets/chimera/`` (обновлятор v0.2.4 берёт из тега только ``scripts/`` и
``assets/``, ``station_update.py``). Что можно сыграть — решает манифест рядом с файлами: нет файла, битый
хеш или чужой формат — команда честно отказывается, а не молчит и не подменяет другой звук.
"""

import hashlib
import json
import wave
from pathlib import Path

PROFILE_NAME = "chimera.json"
CLIPS_DIR = "chimera"
RATE = 16000
# «Игорёк, изобрази птицу Химерой» — ровно один клип. Другие реакции в пакете есть, но этой фразой не играют.
BIRD_PHRASE = "изобрази птицу химерой"
BIRD_CLIP = "bird.wav"


def phrase_key(text: str) -> str:
    """Фраза к сравнению: нижний регистр, «ё» → «е», без знаков по краям."""
    return text.lower().replace("ё", "е").strip(" \t\n\r,.!?;:«»\"'")


def load_profile(base: Path) -> dict:
    """Профиль команд из ``assets/chimera.json``. Битый или чужой — честный отказ, не догадка."""
    path = base / "assets" / PROFILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"профиль CHIMERA не читается: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("commands"), dict):
        raise ValueError("профиль CHIMERA: нет таблицы commands")
    return data


def clip_for(base: Path, text: str) -> "tuple[Path, dict] | None":
    """Клип команды или ``None``, если фраза не из профиля. Битый набор — ``ValueError``, не тишина."""
    key = phrase_key(text)
    if not key:
        return None
    profile = load_profile(base)
    found = [name for phrase, name in profile["commands"].items() if phrase_key(phrase) == key]
    if not found:
        return None
    if len(found) != 1:
        raise ValueError(f"фраза «{key}» указывает на {len(found)} клипа")
    name = found[0]
    if not isinstance(name, str) or "/" in name or name != Path(name).name:
        raise ValueError(f"имя клипа не локальное: {name!r}")
    meta = (profile.get("files") or {}).get(name)
    if not isinstance(meta, dict) or not meta.get("sha256"):
        raise ValueError(f"в манифесте нет SHA256 для {name}")
    path = base / "assets" / CLIPS_DIR / name
    if not path.is_file():
        raise ValueError(f"клипа нет на колонке: {name}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != meta["sha256"]:
        raise ValueError(f"SHA256 {name} не совпал с манифестом")
    _check_wav(path, int(meta.get("rate", RATE)))
    return path, meta


def _check_wav(path: Path, rate: int) -> None:
    with wave.open(str(path)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != rate:
            raise ValueError(f"{path.name}: ждали моно 16 бит {rate} Гц")
        if wav.getnframes() < 1:
            raise ValueError(f"{path.name}: пустой клип")


def chimera_available(base: Path) -> bool:
    """Объявлять CHIMERA можно только если версия колонки — версия выпуска и комплект цел.

    После отката ``VERSION`` снова прежняя, а клипы, которых не было в ``prev/``, на диске остаются
    (сторож v0.2.4 делает ``cp -a``, не удаляет новое). Без этой сверки остаток выглядел бы как установленный.
    """
    try:
        version = (base / "VERSION").read_text(encoding="utf-8").strip()
        release = str(load_profile(base).get("release", "")).strip()
    except (OSError, ValueError):
        return False
    return bool(release) and version == release and not verify_installed(base)


def verify_installed(base: Path, required: "tuple[str, ...]" = (BIRD_CLIP,)) -> "list[str]":
    """Заявленный набор на месте и хеши сходятся. Иначе список причин — CHIMERA объявлять нельзя."""
    reasons = []
    try:
        profile = load_profile(base)
    except ValueError as exc:
        return [str(exc)]
    files = profile.get("files") or {}
    for name in required:
        meta = files.get(name)
        path = base / "assets" / CLIPS_DIR / name
        if not isinstance(meta, dict) or not meta.get("sha256"):
            reasons.append(f"нет записи манифеста: {name}")
            continue
        if not path.is_file():
            reasons.append(f"нет файла: {name}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != str(meta["sha256"]).lower():
            reasons.append(f"хеш не совпал: {name}")
            continue
        try:
            _check_wav(path, int(meta.get("rate", RATE)))
        except (ValueError, wave.Error) as exc:
            reasons.append(str(exc))
    claimed = [phrase for phrase, name in profile["commands"].items() if name in required]
    if len(claimed) != 1:
        reasons.append(f"команд на заявленные клипы: {len(claimed)}, ждали 1")
    return reasons


def chimera_command(text: str) -> str:
    """``play`` — фраза просит клип CHIMERA; иначе пусто (решает другой код)."""
    return "play" if phrase_key(text) == BIRD_PHRASE else ""
