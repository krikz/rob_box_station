"""Офлайн-фразы: синтез заранее, пересинтез при смене голоса, сбой синтеза не роняет и не стирает старое."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_offline as so  # noqa: E402


def _synth(log: list, fail_after: int = 99):
    async def synth(text: str, voice: str) -> bytes:
        if len(log) >= fail_after:
            raise OSError("нет сети")
        log.append((text, voice))
        return f"{voice}:{text}".encode()
    return synth


def test_refresh_synthesizes_missing_once(tmp_path):
    calls: list = []
    cache = so.OfflinePhrases(tmp_path, print, {"a": "Раз.", "b": "Два."})

    assert asyncio.run(cache.refresh(_synth(calls), "V1")) == 2
    assert cache.pcm("a") == "V1:Раз.".encode()
    assert asyncio.run(cache.refresh(_synth(calls), "V1")) == 0
    assert len(calls) == 2


def test_voice_change_resynthesizes_and_keeps_old_until_done(tmp_path):
    calls: list = []
    cache = so.OfflinePhrases(tmp_path, print, {"a": "Раз.", "b": "Два."})
    asyncio.run(cache.refresh(_synth(calls), "V1"))

    assert cache.missing("V2") == ["a", "b"]
    asyncio.run(cache.refresh(_synth(calls, fail_after=3), "V2"))  # «a» новым голосом, на «b» сеть пропала
    assert cache.pcm("a") == "V2:Раз.".encode()
    assert cache.pcm("b") == "V1:Два.".encode()  # старым голосом лучше, чем тишина
    assert cache.missing("V2") == ["b"]


def test_text_change_resynthesizes(tmp_path):
    calls: list = []
    asyncio.run(so.OfflinePhrases(tmp_path, print, {"a": "Раз."}).refresh(_synth(calls), "V"))
    cache = so.OfflinePhrases(tmp_path, print, {"a": "Один."})

    assert cache.missing("V") == ["a"]


def test_key_for_and_missing_file(tmp_path):
    cache = so.OfflinePhrases(tmp_path, print)

    assert cache.key_for("  Слушаю. ") == "heard"
    assert cache.key_for("что-то другое") == ""
    assert cache.pcm("heard") is None


def test_tones_are_audio():
    for kind in ("error", "ok", "tick", "connect", "disconnect"):
        pcm = so.tone(kind)
        assert len(pcm) > 1000 and len(pcm) % 2 == 0


def test_phrases_cover_host_keys():
    for key in ("started", "heard", "no_net", "stt_fail", "stt_limited", "llm_fail", "tts_fail", "mic_off",
                "mic_on", "restarting", "bt_on", "bt_off"):
        assert so.PHRASES[key]


def test_variants_rotate_without_immediate_repeat(tmp_path):
    """Шифу 09.11: «делай вариации, чтоб было несколько, а не однотипно каждый раз»."""
    calls: list = []
    cache = so.OfflinePhrases(tmp_path, print, {"hi": ["Привет.", "Здравствуй.", "Хай."]})
    assert asyncio.run(cache.refresh(_synth(calls), "V")) == 3
    picked = [cache.pick("hi") for _ in range(30)]
    assert all(a[0] != b[0] for a, b in zip(picked, picked[1:]))  # подряд — не тот же
    assert {vid for vid, _ in picked} == {"hi", "hi.1", "hi.2"}
    assert cache.pcm("hi.2") == "V:Хай.".encode() and cache.pcm("hi") is not None


def test_first_variant_reuses_old_single_file(tmp_path):
    """Нулевой вариант — прежнее имя файла: уже синтезированные фразы не пересинтезируются."""
    calls: list = []
    asyncio.run(so.OfflinePhrases(tmp_path, print, {"hi": "Привет."}).refresh(_synth(calls), "V"))
    cache = so.OfflinePhrases(tmp_path, print, {"hi": ["Привет.", "Здравствуй."]})
    assert cache.missing("V") == ["hi.1"]


def test_every_phrase_has_variants():
    assert all(len(v) >= 2 for v in so.PHRASES.values())


def test_last_variant_survives_restart(tmp_path):
    """09.11: «Я снова тут…» прозвучало два старта подряд — прошлый вариант помнился только в процессе."""
    calls: list = []
    cache = so.OfflinePhrases(tmp_path, print, {"hi": ["Привет.", "Здравствуй."]})
    asyncio.run(cache.refresh(_synth(calls), "V"))
    first = cache.pick("hi")[0]
    again = so.OfflinePhrases(tmp_path, print, {"hi": ["Привет.", "Здравствуй."]}).pick("hi")[0]  # «перезапуск»
    assert first != again
