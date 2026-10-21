"""Отказ вызова в ленте — не ошибка (спека P3 §10, смоук P3a).

Язык глифов ленты — круги. Отказ политики, режима, судьи или человека:
глиф `○` приглушённым оранжевым (`REFUSED`), строка `└` серым (`DIM`) и
начинается с того, кто отказал. Успех — `●` `OK`, ошибка — `●` и `ERR`,
стоп и лимит — `○` серым; несостоявшаяся проверка судьи — не отказ, а
остановка `○`. Каждый путь одинаково: вживую, превью, вложенный вызов
из записи субагента."""

from __future__ import annotations

import json

import pytest
from gc_core.message_policy.permission_denials import (
    DECLINED_REASON,
    SESSION_DENIED_REASON,
    TIMED_OUT_REASON,
    UNATTENDED_REASON,
    declined_error,
    session_denied_error,
    timed_out_error,
    unattended_error,
)
from gc_core.security.action_judge_step import CHECK_FAILED_TYPE
from gc_tui.models import TraceEvent
from gc_tui.palette import DIM, ERR, OK, REFUSED
from gc_tui.services.trace_mapper import map_message
from gc_tui.services.trace_render import (
    _dispatch_readonly,
    build_consequence_line,
    merge_nested_readonly,
)
from gc_tui.widgets.tool_call_row import ToolCallRow
from rich.text import Text

_REFUSALS = [
    (
        {"type": "action_blocked", "reason": "Deletes files outside repo."},
        "отклонил судья auto: Deletes files outside repo.",
    ),
    (declined_error(), "вы отклонили"),
    (session_denied_error(), "вы запретили в этой сессии"),
    (unattended_error(), "нужно подтверждение — спросить некого"),
    (timed_out_error(), "нет ответа на подтверждение"),
    (
        {"type": "write_denied_by_mode", "mode": "plan", "reason": "x"},
        "режим plan: запись запрещена",
    ),
    (
        {"type": "read_denied_by_mode", "mode": "manual", "reason": "x"},
        "режим manual: чтение запрещено",
    ),
    (
        {"type": "exec_denied_by_mode", "mode": "plan", "reason": "x"},
        "режим plan: запуск команд запрещён",
    ),
    (
        {"type": "network_denied_by_mode", "mode": "plan", "reason": "x"},
        "режим plan: сетевые запросы запрещены",
    ),
    (
        {
            "type": "permission_denied_by_mode",
            "mode": "plan",
            "category": "mcp",
            "reason": "x",
        },
        "режим plan: действие запрещено",
    ),
    (
        {"type": "write_denied_by_mode", "reason": "x"},
        "режим: запись запрещена",
    ),
    (
        {"type": "redirect_to_other_host", "reason": "x"},
        "режим: переход на другой хост не выполнен",
    ),
    (
        {
            "type": "protected_path_denied",
            "path": "/p/.gc/config.yml",
            "reason": "Path is protected (operational .gc / config).",
        },
        "защита путей: Path is protected (operational .gc / config).",
    ),
    (
        {
            "type": "write_guard_violation",
            "path": "/etc/x",
            "message": "Write operation is outside allowed perimeter.",
        },
        "защита путей: Write operation is outside allowed perimeter.",
    ),
    (
        {
            "type": "read_guard_violation",
            "path": "/etc/shadow",
            "reason": "path is within denied root: /etc",
            "mode": "deny",
        },
        "защита путей: path is within denied root: /etc",
    ),
    (
        {"type": "read_denied", "path": "thread-file://a", "message": "m"},
        "защита путей: m",
    ),
    (
        {"type": "unsafe_command_blocked", "reason": "rm -rf is dangerous"},
        "модерация команд: rm -rf is dangerous",
    ),
    (
        {"type": "url_blocked_by_policy", "reason": "matched blacklist"},
        "политика адресов: matched blacklist",
    ),
]

#: Гард после своего вопроса оставляет свой тип, а причину берёт по
#: исходу вопроса: кто отказал — по исходу, не по типу.
_GUARD_AFTER_QUESTION = [
    (DECLINED_REASON, "вы отклонили"),
    (SESSION_DENIED_REASON, "вы запретили в этой сессии"),
    (UNATTENDED_REASON, "нужно подтверждение — спросить некого"),
    (TIMED_OUT_REASON, "нет ответа на подтверждение"),
]


def _styles_of(text: Text, fragment: str) -> list[str]:
    return [
        str(s.style)
        for s in text.spans
        if fragment in text.plain[s.start : s.end]
    ]


def _lines(row: ToolCallRow) -> Text:
    return Text("\n").join(
        build_consequence_line(row._level, body).text
        for body in row._consequence_bodies()
    )


def _output(error: dict) -> str:
    return json.dumps({"tool": "fs_write_file", "ok": False, "error": error})


def _output_event(error: dict) -> TraceEvent:
    msg = {
        "type": "function_call_output",
        "name": "fs_write_file",
        "call_id": "c1",
        "output": _output(error),
    }
    return map_message(msg, no=2, ts="")


def _assert_refused_row(row: ToolCallRow, text: str, indent: str) -> None:
    prefix = row._render_prefix()
    assert _styles_of(prefix, "○") == [REFUSED]
    assert "●" not in prefix.plain
    line = _lines(row)
    assert line.plain == f"{indent}└ {text}"
    assert _styles_of(line, text) == [DIM]


# --- маппер: классификация в одном месте ------------------------------------


@pytest.mark.parametrize("error, text", _REFUSALS)
def test_mapper_marks_refusal_with_who_and_why(error, text):
    ev = _output_event(error)
    assert ev.kind == "error"
    assert ev.result_outcome == "refused"
    assert ev.summary == text


@pytest.mark.parametrize("reason, text", _GUARD_AFTER_QUESTION)
@pytest.mark.parametrize(
    "guard_type",
    [
        "protected_path_denied",
        "write_guard_violation",
        "unsafe_command_cancelled_by_user",
    ],
)
def test_guard_refusal_after_question_names_the_answer(
    guard_type, reason, text
):
    ev = _output_event({"type": guard_type, "reason": reason})
    assert ev.result_outcome == "refused"
    assert ev.summary == text


def test_check_failed_type_matches_core():
    from gc_tui.services import trace_mapper

    assert trace_mapper.CHECK_FAILED_TYPE == CHECK_FAILED_TYPE


def test_check_failed_is_halted_not_refused():
    ev = _output_event({"type": CHECK_FAILED_TYPE, "reason": "x"})
    assert ev.result_outcome == "halted"
    assert ev.summary == "проверка не прошла — агент может повторить"


def test_mapper_real_error_stays_error():
    ev = _output_event({"type": "not_found", "message": "нет файла"})
    assert ev.result_outcome == "error"
    assert ev.summary == "нет файла"


def test_mapper_stop_stays_halted():
    msg = {
        "type": "function_call_output",
        "name": "fs_read_file",
        "call_id": "c1",
        "output": json.dumps({"ok": False, "status": "step_limit"}),
    }
    assert map_message(msg, no=2, ts="").result_outcome == "halted"


# --- живые факты исхода ------------------------------------------------------

_STOPS = [
    json.dumps({"ok": False, "status": "step_limit"}),
    json.dumps({"ok": False, "status": "cancelled_by_user"}),
    json.dumps({"ok": False, "error": {"type": CHECK_FAILED_TYPE}}),
    _output({"type": "not_found", "message": "нет файла"}),
    _output({"type": "action_blocked", "reason": "очень " * 30 + "\nконец"}),
] + [_output(error) for error, _text in _REFUSALS + _GUARD_AFTER_QUESTION]


@pytest.mark.parametrize("output", _STOPS)
def test_live_facts_classify_as_output(output):
    """Факты события прогресса ядра (`result_facts`) и разбор вывода при
    перерисовке дают один исход: классификатор один."""
    from gc_core.execution.progress import result_facts
    from gc_tui.services.trace_mapper import classify_stop, result_stop

    assert classify_stop(*result_facts(output)) == result_stop(output)


def test_classify_stop_without_facts_is_plain_error():
    from gc_tui.services.trace_mapper import classify_stop

    assert classify_stop(None, None) is None


# --- строка вызова -----------------------------------------------------------


def test_refused_row_is_warn_glyph_and_dim_line():
    row = ToolCallRow(call_id="c1", label="Write", target="a.txt")
    row.set_result(summary="вы отклонили", outcome="refused")
    assert row._render_content().plain == "Write  a.txt"
    _assert_refused_row(row, "вы отклонили", "  ")


def test_ok_row_is_green_circle():
    row = ToolCallRow(call_id="c1", label="Read", target="x")
    row.set_result(summary="188 строк", outcome="ok")
    assert _styles_of(row._render_prefix(), "●") == [OK]


def test_error_row_is_red_circle_and_err_line():
    row = ToolCallRow(call_id="c1", label="Read", target="x")
    row.set_result(summary="нет файла", outcome="error")
    prefix = row._render_prefix()
    assert _styles_of(prefix, "●") == [ERR]
    assert "✕" not in prefix.plain
    assert _styles_of(_lines(row), "нет файла") == [ERR]


def test_halted_row_is_grey_hollow_circle():
    row = ToolCallRow(call_id="c1", label="Read", target="x")
    row.set_result(summary="отменено", outcome="halted")
    assert _styles_of(row._render_prefix(), "○") == [DIM]
    assert _styles_of(_lines(row), "отменено") == [DIM]


# --- превью ------------------------------------------------------------------


class _Target:
    def __init__(self):
        self.children: list = []

    def mount(self, widget, after=None):
        if after is None:
            self.children.append(widget)
        else:
            self.children.insert(self.children.index(after) + 1, widget)


def _call_event() -> TraceEvent:
    return map_message(
        {
            "type": "function_call",
            "name": "fs_write_file",
            "call_id": "c1",
            "arguments": json.dumps({"path": "a.txt", "content": "x"}),
        },
        no=1,
        ts="",
    )


@pytest.mark.parametrize("error, text", _REFUSALS[:3])
def test_preview_refusal_row(error, text):
    target, calls = _Target(), {}
    _dispatch_readonly(target, _call_event(), calls)
    _dispatch_readonly(target, _output_event(error), calls)
    (row,) = target.children
    _assert_refused_row(row, text, "  ")


def _sidecar(seq: int, kind: str, payload: dict) -> dict:
    return {
        "parent_call_id": "p1",
        "seq": seq,
        "kind": kind,
        "payload": payload,
    }


def _nested_records(error: dict) -> list[dict]:
    return [
        _sidecar(1, "call", {"call_id": "n1", "name": "fs_write_file"}),
        _sidecar(2, "error", {"call_id": "n1", "output": _output(error)}),
    ]


def test_preview_nested_refusal_row():
    target, calls = _Target(), {}
    parent = ToolCallRow(call_id="p1", label="Agent", target="")
    target.mount(parent)
    calls["p1"] = parent
    merge_nested_readonly(target, _nested_records(declined_error()), calls)
    _assert_refused_row(target.children[1], "вы отклонили", "    ")


def test_preview_nested_error_is_red_circle():
    target, calls = _Target(), {}
    parent = ToolCallRow(call_id="p1", label="Agent", target="")
    target.mount(parent)
    calls["p1"] = parent
    error = {"type": "not_found", "message": "нет файла"}
    merge_nested_readonly(target, _nested_records(error), calls)
    assert _styles_of(target.children[1]._render_prefix(), "●") == [ERR]


# --- вживую ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_refusal_row(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_event(_call_event())
        scr._render_event(
            _output_event({"type": "action_blocked", "reason": "risky"})
        )
        await pilot.pause()
        (row,) = [r for r in scr.query(ToolCallRow) if r.call_id == "c1"]
        _assert_refused_row(row, "отклонил судья auto: risky", "  ")


@pytest.mark.asyncio
async def test_live_nested_refusal_from_subagent_record():
    """Переоткрытие идущего субагента: вложенный отказ из записи — `○`."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.subagent_sidecar_mapper import (
        apply_sidecar_nested_actions,
        plan_sidecar_nested_actions,
    )
    from textual.containers import VerticalScroll

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        parent = ToolCallRow(call_id="p1", label="Agent", target="")
        trace.mount(parent)
        scr._active_calls["p1"] = parent
        await pilot.pause()
        apply_sidecar_nested_actions(
            plan_sidecar_nested_actions(
                _nested_records(session_denied_error())
            ),
            mount_nested_tool=scr._mount_nested_tool,
            finish_nested_tool=scr._finish_nested_tool,
            mount_nested_prose=lambda **_kw: None,
        )
        await pilot.pause()
        row = scr._nested_tool_rows["n1"]
        _assert_refused_row(row, "вы запретили в этой сессии", "    ")
