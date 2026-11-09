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
    for kind in ("error", "ok", "tick"):
        pcm = so.tone(kind)
        assert len(pcm) > 1000 and len(pcm) % 2 == 0


def test_phrases_cover_host_keys():
    for key in ("started", "heard", "no_net", "stt_fail", "stt_limited", "llm_fail", "tts_fail", "mic_off",
                "mic_on", "restarting"):
        assert so.PHRASES[key]
