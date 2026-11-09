"""Хост колонки: кольцо не залипает в «думаю», вейк своей моделью.

Повтор на 429 и запасной провайдер STT — ``test_station_stt.py`` (цепочка ``station_stt``).
08.11 баг на живой второй колонке: адресованный ход с пустой репликой (`ход 0.05 с: []`) оставлял кольцо в `think` навсегда: в очередь
   озвучки ничего не попадало, `_play` не просыпался и `ring.idle()` не вызывался.
"""
import asyncio
import types
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import station_host as sh  # noqa: E402


class _Ring:
    def __init__(self) -> None:
        self.states: list = []
        self.state = "think"

    def set(self, state: str) -> None:
        self.states.append(state)
        self.state = state

    def idle(self) -> None:
        self.states.append("idle")
        self.state = "off"


def test_drain_idles_ring_without_speech():
    """Ход без реплики: очередь пуста — кольцо возвращается в покой, а не висит в `think`."""
    live = object.__new__(sh.LiveOut)
    live._ring, live._queue = _Ring(), asyncio.Queue()

    asyncio.run(live.drain())

    assert live._ring.states == ["idle"]


class _Result:
    def __init__(self, addressed: bool, said: list) -> None:
        self.addressed, self.said = addressed, said


def test_wordless_turn_keeps_ring_listening():
    """Голое «Игорёк» (ход без слов): кольцо показывает «слушаю», а не гаснет молча."""
    ring = _Ring()

    async def run() -> bool:
        return sh.wordless_tail(ring, _Result(addressed=True, said=[]))

    assert asyncio.run(run()) is True
    assert ring.states == ["listen"]


def test_turn_with_speech_does_not_touch_ring():
    """Обычный ответ кольцом управляет озвучка, а не этот хелпер."""
    ring = _Ring()

    async def run() -> bool:
        return sh.wordless_tail(ring, _Result(addressed=True, said=["У меня всё отлично."]))

    assert asyncio.run(run()) is False
    assert ring.states == []


def test_kws_wake_shadow_by_default(monkeypatch):
    """По умолчанию модель только пишет в лог: уверенный вейк не решает за облако."""
    monkeypatch.delenv("RBX_KWS_DECIDE", raising=False)

    assert sh.kws_wake("igorek", 0.99) is False


def test_kws_wake_respects_threshold(monkeypatch):
    monkeypatch.setenv("RBX_KWS_DECIDE", "1")
    monkeypatch.setenv("RBX_KWS_THRESHOLD", "0.8")

    assert sh.kws_wake("igorek", 0.81) is True
    assert sh.kws_wake("igorek", 0.79) is False
    assert sh.kws_wake("_unknown_", 0.99) is False


def test_kws_wake_text_prefixes_missing_wake(monkeypatch):
    """STT не разобрал вейк, модель уверена — код подставляет вейк, иначе ядро скажет no_wake."""
    monkeypatch.setenv("RBX_KWS_DECIDE", "1")
    wake = sh.WAKE_WORDS[0]

    assert sh.kws_wake_text("Горек, который час?", "igorek", 0.95) == f"{wake}, Горек, который час?"
    assert sh.kws_wake_text("который час?", "igorek", 0.95) == f"{wake}, который час?"


def test_kws_wake_text_keeps_existing_wake(monkeypatch):
    """Вейк уже в тексте — не дублируем; режим тени — текст не трогаем."""
    monkeypatch.setenv("RBX_KWS_DECIDE", "1")
    text = f"{sh.WAKE_WORDS[0]}, как дела"

    assert sh.kws_wake_text(text, "igorek", 0.95) == text
    monkeypatch.setenv("RBX_KWS_DECIDE", "0")
    assert sh.kws_wake_text("который час?", "igorek", 0.95) == "который час?"


def test_wake_fix_one_edit_without_model():
    """«Горек, который час» — облако съело «И»: одна правка до формы имени — это вейк."""
    assert sh.wake_fix("Горек, который час?") == f"{sh.WAKE_WORDS[0]}, который час?"
    assert sh.wake_fix("И горек, ты здесь?") == f"{sh.WAKE_WORDS[0]}, ты здесь?"


def test_wake_fix_keeps_plain_names_and_words():
    """«Игорь» и «Егорка» — обычные имена: без своей модели не вейк; обычная фраза не трогается."""
    assert sh.wake_fix("Игорь, иди сюда") == "Игорь, иди сюда"
    assert sh.wake_fix("который час") == "который час"
    text = f"{sh.WAKE_WORDS[0]}, как дела"
    assert sh.wake_fix(text) == text


def test_wake_fix_two_edits_need_model(monkeypatch):
    monkeypatch.delenv("RBX_KWS_DECIDE", raising=False)
    assert sh.wake_fix("Игорь, иди сюда", "igorek", 0.7) == f"{sh.WAKE_WORDS[0]}, иди сюда"
    assert sh.wake_fix("Игорь, иди сюда", "_unknown_", 0.9) == "Игорь, иди сюда"


class _Buttons:
    def __init__(self) -> None:
        self.armed_with = None

    def arm(self, ring) -> None:
        self.armed_with = ring
        ring.set("listen")


def test_wordless_turn_arms_window_without_wake():
    """#13: после голого «Игорёк» фраза «проверь обновления» без имени уходила модели — теперь окно без вейка."""
    ring, buttons = _Ring(), _Buttons()

    async def run() -> bool:
        return sh.wordless_tail(ring, _Result(addressed=True, said=[]), buttons)

    assert asyncio.run(run()) is True
    assert buttons.armed_with is ring and ring.states == ["listen"]


def test_device_facts_tell_model_it_can_report(monkeypatch):
    monkeypatch.setattr(sh, "wifi_now", lambda: {"ssid": "net", "ip": "10.1.1.13"})
    facts = sh.device_facts()

    assert "report_issue" in facts and "never say you cannot" in facts
    assert "проверь обновление" in facts


def _fake_core(monkeypatch):
    """Каталог тулов и knowledge ядра — подмена: пакетов ядра в окружении тестов нет."""
    import types
    from dataclasses import dataclass, field
    from types import MappingProxyType

    @dataclass(frozen=True)
    class Entry:
        name: str
        description: str
        parameters: object
        llm_visible: bool = True
        read_only: bool = False
        destructive: bool = True
        idempotent: bool = False
        skill: tuple = ()
        signature: object = field(default_factory=dict)

    tc = types.ModuleType("rob_box_core.tool_catalog")
    tc.ToolCatalogEntry, tc.CORE_SKILL = Entry, "core"
    tc.TOOL_CATALOG = (Entry("get_current_time", "time", {}),)
    tc._BY_NAME = MappingProxyType({"get_current_time": tc.TOOL_CATALOG[0]})
    kn = types.ModuleType("rob_box_dialog.knowledge")
    kn.TOOL_CLASSES = MappingProxyType({"get_current_time": "query"})
    kn.TOOLS_ALWAYS = ("speak_text", "get_current_time")
    core, dialog = types.ModuleType("rob_box_core"), types.ModuleType("rob_box_dialog")
    core.tool_catalog, dialog.knowledge = tc, kn
    for name, mod in (("rob_box_core", core), ("rob_box_core.tool_catalog", tc), ("rob_box_dialog", dialog),
                      ("rob_box_dialog.knowledge", kn)):
        monkeypatch.setitem(sys.modules, name, mod)
    return tc, kn


def test_report_tool_registered_once(monkeypatch):
    tc, kn = _fake_core(monkeypatch)

    assert sh.register_report_tool() and sh.register_report_tool()
    assert [e.name for e in tc.TOOL_CATALOG].count("report_issue") == 1
    assert tc._BY_NAME["report_issue"].parameters["required"] == ["complaint"]
    assert kn.TOOL_CLASSES["report_issue"] == "query"
    assert kn.TOOLS_ALWAYS.count("report_issue") == 1


def test_report_tool_handler_files_issue(monkeypatch):
    _fake_core(monkeypatch)
    calls = []

    class _Reporter:
        def file_by_model(self, complaint, title, details, with_log):
            calls.append((complaint, title, details, with_log))
            return {"status": "done", "issue_number": 21, "say": "Завёл issue номер 21."}

    stand = types.SimpleNamespace(tools=types.SimpleNamespace())
    monkeypatch.delenv("RBX_REPORT_DRY", raising=False)
    assert sh.install_report_tool(stand, _Reporter())
    ok, data = stand.tools._t_report_issue({"complaint": "нет логов на странице", "title": "логи",
                                            "with_log": False})

    assert ok and data["issue_number"] == 21
    assert calls == [("нет логов на странице", "логи", "", False)]


def test_first_heard_takes_first_ok_and_waits_past_failure():
    import station_stt as st

    async def run(first, second, delay_first, delay_second):
        async def later(value, delay):
            await asyncio.sleep(delay)
            return value
        return await sh.first_heard(asyncio.ensure_future(later(first, delay_first)),
                                    asyncio.ensure_future(later(second, delay_second)))

    slow_ok, fast_ok = st.Heard("цепочка", "minimax"), st.Heard("гонщик", "yandex")
    assert asyncio.run(run(slow_ok, fast_ok, 0.05, 0.0)).text == "гонщик"
    assert asyncio.run(run(slow_ok, st.Heard(error="offline"), 0.05, 0.0)).text == "цепочка"
    assert asyncio.run(run(st.Heard(error="error"), st.Heard(error="offline"), 0.0, 0.01)).ok is False


def test_ring_leds_get_different_colors(monkeypatch):
    """Шифу 09.11: «три светодиода, а горят одним цветом». Стиль color — у диодов разные цвета, alice — один."""
    shown = []
    ring = sh.Ring(None)
    monkeypatch.setattr(ring, "_show", shown.append)
    monkeypatch.setattr(sh, "LED_BRIGHT", 255)

    async def frame(style):
        monkeypatch.setenv("RBX_RING_STYLE", style)
        ring._patterns = {}
        ring.set("listen")
        ring._since = sh.time.monotonic() - 0.8  # пик дыхания
        task = asyncio.ensure_future(ring._run())
        await asyncio.sleep(0.02)
        task.cancel()
        return shown[-1]

    colored = asyncio.run(frame("color"))
    assert len({tuple(c) for c in colored}) == 3
    single = asyncio.run(frame("alice"))
    assert len({tuple(c) for c in single}) == 1
    assert set(sh.Ring.PALETTES) >= {"listen", "think", "speak", "call", "muted", "error", "off", "connect"}
