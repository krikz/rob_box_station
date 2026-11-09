"""Звонки через телефон (HFP Hands-Free, #16): фразы, книга контактов, AT-канал, индикаторы звонка."""
import asyncio
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_hfp as h  # noqa: E402

BOOK = [("+79161112233", "Кир Петров"), ("+79160000001", "Кирилл Работа"), ("+79165556677", "Мама"),
        ("+7 916 111-22-33", "Кир Петров"), ("8800", "Кира Соседка")]


def test_call_command_forms():
    assert h.call_command("набери Кира") == ("dial", "Кира")
    assert h.call_command("позвони Киру.") == ("dial", "Киру")
    assert h.call_command("позвони на номер +7 916 111 22 33") == ("dial", "+7 916 111 22 33")
    assert h.call_command("прими звонок") == ("answer", "")
    assert h.call_command("возьми трубку") == ("answer", "")
    assert h.call_command("сбрось") == ("hangup", "")
    assert h.call_command("положи трубку") == ("hangup", "")
    assert h.call_command("перезвони") == ("redial", "")
    assert h.call_command("который час") == ("", "")
    assert h.call_command("набери") == ("", "")


def test_parse_cpbr():
    assert h.parse_cpbr('+CPBR: 12,"+79161234567",145,"Кир Петров"') == ("+79161234567", "Кир Петров")
    assert h.parse_cpbr("+CPBS: \"ME\",1,2") is None
    # 09.11 Redmi: тип номера в имени — колонка зачитывала «Кир слэш M»
    assert h.parse_cpbr('+CPBR: 7,"+79160000000",129,"Кир/M"') == ("+79160000000", "Кир")


def test_exact_name_beats_similar_ones():
    """09.11 живьём: «позвони Киру» → «нашёл несколько: Кир, …Кириченко, Кирилл …». Есть точный «Кир» — звоним ему."""
    book = [("1", "Кир"), ("2", "Елена Мама Даши Кириченко 1А"), ("3", "Кирилл Батайск")]
    assert h.find_contacts("Киру", book) == [("1", "Кир")]


def test_find_contacts_by_stem_and_dedup():
    """«Кира»/«Киру» → «Кир Петров» (один номер, хоть и записан двумя способами); «Кирилл» — только как префикс."""
    found = h.find_contacts("Киру", BOOK)
    assert found[0] == ("+79161112233", "Кир Петров")
    assert [n for _, n in found].count("Кир Петров") == 1
    assert "Кирилл Работа" not in [n for _, n in found]  # точные есть — похожие не мешают
    assert h.find_contacts("Кирил", BOOK) == [("+79160000001", "Кирилл Работа")]  # точных нет — по началу
    assert h.find_contacts("маме", BOOK) == [("+79165556677", "Мама")]
    assert h.find_contacts("Вася", BOOK) == []


def test_dial_string_keeps_only_dial_chars():
    assert h.dial_string("+7 (916) 111-22-33") == "ATD+79161112233;"


#: AT-канал — fd сокета RFCOMM колонки (Linux): ``add_reader`` и ``os.read`` по сокету на Windows не работают.
linux_only = pytest.mark.skipif(sys.platform == "win32", reason="fd RFCOMM колонки — Linux")


def _channel():
    a, b = socket.socketpair()
    return h.AtChannel(a.detach(), print), b


@linux_only
def test_at_channel_ask_collects_lines_and_routes_events():
    async def run():
        at, phone = _channel()
        task = asyncio.ensure_future(at.ask("AT+CPBR=1,2"))
        await asyncio.sleep(0.05)
        assert phone.recv(100) == b"AT+CPBR=1,2\r"
        phone.send(b'\r\n+CPBR: 1,"1",129,"A"\r\n:\r\n+CIEV: 4,3\r\n+CPBR: 2,"2",129,"B"\r\n:OK\r\n')
        ok, lines = await task
        event = await asyncio.wait_for(at.events.get(), 1)
        at.close()
        phone.close()
        return ok, lines, event

    ok, lines, event = asyncio.run(run())
    assert ok and lines == ['+CPBR: 1,"1",129,"A"', '+CPBR: 2,"2",129,"B"']
    assert event == "+CIEV: 4,3"


@linux_only
def test_at_channel_late_ok_does_not_close_next_command():
    async def run():
        at, phone = _channel()
        first = await at.ask("AT+SLOW", timeout=0.1)  # телефон молчит
        phone.recv(100)
        phone.send(b"OK\r\n")  # запоздавший ответ первой
        await asyncio.sleep(0.05)
        task = asyncio.ensure_future(at.ask("AT+CLCC", timeout=1))
        await asyncio.sleep(0.05)
        phone.send(b"+CLCC: 1,0,0,0,0\r\nOK\r\n")
        second = await task
        at.close()
        phone.close()
        return first, second

    first, second = asyncio.run(run())
    assert first == (False, ["timeout"])
    assert second == (True, ["+CLCC: 1,0,0,0,0"])


@linux_only
def test_phone_hangup_of_channel_is_event():
    async def run():
        at, phone = _channel()
        phone.close()
        return await asyncio.wait_for(at.events.get(), 1)

    assert asyncio.run(run()) == "DISCONNECTED"


def _hfp():
    calls = []
    hfp = h.Hfp(print, on_call=calls.append)
    return hfp, calls


def test_outgoing_call_is_up_from_dialing_until_hangup():
    """Исходящий: callsetup 2/3 — звук нужен уже на гудках; сброс — callsetup 0 без call."""
    hfp, calls = _hfp()
    hfp._ciev("+CIEV: 2,2")
    hfp._ciev("+CIEV: 2,3")
    hfp._ciev("+CIEV: 1,1")
    hfp._ciev("+CIEV: 2,0")
    hfp._ciev("+CIEV: 4,3")  # сигнал сети — не про звонок
    assert calls == [True] and hfp.in_call
    hfp._ciev("+CIEV: 1,0")
    assert calls == [True, False] and not hfp.in_call


def test_incoming_ring_is_not_call_until_answered():
    hfp, calls = _hfp()
    hfp.ringing = "Мама"
    hfp._ciev("+CIEV: 2,1")
    assert calls == [] and hfp.ringing == "Мама"
    hfp._ciev("+CIEV: 2,0")  # не ответили — звонок пропал
    assert calls == [] and hfp.ringing == ""


class _FakeAt:
    """Телефон: книга на 60 записей, запись 30 битая (кусок с ней молчит, сама она — ERROR)."""

    def __init__(self) -> None:
        self.asked = []

    async def ask(self, cmd, timeout=6.0):
        self.asked.append(cmd)
        if cmd == "AT+CPBS?":
            return True, ['+CPBS: "ME",60,2048']
        if cmd.startswith("AT+CPBR="):
            a, _, b = cmd[8:].partition(",")
            lo, hi = int(a), int(b or a)
            if lo <= 30 <= hi and hi > lo:
                return False, ["timeout"]
            if lo == hi == 30:
                return False, ["ERROR"]
            return True, [f'+CPBR: {i},"+7900{i:04d}",145,"Контакт {i}"' for i in range(lo, hi + 1)]
        return True, []


def test_load_book_survives_bad_entry():
    hfp, _ = _hfp()
    hfp.at = _FakeAt()
    assert asyncio.run(hfp.load_book()) == 59
    assert "AT+CPBR=26,50" in hfp.at.asked and "AT+CPBR=30" in hfp.at.asked


def test_handle_dials_found_contact_and_asks_on_ambiguity():
    hfp, _ = _hfp()
    hfp.at = _FakeAt()
    hfp.book, hfp.book_at = list(BOOK), 10**12

    async def attach():
        return True

    hfp.attach = attach
    # «набери Кира» — и Кир, и Кира: колонка переспрашивает, а не звонит наугад
    assert "несколько" in asyncio.run(hfp.handle("набери Кира"))
    assert asyncio.run(hfp.handle("позвони Киру Петрову")) == "Звоню: Кир Петров."
    assert hfp.at.asked[-1] == "ATD+79161112233;"
    hfp.book = [("1", "Мама")]
    assert asyncio.run(hfp.handle("набери маму")) == "Звоню: Мама."
    assert asyncio.run(hfp.handle("прими")) == ""
    assert asyncio.run(hfp.handle("расскажи анекдот")) is None


def test_watchdog_ends_call_when_phone_lists_no_calls(monkeypatch):
    """09.11 живьём: конец звонка не дошёл через +CIEV, хост держал микрофон в «разговоре» — сторож по AT+CLCC."""
    monkeypatch.setattr(h, "CLCC_EVERY_S", 0.01)

    class _NoCalls:
        async def ask(self, cmd, timeout=6.0):
            return True, []

    async def run():
        hfp, calls = _hfp()
        hfp.at = _NoCalls()
        hfp._set_call(True)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not hfp.in_call:
                break
        return hfp, calls

    hfp, calls = asyncio.run(run())
    assert calls == [True, False] and not hfp.in_call and hfp.setup == 0


def test_watchdog_keeps_live_call(monkeypatch):
    monkeypatch.setattr(h, "CLCC_EVERY_S", 0.01)

    class _Talking:
        async def ask(self, cmd, timeout=6.0):
            return True, ['+CLCC: 1,0,0,0,0,"…",129']

    async def run():
        hfp, calls = _hfp()
        hfp.at = _Talking()
        hfp._set_call(True)
        await asyncio.sleep(0.1)
        still = hfp.in_call
        hfp.in_call = False  # остановить сторожа
        await asyncio.sleep(0.03)
        return still, calls

    still, calls = asyncio.run(run())
    assert still and calls == [True]


def test_call_audio_off_by_default_never_opens_sco(monkeypatch):
    """09.11: SCO через HCI вешает чип Amlogic W1 — по умолчанию колонка звук звонка не трогает."""
    monkeypatch.setattr(h, "CALL_AUDIO", False)
    started = []

    class _Audio:
        def start(self):
            started.append("start")

        def stop(self):
            started.append("stop")

    hfp, calls = _hfp()
    hfp.audio = _Audio()
    hfp._ciev("+CIEV: 2,2")
    hfp._ciev("+CIEV: 2,0")
    assert calls == [True, False] and started == ["stop"]
    monkeypatch.setattr(h, "CALL_AUDIO", True)
    hfp._ciev("+CIEV: 1,1")
    assert started == ["stop", "start"]


def test_call_without_audio_releases_hands_free_and_holds(monkeypatch):
    """Шифу 09.11: «пока подключена гарнитура, в телефоне ничего не слышно, даже если выбрать телефон» —
    через секунду после начала звонка колонка отпускает Hands-Free и обратно не просит, пока держит паузу."""
    monkeypatch.setattr(h, "CALL_AUDIO", False)
    released = []

    async def run():
        hfp, calls = _hfp()

        async def release():
            released.append(True)
            hfp.released_until = h.time.time() + 600

        hfp.release = release
        hfp._ciev("+CIEV: 2,2")
        await asyncio.sleep(1.2)
        return hfp

    hfp = asyncio.run(run())
    assert released == [True] and hfp.released_until > h.time.time()


def test_at_fd_not_inherited_by_exec(monkeypatch):
    """09.11: после самообновления (execv) новый хост держал AT-канал старого — «Device or resource busy»."""
    import inspect

    assert "os.set_inheritable(fd, False)" in inspect.getsource(h.Hfp.attach)
