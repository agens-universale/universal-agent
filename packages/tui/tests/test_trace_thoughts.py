"""Мысли модели в ленте (спека мыслей §2, §3, §7): только из стрима,
нигде не хранятся.

Под флагом `tui.show_reasoning: on` мысль идущей попытки — строка
`Thinking` в конце ленты; новая попытка оставляет прежнюю строку на месте
законченной `Thought`; к концу хода строки остаются. После перерисовки
ленты из треда (переоткрытие) мыслей нет. Смена флага скрывает и
возвращает строки сессии, ленту не перерисовывает.
"""

from __future__ import annotations

import json

import pytest
from gc_testkit.develop_files import append_new_thread_messages
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


# --- живая строка ------------------------------------------------------------


async def _live(pilot, text="думаю"):
    """Открытый тред с идущим прогоном и мыслью новой попытки в стриме."""
    scr = pilot.app.screen
    scr.attach_active_task(TID)
    runner = _Runner()
    pilot.app.active_runner = runner
    sink = pilot.app._stream_sink
    sink.push("reset", "")
    sink.push("think", text)
    return scr, sink, runner


@pytest.mark.asyncio
async def test_new_attempt_keeps_previous_thought(tmp_path):
    """Две попытки в стриме — две строки: прежняя законченная `Thought` на
    своём месте, новая `Thinking` за ней."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr, sink, _runner = await _live(pilot, "первая попытка")
        await wait_until(
            pilot,
            lambda: (
                len(_thoughts(scr)) == 1
                and _line(_thoughts(scr)[0]) == "● Thinking  первая попытка"
            ),
            "строка первой попытки",
        )
        sink.push("reset", "")
        sink.push("think", "вторая попытка")
        await wait_until(
            pilot, lambda: len(_thoughts(scr)) == 2, "строка второй попытки"
        )
        first, second = _thoughts(scr)
        assert first.settled and not second.settled
        assert _line(first).startswith("● Thought  первая попытка")
        await wait_until(
            pilot,
            lambda: _line(second).startswith("● Thinking  вторая попытка"),
            "строка Thinking второй попытки",
        )


@pytest.mark.asyncio
async def test_thought_stays_above_its_step_rows(tmp_path):
    """Мысль встала, потом пришли вызов и вывод её шага — мысль над ними
    и не переставляется."""
    from gc_tui.widgets.thought_row import ThoughtRow

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr, sink, _runner = await _live(pilot, "план")
        await wait_until(pilot, lambda: len(_thoughts(scr)) == 1, "мысль")
        sink.push("tool", "{}")
        _messages(tmp_path, _call(), _output(), start_seq=1)
        await wait_until(
            pilot,
            lambda: _top_index(scr, ToolCallRow) >= 0,
            "строка вызова шага",
        )
        await pilot.pause(0.3)
        assert len(_thoughts(scr)) == 1
        assert _top_index(scr, ThoughtRow) < _top_index(scr, ToolCallRow)


@pytest.mark.asyncio
async def test_previous_step_rows_read_with_new_thought_stand_above(tmp_path):
    """Вызов и вывод шага 1 дочитаны тем же тиком, в котором пришла мысль
    шага 2: строки шага 1 выше мысли, мысль дорастает."""
    from gc_tui.widgets.thought_row import ThoughtRow

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        _messages(tmp_path, _call(), _output(), start_seq=1)
        scr, sink, _runner = await _live(pilot, "шаг два")
        await wait_until(
            pilot,
            lambda: (
                len(_thoughts(scr)) == 1 and _top_index(scr, ToolCallRow) >= 0
            ),
            "строка вызова и мысль второго шага",
        )
        assert _top_index(scr, ThoughtRow) > _top_index(scr, ToolCallRow)
        sink.push("think", " дальше")
        await wait_until(
            pilot,
            lambda: "дальше" in _line(_thoughts(scr)[0]),
            "строка дорастает",
        )


@pytest.mark.asyncio
async def test_feed_row_keeps_next_answer_preview(tmp_path):
    """Под флагом мыслей строка шага 1 не глушит превью ответа шага 2:
    ответ, пришедший после неё, виден."""
    from textual.widgets import Static

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    app = _app(tmp_path, **{"tui.stream_display": "answer"})
    async with app.run_test(size=(100, 40)) as pilot:
        scr, sink, _runner = await _live(pilot, "шаг два")
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
async def test_stop_while_thinking_keeps_finished_thought(tmp_path):
    """Стоп во время размышления — строка остаётся законченной `Thought`;
    в треде мысль не записана."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr, _sink, runner = await _live(pilot)
        await wait_until(pilot, lambda: len(_thoughts(scr)) == 1, "мысль")
        runner.running = False
        await wait_until(
            pilot, lambda: _thoughts(scr)[0].settled, "строка законченная"
        )
        assert _line(_thoughts(scr)[0]).startswith("● Thought  думаю")
    assert not list(tmp_path.glob("stream_reasoning_*"))


@pytest.mark.asyncio
async def test_reopen_has_no_thoughts(tmp_path):
    """Мысли нигде не хранятся: тред открыт заново — строк мыслей нет."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr, _sink, runner = await _live(pilot)
        await wait_until(pilot, lambda: len(_thoughts(scr)) == 1, "мысль")
        runner.running = False
        pilot.app.active_runner = None
        scr = await _reopen(pilot, tmp_path)
        await pilot.pause(0.3)
        assert not _thoughts(scr)


@pytest.mark.asyncio
async def test_flag_toggle_hides_and_returns_rows(tmp_path):
    """Флаг выключили в сессии — строки мыслей скрыты, включили — те же
    строки видны снова; лента не перерисовывается."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr, _sink, _runner = await _live(pilot)
        await wait_until(pilot, lambda: len(_thoughts(scr)) == 1, "мысль")
        row = _thoughts(scr)[0]
        pilot.app.set_session_override("tui.show_reasoning", "off")
        await wait_until(pilot, lambda: row.display is False, "мысль скрыта")
        pilot.app.set_session_override("tui.show_reasoning", "on")
        await wait_until(pilot, lambda: row.display is True, "мысль видна")
        assert _thoughts(scr) == [row]


@pytest.mark.asyncio
async def test_flag_off_creates_no_live_row(tmp_path):
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path, reasoning="off").run_test(
        size=(100, 40)
    ) as pilot:
        scr, _sink, _runner = await _live(pilot)
        await pilot.pause(0.6)
        assert not _thoughts(scr)


@pytest.mark.asyncio
async def test_thought_text_is_dim_from_styles(tmp_path):
    """Хвост мысли серый — `$dim` из styles.tcss окна."""
    from gc_tui.palette import DIM
    from textual.color import Color

    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr, _sink, _runner = await _live(pilot, "серая мысль")
        await wait_until(pilot, lambda: len(_thoughts(scr)) == 1, "мысль")
        tail = _thoughts(scr)[0].query_one(".thought-text")
        assert tail.rich_style.color == Color.parse(DIM).rich_color


@pytest.mark.asyncio
async def test_reasoning_tag_lines_not_shown(tmp_path):
    """Строки из одного тега рассуждения (`</thinking>` от модели) в
    строке мысли не видны; стрим из одних тегов строки не даёт."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply())
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr, sink, _runner = await _live(pilot, "</thinking>\n")
        await pilot.pause(0.6)
        assert not _thoughts(scr)
        sink.push("think", "план\n</thinking>")
        await wait_until(pilot, lambda: len(_thoughts(scr)) == 1, "мысль")
        await wait_until(
            pilot,
            lambda: _line(_thoughts(scr)[0]) == "● Thinking  план",
            "строка без тегов",
        )
