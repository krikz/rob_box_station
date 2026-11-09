"""Цепочка STT колонки: MiniMax → Яндекс (#4550), хедж молчащего провайдера, отказ — не тишина."""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_stt as st  # noqa: E402

PCM = b"\x00\x00" * 1600


class _Fake:
    def __init__(self, name: str, answers: list, delay: float = 0.0) -> None:
        self.name, self.answers, self.delay, self.calls = name, list(answers), delay, 0

    def recognize(self, _pcm: bytes) -> str:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_first_provider_answers():
    a, b = _Fake("minimax", ["Игорёк, ты здесь?"]), _Fake("yandex", ["не нужен"])
    heard = st.SttChain([a, b], hedge_s=5).hear(PCM)

    assert (heard.text, heard.provider, heard.ok) == ("Игорёк, ты здесь?", "minimax", True)
    assert b.calls == 0


def test_429_goes_to_fallback_without_pause():
    """08.11: 429 у MiniMax — хост молчал 20 с. Теперь сразу Яндекс."""
    a = _Fake("minimax", [st.SttError("limited", "HTTP 429")])
    b = _Fake("yandex", ["игорёк который час"])
    heard = st.SttChain([a, b], hedge_s=5).hear(PCM)

    assert (heard.text, heard.provider) == ("игорёк который час", "yandex")
    assert any("minimax: limited" in n for n in heard.notes)


def test_single_provider_retries_429_once():
    a = _Fake("minimax", [st.SttError("limited"), "текст"])
    heard = st.SttChain([a], hedge_s=0, limited_retry_s=0).hear(PCM)

    assert heard.text == "текст" and a.calls == 2


def test_slow_provider_is_hedged():
    """08.11: MiniMax отвечал 10–19 с. Молчит дольше хеджа — Яндекс параллельно, берётся первый ответ."""
    a = _Fake("minimax", ["поздно"], delay=1.0)
    b = _Fake("yandex", ["быстро"])
    t0 = time.time()
    heard = st.SttChain([a, b], hedge_s=0.1).hear(PCM)

    assert (heard.text, heard.provider) == ("быстро", "yandex")
    assert time.time() - t0 < 0.8
    assert any("хедж" in n for n in heard.notes)


def test_all_failed_is_error_not_silence():
    a = _Fake("minimax", [st.SttError("offline", "timeout")])
    b = _Fake("yandex", [st.SttError("offline", "dns")])
    heard = st.SttChain([a, b], hedge_s=5).hear(PCM)

    assert heard.ok is False and heard.error == "offline" and heard.text == ""


def test_all_limited_marked_limited():
    a = _Fake("minimax", [st.SttError("limited")])
    b = _Fake("yandex", [st.SttError("limited")])

    assert st.SttChain([a, b], hedge_s=5).hear(PCM).error == "limited"


def test_empty_text_is_ok_silence():
    """Пустой текст от провайдера — тишина/шум, а не отказ: запасного не зовём."""
    a, b = _Fake("minimax", [""]), _Fake("yandex", ["x"])
    heard = st.SttChain([a, b], hedge_s=5).hear(PCM)

    assert heard.ok and heard.text == "" and b.calls == 0


def test_order_from_env_skips_missing(monkeypatch):
    monkeypatch.setenv("RBX_STT_ORDER", "yandex,minimax")
    y = _Fake("yandex", ["a"])
    chain = st.SttChain.from_env({"minimax": None, "yandex": y})

    assert chain.names() == ["yandex"]


def test_yandex_rest_request_and_result():
    seen = {}

    def transport(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=headers)
        return 200, json.dumps({"result": "Игорёк, привет"}).encode()

    text = st.YandexSTT("KEY", "FOLDER", transport=transport).recognize(PCM)

    assert text == "Игорёк, привет"
    assert "format=lpcm" in seen["url"] and "sampleRateHertz=16000" in seen["url"] and "folderId=FOLDER" in seen["url"]
    assert seen["headers"]["Authorization"] == "Api-Key KEY"
    assert seen["body"] == PCM


def test_yandex_strips_wav_header_and_maps_errors():
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(PCM)
    got = {}

    def ok(url, body, headers, timeout):
        got["body"] = body
        return 200, b'{"result": ""}'

    st.YandexSTT("K", transport=ok).recognize(buf.getvalue())
    assert got["body"] == PCM

    for status, kind in ((429, "limited"), (401, "auth"), (500, "error")):
        try:
            st.YandexSTT("K", transport=lambda *a, s=status: (s, b'{"error_code":"X"}')).recognize(PCM)
        except st.SttError as exc:
            assert exc.kind == kind
        else:
            raise AssertionError(f"нет отказа на {status}")


def test_yandex_absent_without_key():
    assert st.YandexSTT.maybe_from_env({}) is None
    assert st.YandexSTT.maybe_from_env({"YANDEX_API_KEY": "k"}).api_key == "k"


def test_hedge_loser_does_not_block_return():
    release = threading.Event()

    class _Hang:
        name = "minimax"

        def recognize(self, _pcm):
            release.wait(2)
            return "поздно"

    heard = st.SttChain([_Hang(), _Fake("yandex", ["есть"])], hedge_s=0.05).hear(PCM)
    release.set()
    assert heard.text == "есть"


def test_sprinter_is_yandex_only_as_fallback():
    m, y = _Fake("minimax", ["a"]), _Fake("yandex", ["b"])

    assert st.SttChain([m, y]).sprinter() == "yandex"
    assert st.SttChain([y, m]).sprinter() == ""  # Яндекс и так первый
    assert st.SttChain([m]).sprinter() == ""


def test_hear_one_returns_error_not_raise():
    chain = st.SttChain([_Fake("minimax", ["a"]), _Fake("yandex", [st.SttError("offline", "dns")])])

    assert chain.hear_one("yandex", PCM).error == "offline"
    assert chain.hear_one("minimax", PCM).text == "a"
    assert chain.hear_one("vosk", PCM).ok is False


def test_chain_does_not_hedge_to_provider_already_racing():
    """09.11 живьём: хост пустил Яндекс наперегонки, а хедж цепочки позвал его второй раз — платный дубль."""
    slow, y = _Fake("minimax", ["поздно"], delay=0.3), _Fake("yandex", ["быстро"])
    chain = st.SttChain([slow, y], hedge_s=0.05)
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("h", chain.hear(PCM)))
    worker.start()
    time.sleep(0.01)
    assert chain.hear_one("yandex", PCM).text == "быстро"
    worker.join(2)

    assert y.calls == 1 and out["h"].text == "поздно"
