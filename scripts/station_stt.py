"""Распознавание речи колонки: цепочка провайдеров с запасным и хеджем (#4550 в rob_box_project).

08.11: две колонки на одном ключе MiniMax ловили ``HTTP 429``, хост молчал 20 с; 08.11 же MiniMax отвечал 10–19 с и
падал по таймауту рукопожатия TLS — а Яндекс SpeechKit REST v1 с ключом колонки отвечает за 0,56 с (замер 09.11 с
колонки, тишина 1 с → ``{"result":""}``). gRPC на колонке нет, поэтому REST v1 на urllib (лимит 1 МБ: фраза 12 с —
384 КБ PCM).

Порядок — ``RBX_STT_ORDER`` (по умолчанию ``minimax,yandex``; провайдер без ключа пропускается):

* первый провайдер отказал (429, сеть, ошибка) — сразу следующий, без паузы;
* первый молчит дольше ``RBX_STT_HEDGE_S`` (2 с) — следующий запускается параллельно, берётся первый ответ;
* отказали все — :class:`Heard` с ``error`` (``limited`` — все упёрлись в лимит, ``offline`` — сеть, ``error``).

Решает код (ADR-0148): что считать отказом и кого звать — здесь, а не в модели.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

RATE = 16000
YANDEX_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
#: REST v1: не больше 1 МБ и 30 с звука за запрос.
YANDEX_MAX_BYTES = 1024 * 1024


class SttError(Exception):
    """Провайдер не дал текста. ``kind``: ``limited`` (429/квота), ``offline`` (сеть), ``auth``, ``error``."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


@dataclass
class Heard:
    """Итог распознавания: текст (может быть пустым — тишина/шум), кто ответил, что отказало по дороге."""

    text: str = ""
    provider: str = ""
    error: str = ""  # пусто — распознано; иначе вид отказа последнего провайдера
    seconds: float = 0.0
    notes: tuple = ()  # «minimax: limited», «хедж: yandex» — для лога

    @property
    def ok(self) -> bool:
        return not self.error


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # noqa: PLC0415  (у Питона колонки нет системных корневых сертификатов)

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # на ПК (тесты) — системные
        return ssl.create_default_context()


def urllib_transport(url: str, body: bytes, headers: dict, timeout: float) -> tuple:
    """(статус, тело) POST; HTTP-ошибка — статус, сеть — :class:`SttError` ``offline``."""
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:  # DNS, отказ соединения, таймаут
        raise SttError("offline", f"{type(exc).__name__}: {exc}") from exc


class YandexSTT:
    """Yandex SpeechKit REST v1 (синхронное распознавание короткого аудио), ключ ``Api-Key``."""

    name = "yandex"

    def __init__(self, api_key: str, folder_id: str = "", transport: Callable = urllib_transport,
                 timeout: float = 8.0, lang: str = "ru-RU") -> None:
        self.api_key, self.folder_id = api_key.strip(), folder_id.strip()
        self.transport, self.timeout, self.lang = transport, timeout, lang

    @classmethod
    def maybe_from_env(cls, env=os.environ, **kw) -> Optional["YandexSTT"]:
        key = env.get("YANDEX_API_KEY", "").strip()
        return cls(key, env.get("YANDEX_FOLDER_ID", ""), **kw) if key else None

    def recognize(self, pcm: bytes) -> str:
        """PCM s16le 16 кГц моно → текст. Отказ — :class:`SttError`."""
        if pcm[:4] == b"RIFF":  # пришёл WAV — заголовок Яндексу не нужен (format=lpcm)
            pcm = pcm[44:]
        pcm = pcm[:YANDEX_MAX_BYTES - (YANDEX_MAX_BYTES % 2)]
        query = {"lang": self.lang, "format": "lpcm", "sampleRateHertz": str(RATE), "topic": "general"}
        if self.folder_id:
            query["folderId"] = self.folder_id
        status, body = self.transport(f"{YANDEX_URL}?{urllib.parse.urlencode(query)}", pcm,
                                      {"Authorization": f"Api-Key {self.api_key}"}, self.timeout)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        if status == 200 and "result" in data:
            return str(data.get("result") or "").strip()
        detail = f"HTTP {status} {str(data.get('error_code') or '')} {str(data.get('error_message') or '')[:120]}"
        if status == 429:
            raise SttError("limited", detail.strip())
        if status in (401, 403):
            raise SttError("auth", detail.strip())
        raise SttError("error", detail.strip())


class MiniMaxSTT:
    """Обёртка провайдера ядра (``rob_box_voice.stt_providers.minimax_provider``) под общий интерфейс цепочки."""

    name = "minimax"

    def __init__(self, provider) -> None:
        self.provider = provider

    @classmethod
    def maybe_from_env(cls) -> Optional["MiniMaxSTT"]:
        try:
            from rob_box_voice.stt_providers.minimax_provider import MiniMaxSTTProvider  # noqa: PLC0415
        except ImportError:
            return None
        provider = MiniMaxSTTProvider.maybe_from_env()
        return cls(provider) if provider is not None else None

    def recognize(self, pcm: bytes) -> str:
        from rob_box_voice.stt_providers import minimax_provider as mm  # noqa: PLC0415

        wav = pcm if pcm[:4] == b"RIFF" else mm.ensure_wav_container(pcm, sample_rate=RATE)
        try:
            return (self.provider.transcribe(wav).text or "").strip()
        except mm.MiniMaxSTTRateLimitError as exc:
            raise SttError("limited", "HTTP 429") from exc
        except getattr(mm, "MiniMaxSTTQuotaError", mm.MiniMaxSTTRateLimitError) as exc:
            raise SttError("limited", f"квота: {exc}") from exc
        except mm.MiniMaxSTTAuthError as exc:
            raise SttError("auth", str(exc)[:120]) from exc
        except mm.MiniMaxSTTUnavailableError as exc:  # таймаут, сеть
            raise SttError("offline", str(exc)[:160]) from exc
        except mm.MiniMaxSTTError as exc:
            raise SttError("error", f"{type(exc).__name__}: {str(exc)[:120]}") from exc


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


class SttChain:
    """Провайдеры по порядку: отказ — следующий сразу, молчание дольше ``hedge_s`` — следующий параллельно."""

    def __init__(self, providers: Sequence, hedge_s: Optional[float] = None, limited_retry_s: float = 0.6) -> None:
        self.providers: List = [p for p in providers if p is not None]
        self.hedge_s = _env_float("RBX_STT_HEDGE_S", 2.0) if hedge_s is None else hedge_s
        self.limited_retry_s = limited_retry_s
        #: Провайдеры, уже пущенные наперегонки (:meth:`hear_one`) на эту фразу; ``hear`` очищает в начале.
        self.racing: set = set()
        self._pool = cf.ThreadPoolExecutor(max_workers=max(2, len(self.providers) + 1), thread_name_prefix="stt")

    @classmethod
    def from_env(cls, available: Optional[dict] = None) -> "SttChain":
        """Цепочка по ``RBX_STT_ORDER``; ``available`` — имя → провайдер (тесты), иначе ключи из окружения."""
        if available is None:
            available = {"minimax": MiniMaxSTT.maybe_from_env(), "yandex": YandexSTT.maybe_from_env()}
        order = [n.strip().lower() for n in os.environ.get("RBX_STT_ORDER", "minimax,yandex").split(",") if n.strip()]
        chosen = [available.get(name) for name in order]
        return cls(chosen)

    def names(self) -> List[str]:
        return [p.name for p in self.providers]

    def sprinter(self) -> str:
        """Запасной, которого стоит пустить наперегонки с первым, когда фраза, похоже, обращение: Яндекс в 3 раза
        быстрее (09.11 на записях колонки 0,47–0,57 с против 1,29–2,43 с у MiniMax), но платный за запрос — на весь
        фон мастерской (~7 фраз/мин) его не зовём."""
        rest = [p.name for p in self.providers[1:]]
        return "yandex" if "yandex" in rest else ""

    def hear_one(self, name: str, pcm: bytes) -> Heard:
        """Один провайдер по имени (гонка с цепочкой): отказ — ``Heard.error``."""
        t0 = time.time()
        provider = next((p for p in self.providers if p.name == name), None)
        if provider is None:
            return Heard(error="error", notes=(f"нет провайдера {name}",))
        self.racing.add(name)  # цепочка этого же провайдера хеджем второй раз не зовёт (платный запрос)
        try:
            return Heard(text=provider.recognize(pcm), provider=name, seconds=time.time() - t0)
        except SttError as exc:
            return Heard(error=exc.kind, seconds=time.time() - t0, notes=(f"{name}: {exc.kind} {exc.detail}".strip(),))
        except Exception as exc:  # noqa: BLE001
            return Heard(error="error", seconds=time.time() - t0, notes=(f"{name}: {type(exc).__name__}",))

    def _call(self, provider, pcm: bytes) -> str:
        """Один провайдер; 429 — ещё одна попытка через ``limited_retry_s`` (лимит MiniMax транзиентный)."""
        try:
            return provider.recognize(pcm)
        except SttError as exc:
            if exc.kind != "limited" or len(self.providers) > 1:
                raise  # есть запасной — не ждём, сразу к нему
            time.sleep(self.limited_retry_s)
            return provider.recognize(pcm)

    def hear(self, pcm: bytes) -> Heard:
        t0 = time.time()
        if not self.providers:
            return Heard(error="error", notes=("нет ни одного провайдера STT",))
        notes: list = []
        errors: list = []
        futures: dict = {}
        queue = list(self.providers)

        def launch() -> None:
            provider = queue.pop(0)
            futures[self._pool.submit(self._call, provider, pcm)] = provider

        self.racing = set()
        launch()
        while futures:
            hedge = self.hedge_s if queue and self.hedge_s > 0 else None
            done, _ = cf.wait(list(futures), timeout=hedge, return_when=cf.FIRST_COMPLETED)
            if not done and queue[0].name in self.racing:  # он уже в гонке у хоста — второй запрос не нужен
                notes.append(f"хедж: {queue[0].name} уже в гонке")
                queue.pop(0)
                continue
            if not done:  # первый молчит — зовём следующего параллельно, ждём того, кто ответит раньше
                notes.append(f"хедж: {futures[next(iter(futures))].name} молчит {self.hedge_s:.1f} с → "
                             f"{queue[0].name}")
                launch()
                continue
            for future in done:
                provider = futures.pop(future)
                try:
                    text = future.result()
                except SttError as exc:
                    errors.append(exc.kind)
                    notes.append(f"{provider.name}: {exc.kind} {exc.detail}".strip())
                    if queue and not futures:
                        launch()
                    continue
                except Exception as exc:  # noqa: BLE001 — сбой провайдера не роняет хост
                    errors.append("error")
                    notes.append(f"{provider.name}: {type(exc).__name__}")
                    if queue and not futures:
                        launch()
                    continue
                for other in futures:  # проигравший хедж: результат не нужен (поток досчитает сам)
                    other.cancel()
                return Heard(text=text, provider=provider.name, seconds=time.time() - t0, notes=tuple(notes))
        if errors and all(e == "limited" for e in errors):
            kind = "limited"
        elif errors and all(e == "offline" for e in errors):
            kind = "offline"
        else:
            kind = "error"
        return Heard(error=kind, seconds=time.time() - t0, notes=tuple(notes))
