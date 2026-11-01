"""Мысли модели в ленте (спека мыслей §2, §3, §7): запись потока
``reasoning`` — истина, живая строка из стрима временная.

Под флагом `tui.show_reasoning: on` мысль идущей попытки — временная
строка `Thinking` в конце ленты; пришла запись её попытки — та же строка
становится записанной `Thought` (перед вызовами своего шага), строки
прежних попыток уходят; без записи к концу хода строки снимаются.
Повторное открытие и превью `/sessions` рисуют записи той же строкой.
Флаг выключен — записи не рисуются; смена флага перерисовывает ленту.
"""

from __future__ import annotations

import json

import pytest
from gc_core.persistence.reasoning_record import ReasoningRecord
from gc_testkit.develop_files import append_new_thread_messages
from gc_testkit.thread_writes import reasoning_record
from gc_testkit.tui_support import tui_settings, wait_until
from gc_tui.widgets.tool_call_row import ToolCallRow

TID = "thgt0000000000000000000000000001a"
OTHER = "thgt0000000000000000000000000002b"


def _write_thread(tmp_path, tid=TID):
    meta = {"thread_id": tid, "system_id": "cli"}
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _messages(tmp_path, *payloads, start_seq=0):
    append_new_thread_messages(
        str(tmp_path), "cli", TID, list(payloads), set(), start_seq=start_seq
    )


def _reply(mid="u1", text="вопрос"):
    return {"id": mid, "role": "user", "content": text}


def _call(mid="fc1", call_id="c1"):
    return {
        "id": mid,
        "type": "function_call",
        "call_id": call_id,
        "name": "read_file",
        "arguments": json.dumps({"path": "src/a.py"}),
    }


def _output(mid="fco1", call_id="c1"):
    return {
        "id": mid,
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps({"tool": "read_file", "ok": True}),
    }


def _thought(tmp_path, text, *, attempt=None, seconds=3.0, after="u1"):
    reasoning_record(
        str(tmp_path),
        "cli",
        TID,
        ReasoningRecord(text=text, duration_sec=seconds, attempt=attempt),
        after=after,
    )


def _app(tmp_path, reasoning="on", **config):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "backend.api_mode": "chat_completions",
                "tui.show_reasoning": reasoning,
                **config,
            }
        ),
    )


class _Runner:
    """Живой прогон открытого треда без исполнителя."""

    _thread_kind = "run"
    _stop_requested = False

    def __init__(self, tid=TID):
        self._last_target = (tid, "cli")
        self.running = True
        self.run_seq = 1

    def is_running(self):
        return self.running

    def turn_effective_start(self):
        return None

    def submit_ask_human_reply(self, _answer):
        pass

    def cancel_ask_human(self):
        pass

    def cancel_confirm(self):
        pass

    def add_message(self, _message):
        pass

    def stop(self):
        self.running = False


def _thoughts(scr):
    from gc_tui.widgets.thought_row import ThoughtRow

    return list(scr.query(ThoughtRow))


def _top_index(scr, widget_type) -> int:
    """Индекс верхнего виджета ленты, который есть ``widget_type`` или
    держит его; нет — -1."""
    from textual.containers import VerticalScroll

    trace = scr.query_one("#trace", VerticalScroll)
    for i, child in enumerate(trace.children):
        if isinstance(child, widget_type) or child.query(widget_type):
            return i
    return -1


def _line(row) -> str:
    """Строка мысли текстом; ещё не собрана — пусто."""
    from textual.widgets import Static

    lines = row.query(".thought-line")
    if not lines:
        return ""
    statics = lines.first().query(Static)
    return "".join(str(s.render()) for s in statics)


async def _reopen(pilot, tmp_path):
    """Переоткрыть тред через другой, как из `/sessions`."""
    _write_thread(tmp_path, OTHER)
    scr = pilot.app.screen
    scr.attach_active_task(OTHER, is_resume=True)
    scr.attach_active_task(TID, is_resume=True)
    await pilot.pause()
    return scr


# --- живая строка и запись --------------------------------------------------


@pytest.mark.asyncio
async def test_two_attempts_and_one_record_give_one_thought(tmp_path):
    """Две попытки в стриме, запись второй — одна строка `Thought` с
    текстом записи перед строкой вызова; Enter открывает мысль целиком."""
    from gc_tui.screens.viewer import ViewerScreen
    from gc_tui.widgets.thought_row import ThoughtRow
    from gc_tui.widgets.tool_call_row import ToolCallRow

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        pilot.app.active_runner = _Runner()
        sink = pilot.app._stream_sink
        sink.push("reset", "att_1")
        sink.push("think", "первая попытка")
        await wait_until(
            pilot,
            lambda: [r.attempt for r in _thoughts(scr)] == ["att_1"],
            "временная строка первой попытки",
        )
        await wait_until(
            pilot,
            lambda: _line(_thoughts(scr)[0]) == "● Thinking  первая попытка",
            "строка Thinking",
        )
        sink.push("reset", "att_2")
        sink.push("think", "вторая попытка")
        await wait_until(
            pilot,
            lambda: [r.attempt for r in _thoughts(scr)] == ["att_1", "att_2"],
            "временная строка второй попытки",
        )
        live = _thoughts(scr)[1]
        sink.push("tool", "{}")
        _thought(tmp_path, "мысль из записи", attempt="att_2", seconds=4.0)
        _messages(tmp_path, _call(), _output(), start_seq=1)
        await wait_until(
            pilot,
            lambda: (
                _top_index(scr, ToolCallRow) >= 0 and _thoughts(scr) == [live]
            ),
            "строка вызова, одна мысль",
        )
        assert live.persisted and live.event_no is not None
        assert _line(live) == "● Thought  мысль из записи · 4с"
        assert _top_index(scr, ThoughtRow) < _top_index(scr, ToolCallRow)
        live.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ViewerScreen)
        assert "мысль из записи" in pilot.app.screen._content


@pytest.mark.asyncio
async def test_live_row_survives_feed_row_of_previous_step(tmp_path):
    """Вызов и вывод тула шага 1 дочитаны одним тиком, когда стрим шага 2
    уже начался (метка пришла до первого токена): строка шага 2 есть и
    дорастает."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        pilot.app.active_runner = _Runner()
        sink = pilot.app._stream_sink
        sink.push("reset", "att_2")
        sink.push("think", "шаг два")
        _messages(tmp_path, _call(), _output(), start_seq=1)
        await wait_until(
            pilot,
            lambda: (
                [r.attempt for r in _thoughts(scr)] == ["att_2"]
                and _top_index(scr, ToolCallRow) >= 0
            ),
            "строка вызова и временная строка второго шага",
        )
        from gc_tui.widgets.thought_row import ThoughtRow

        assert _top_index(scr, ThoughtRow) > _top_index(scr, ToolCallRow)
        await pilot.pause(0.6)
        sink.push("think", " дальше")
        await wait_until(
            pilot,
            lambda: "дальше" in _line(_thoughts(scr)[0]),
            "строка дорастает",
        )


@pytest.mark.asyncio
async def test_live_row_moves_below_rows_read_in_later_tick(tmp_path):
    """Строка идущей попытки встала, строки прошлого шага дочитаны позже:
    та же строка уходит под них — идущая попытка после всего записанного."""
    from gc_tui.widgets.thought_row import ThoughtRow

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        pilot.app.active_runner = _Runner()
        sink = pilot.app._stream_sink
        sink.push("reset", "att_2")
        sink.push("think", "шаг два")
        await wait_until(
            pilot, lambda: len(_thoughts(scr)) == 1, "временная строка"
        )
        live = _thoughts(scr)[0]
        _messages(tmp_path, _call(), _output(), start_seq=1)
        await wait_until(
            pilot,
            lambda: _top_index(scr, ToolCallRow) >= 0,
            "строка вызова прошлого шага",
        )
        await pilot.pause()
        assert _thoughts(scr) == [live] and not live.persisted
        assert _top_index(scr, ThoughtRow) > _top_index(scr, ToolCallRow)


@pytest.mark.asyncio
async def test_feed_row_keeps_next_answer_preview(tmp_path):
    """Под флагом мыслей строка шага 1 не глушит превью ответа шага 2:
    ответ, пришедший после неё, виден."""
    from textual.widgets import Static

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    app = _app(tmp_path, **{"tui.stream_display": "answer"})
    async with app.run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        pilot.app.active_runner = _Runner()
        sink = pilot.app._stream_sink
        sink.push("reset", "att_2")
        sink.push("think", "шаг два")
        _messages(tmp_path, _call(), _output(), start_seq=1)
        await wait_until(
            pilot,
            lambda: _top_index(scr, ToolCallRow) >= 0,
            "строки шага 1",
        )
        await pilot.pause(0.3)
        sink.push("answer", "ответ шага два")

        def shown() -> bool:
            row = scr._stream_answer_row
            if row is None:
                return False
            content = row.query(".trace-content")
            return bool(content) and "ответ шага два" in str(
                content.first(Static).render()
            )

        await wait_until(pilot, shown, "превью ответа шага 2")


@pytest.mark.asyncio
async def test_persisted_row_not_duplicated_by_kept_thought(tmp_path):
    """Запись попытки пришла, а мысль этой попытки ещё в стриме: строка
    одна."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        pilot.app.active_runner = _Runner()
        sink = pilot.app._stream_sink
        sink.push("reset", "att_1")
        sink.push("think", "мысль")
        _thought(tmp_path, "мысль", attempt="att_1")
        _messages(tmp_path, _call(), _output(), start_seq=1)
        await wait_until(
            pilot,
            lambda: _top_index(scr, ToolCallRow) >= 0,
            "строки шага",
        )
        await pilot.pause(0.6)
        rows = _thoughts(scr)
        assert len(rows) == 1 and rows[0].persisted


@pytest.mark.asyncio
async def test_old_record_keeps_current_live_row(tmp_path):
    """Запись прежней попытки без своей строки, а уже идёт новая: живая
    строка остаётся, записанная встаёт над ней."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        pilot.app.active_runner = _Runner()
        sink = pilot.app._stream_sink
        sink.push("reset", "att_2")
        sink.push("think", "новая мысль")
        await wait_until(
            pilot, lambda: len(_thoughts(scr)) == 1, "временная строка"
        )
        live = _thoughts(scr)[0]
        _thought(tmp_path, "старая мысль", attempt="att_1")
        await wait_until(
            pilot, lambda: len(_thoughts(scr)) == 2, "записанная мысль"
        )
        old, current = _thoughts(scr)
        assert current is live and not live.persisted
        assert old.persisted and old.attempt == "att_1"


@pytest.mark.asyncio
async def test_stop_while_thinking_leaves_no_thought(tmp_path):
    """Стоп во время размышления, записи нет — после ухода в idle строк
    мыслей нет."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        runner = _Runner()
        pilot.app.active_runner = runner
        sink = pilot.app._stream_sink
        sink.push("reset", "att_1")
        sink.push("think", "думаю")
        await wait_until(
            pilot, lambda: len(_thoughts(scr)) == 1, "временная строка"
        )
        runner.running = False
        await wait_until(
            pilot, lambda: not _thoughts(scr), "строки мыслей сняты"
        )


@pytest.mark.asyncio
async def test_reopen_places_thought_before_its_step(tmp_path):
    """Тред с записью мысли открыт заново — `Thought` на месте записи,
    перед вызовом своего шага, тем же виджетом."""
    from gc_tui.widgets.thought_row import ThoughtRow
    from gc_tui.widgets.tool_call_row import ToolCallRow

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    _thought(tmp_path, "план", attempt="att_1", seconds=2.0)
    _messages(tmp_path, _call(), _output(), start_seq=1)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _reopen(pilot, tmp_path)
        rows = _thoughts(scr)
        assert len(rows) == 1 and rows[0].persisted
        assert _line(rows[0]) == "● Thought  план · 2с"
        assert 0 <= _top_index(scr, ThoughtRow) < _top_index(scr, ToolCallRow)


@pytest.mark.asyncio
async def test_flag_off_hides_records_and_toggle_shows(tmp_path):
    """Флаг выключен — записи не рисуются; включили в сессии — видны;
    выключили — снова скрыты."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    _thought(tmp_path, "скрытая мысль", attempt="att_1")
    _messages(tmp_path, _call(), _output(), start_seq=1)
    async with _app(tmp_path, reasoning="off").run_test(
        size=(100, 40)
    ) as pilot:
        scr = await _reopen(pilot, tmp_path)
        await pilot.pause(0.3)
        assert not _thoughts(scr)
        pilot.app.set_session_override("tui.show_reasoning", "on")
        await wait_until(
            pilot, lambda: len(_thoughts(scr)) == 1, "мысль видна"
        )
        pilot.app.set_session_override("tui.show_reasoning", "off")
        await wait_until(pilot, lambda: not _thoughts(scr), "мысль скрыта")


@pytest.mark.asyncio
async def test_flag_off_creates_no_live_row(tmp_path):
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path, reasoning="off").run_test(
        size=(100, 40)
    ) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        pilot.app.active_runner = _Runner()
        pilot.app._stream_sink.push("reset", "att_1")
        pilot.app._stream_sink.push("think", "думаю")
        await pilot.pause(0.6)
        assert not _thoughts(scr)


@pytest.mark.asyncio
async def test_no_stream_record_gives_thought_without_timer(tmp_path):
    """Стриминг выключен — временной строки нет, запись без длительности
    даёт `Thought` без таймера."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path, **{"backend.stream": False}).run_test(
        size=(100, 40)
    ) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        pilot.app.active_runner = _Runner()
        await pilot.pause(0.3)
        assert not _thoughts(scr)
        _thought(tmp_path, "без стрима", attempt="att_1", seconds=None)
        _messages(tmp_path, _call(), _output(), start_seq=1)
        await wait_until(
            pilot, lambda: len(_thoughts(scr)) == 1, "мысль из записи"
        )
        row = _thoughts(scr)[0]
        assert row.persisted
        await wait_until(
            pilot,
            lambda: _line(row) == "● Thought  без стрима",
            "строка Thought без таймера",
        )


@pytest.mark.asyncio
async def test_thought_text_is_dim_from_styles(tmp_path):
    """Хвост мысли серый — `$dim` из styles.tcss окна."""
    from gc_tui.palette import DIM
    from textual.color import Color

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    _thought(tmp_path, "серая мысль", attempt="att_1")
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _reopen(pilot, tmp_path)
        tail = _thoughts(scr)[0].query_one(".thought-text")
        assert tail.rich_style.color == Color.parse(DIM).rich_color


# --- хранилище и превью -----------------------------------------------------


def test_store_places_thought_after_anchor(tmp_path):
    """Сегменты и полное чтение ставят запись мысли за сообщением
    привязки, перед сообщениями шага."""
    from gc_tui.services.stores.file_store import FileTaskStore

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    _thought(tmp_path, "план", attempt="att_1")
    _messages(tmp_path, _call(), _output(), start_seq=1)
    store = FileTaskStore(str(tmp_path))
    records, _cursor = store.read_all_events(TID)
    seg = store.read_segments(TID)[0]["messages"]
    for recs in (records, seg):
        kinds = [r.get("type") for r in recs]
        assert kinds == ["message", "thought", "message", "message"]
        assert recs[1]["thought"]["text"] == "план"


def _segments(tmp_path):
    from gc_tui.services.stores.file_store import FileTaskStore

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    _thought(tmp_path, "мысль в превью", attempt="att_1", seconds=5.0)
    _messages(tmp_path, _call(), _output(), start_seq=1)
    return FileTaskStore(str(tmp_path)).read_segments(TID)


def _preview_app(segments, *, show):
    from gc_tui.services.trace_render import render_segments_readonly
    from textual.app import App
    from textual.containers import VerticalScroll

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_segments_readonly(
                self.query_one("#t"), segments, show_thoughts=show
            )

    return _A()


@pytest.mark.asyncio
async def test_preview_shows_thought(tmp_path):
    from gc_tui.widgets.thought_row import ThoughtRow

    app = _preview_app(_segments(tmp_path), show=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        rows = list(app.query(ThoughtRow))
        assert len(rows) == 1 and rows[0].persisted
        assert not rows[0].can_focus
        assert _line(rows[0]) == "● Thought  мысль в превью · 5с"


@pytest.mark.asyncio
async def test_preview_hides_thought_when_flag_off(tmp_path):
    from gc_tui.widgets.thought_row import ThoughtRow

    app = _preview_app(_segments(tmp_path), show=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not list(app.query(ThoughtRow))
