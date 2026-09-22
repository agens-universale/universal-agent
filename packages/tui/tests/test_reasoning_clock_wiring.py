"""Проводка ReasoningClock в TraceScreen: когнитивные тул-события + stream-источник."""
import pytest
from gc_tui.models import TraceEvent
from gc_tui.services.reasoning_clock import ReasoningClock


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _call_ev(tool_name: str, call_id: str) -> TraceEvent:
    return TraceEvent(no=1, event_ts_utc_iso="", kind="call", title="",
                      summary="", full_payload={}, call_id=call_id, tool_name=tool_name)


def _result_ev(call_id: str) -> TraceEvent:
    return TraceEvent(no=2, event_ts_utc_iso="", kind="result", title="",
                      summary="ok", full_payload={}, call_id=call_id, tool_name="")


@pytest.mark.asyncio
async def test_cognitive_call_lights_reasoning_and_accumulates(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        scr = pilot.app.screen
        fc = _FakeClock()
        scr._reasoning_clock = ReasoningClock(clock=fc, hold=0.4)
        scr._render_event(_call_ev("session_memory", "c1"))
        assert scr._reasoning_clock.live() is True
        fc.t = 2.0
        scr._render_event(_result_ev("c1"))
        assert scr._reasoning_clock.episode() == 2.0
        fc.t = 2.5  # > hold после stop
        assert scr._reasoning_clock.live() is False


@pytest.mark.asyncio
async def test_non_cognitive_call_ignored(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        scr = pilot.app.screen
        fc = _FakeClock()
        scr._reasoning_clock = ReasoningClock(clock=fc, hold=0.4)
        scr._render_event(_call_ev("fs_write_file", "w1"))
        assert scr._reasoning_clock.live() is False
        assert scr._reasoning_clock.episode() == 0.0


@pytest.mark.asyncio
async def test_worldly_call_interrupts_episode(tmp_path):
    """Не-когнитивный тул (действие в мире) между двумя когнитивными рвёт эпизод:
    второе раздумье считается заново, а не суммируется."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        scr = pilot.app.screen
        fc = _FakeClock()
        scr._reasoning_clock = ReasoningClock(clock=fc, hold=0.5)
        # эпизод 1: память 5с
        scr._render_event(_call_ev("session_memory", "c1"))
        fc.t = 5.0
        scr._render_event(_result_ev("c1"))
        assert scr._reasoning_clock.episode() == 5.0
        # действие в мире → interrupt
        scr._render_event(_call_ev("fs_write_file", "w1"))
        fc.t = 6.0
        scr._render_event(_result_ev("w1"))
        # эпизод 2: память 3с — считается заново
        scr._render_event(_call_ev("session_memory", "c2"))
        fc.t = 9.0
        scr._render_event(_result_ev("c2"))
        assert scr._reasoning_clock.episode() == 3.0   # НЕ 8.0


@pytest.mark.asyncio
async def test_cognitive_call_without_call_id_does_not_interrupt(tmp_path):
    """Когнитивный тул без call_id — no-op, НЕ рвёт эпизод (не проваливается в interrupt).
    Следующая когнитивная операция продолжает тот же эпизод (склейка)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        scr = pilot.app.screen
        fc = _FakeClock()
        scr._reasoning_clock = ReasoningClock(clock=fc, hold=0.5)
        scr._render_event(_call_ev("session_memory", "c1"))
        fc.t = 4.0
        scr._render_event(_result_ev("c1"))            # эпизод = 4.0
        scr._render_event(_call_ev("session_memory", ""))   # без call_id → no-op (не interrupt)
        scr._render_event(_call_ev("session_memory", "c2"))
        fc.t = 6.0
        scr._render_event(_result_ev("c2"))            # +2.0, тот же эпизод
        assert scr._reasoning_clock.episode() == 6.0   # склейка 4+2; при баге было бы 2.0


@pytest.mark.asyncio
async def test_reset_on_user_send_clears_clock(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        scr = pilot.app.screen
        fc = _FakeClock()
        scr._reasoning_clock = ReasoningClock(clock=fc, hold=0.4)
        scr._render_event(_call_ev("session_memory", "c1"))
        fc.t = 1.0
        scr._render_event(_result_ev("c1"))
        assert scr._reasoning_clock.episode() == 1.0
        scr._reset_thinking_timer()
        assert scr._reasoning_clock.episode() == 0.0


@pytest.mark.asyncio
async def test_stream_source_pump_edge(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        scr = pilot.app.screen
        fc = _FakeClock()
        scr._reasoning_clock = ReasoningClock(clock=fc, hold=0.4)
        sink = pilot.app._stream_sink
        sink.reset()
        sink.push("think", "рассуждаю")     # reasoning_state → "thinking"
        scr._pump_reasoning_stream_source()
        assert scr._reasoning_clock.live() is True
        fc.t = 1.0
        sink.push("answer", "ответ")         # reasoning_state → "done"
        scr._pump_reasoning_stream_source()
        assert scr._reasoning_clock.episode() == 1.0


@pytest.mark.asyncio
async def test_indicator_gets_clock(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        scr = pilot.app.screen
        scr._show_thinking_indicator(state="thinking")
        assert scr._thinking._reasoning_clock is scr._reasoning_clock
