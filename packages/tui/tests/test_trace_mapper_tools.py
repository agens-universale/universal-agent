"""trace_mapper: function_call/output, правила извлечения главного аргумента (§3)."""

import json

from gc_tui.services.trace_mapper import map_message


def test_read_file_shows_path():
    ev = map_message(
        {"type": "function_call", "name": "read_file", "arguments": json.dumps({"path": "src/cli.py"})},
        no=1,
        ts="t",
    )
    assert ev.kind == "call"
    assert ev.tool_name == "read_file"
    assert "src/cli.py" in ev.summary


def test_write_to_file_shows_path_and_chars():
    body = "x" * 12400
    ev = map_message(
        {
            "type": "function_call",
            "name": "write_to_file",
            "arguments": json.dumps({"path": "report.md", "content": body}),
        },
        no=2,
        ts="t",
    )
    assert "report.md" in ev.summary
    assert "<12400 chars>" in ev.summary


def test_search_files_query_or_pattern():
    ev = map_message(
        {"type": "function_call", "name": "search_files", "arguments": json.dumps({"query": "AgentExecutor"})},
        no=3,
        ts="t",
    )
    assert "AgentExecutor" in ev.summary


def test_execute_command_shows_command():
    ev = map_message(
        {"type": "function_call", "name": "execute_command", "arguments": json.dumps({"command": "pytest tests/"})},
        no=4,
        ts="t",
    )
    assert "pytest tests/" in ev.summary


def test_fetch_url():
    ev = map_message(
        {"type": "function_call", "name": "fetch_url", "arguments": json.dumps({"url": "https://example.com"})},
        no=5,
        ts="t",
    )
    assert "https://example.com" in ev.summary


def test_memory_files_action_name():
    ev = map_message(
        {
            "type": "function_call",
            "name": "memory_files",
            "arguments": json.dumps({"action": "append", "name": "journal"}),
        },
        no=6,
        ts="t",
    )
    assert "append" in ev.summary
    assert "journal" in ev.summary


def test_unknown_tool_default():
    ev = map_message(
        {"type": "function_call", "name": "mystery", "arguments": json.dumps({"foo": "bar", "baz": "qux"})},
        no=7,
        ts="t",
    )
    assert ev.tool_name == "mystery"
    assert "mystery" in ev.summary


def test_submit_solution_surfaces_task_report_as_agent():
    ev = map_message(
        {
            "type": "function_call",
            "name": "submit_solution",
            "arguments": json.dumps(
                {
                    "solution_type": "full_solution",
                    "task_report": "готово: всё работает",
                }
            ),
        },
        no=8,
        ts="t",
    )
    assert ev.kind == "agent"
    assert "готово" in ev.summary


def test_function_call_output_ok():
    ev = map_message(
        {"type": "function_call_output", "call_id": "fc_1", "output": json.dumps({"ok": True, "lines": 312})},
        no=9,
        ts="t",
    )
    assert ev.kind == "result"
    assert ev.call_id == "fc_1"


def test_function_call_output_error():
    ev = map_message(
        {"type": "function_call_output", "call_id": "fc_2", "output": json.dumps({"ok": False, "error": "ENOENT"})},
        no=10,
        ts="t",
    )
    assert ev.kind == "error"
    assert "ENOENT" in ev.summary


def test_function_call_output_command_moderation_block_is_error_not_agent():
    """Новая fail-closed форма execute_command (read_guard PR#17): blocked-вывод
    с dict-error рендерится как error, НЕ как финальный ответ, без краша."""
    out = json.dumps(
        {
            "tool": "exec_command",
            "ok": False,
            "command": "rm -rf /",
            "cwd": "/work",
            "exit_code": None,
            "timed_out": False,
            "error": {
                "type": "unsafe_command_blocked",
                "mode": "deny",
                "reason": "Judge is unavailable; fail-closed policy blocks execution.",
                "risk_flags": [],
            },
            "stdout": "",
            "stderr": "",
        }
    )
    ev = map_message(
        {"type": "function_call_output", "name": "exec_command", "call_id": "fc_x", "output": out}, no=1, ts="t"
    )
    assert ev.kind == "error"
    assert "unsafe_command_blocked" in ev.summary


def test_function_call_output_read_guard_block_is_error():
    """read_guard-блок (fs_read_file) с dict-error рендерится как error."""
    out = json.dumps(
        {
            "tool": "fs_read_file",
            "ok": False,
            "status": "blocked",
            "error": {
                "type": "read_guard_violation",
                "path": "/etc/shadow",
                "operation": "read",
                "reason": "path is within denied root: /etc",
                "mode": "deny",
            },
        }
    )
    ev = map_message(
        {"type": "function_call_output", "name": "fs_read_file", "call_id": "fc_y", "output": out}, no=2, ts="t"
    )
    assert ev.kind == "error"
    assert "read_guard_violation" in ev.summary


# --- Task 2.2: ask_human / submit_solution special cases ---


def test_ask_human_call_returns_kind_ask_human():
    msg = {
        "type": "function_call",
        "name": "ask_human",
        "call_id": "c1",
        "arguments": json.dumps({"question": "Какой тип отчёта?\nA) ...\nB) ..."}),
    }
    ev = map_message(msg, no=5, ts="2026-05-21T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "ask_human"
    assert "Какой тип отчёта" in ev.summary
    assert ev.call_id == "c1"


def test_ask_human_empty_question_returns_none():
    msg = {
        "type": "function_call",
        "name": "ask_human",
        "call_id": "c1",
        "arguments": json.dumps({"question": ""}),
    }
    ev = map_message(msg, no=5, ts="t")
    assert ev is None


def test_submit_solution_returns_agent_with_comment_attached():
    msg = {
        "type": "function_call",
        "name": "submit_solution",
        "call_id": "c3",
        "arguments": json.dumps(
            {
                "solution_type": "answer_question",
                "task_report": "Проект проанализирован.",
                "comment": "Найдена папка planner/ не упомянутая в README.",
            }
        ),
    }
    ev = map_message(msg, no=10, ts="2026-05-21T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "agent"
    assert ev.summary == "Проект проанализирован."
    assert ev.full_payload["_submit_comment"] == "Найдена папка planner/ не упомянутая в README."


def test_submit_solution_output_with_name_returns_none():
    msg = {
        "type": "function_call_output",
        "call_id": "c3",
        "name": "submit_solution",
        "output": json.dumps({"tool": "submit_solution", "ok": True}),
    }
    ev = map_message(msg, no=11, ts="2026-05-21T12:00:00+00:00")
    assert ev is None


def test_submit_solution_without_task_report_returns_none():
    msg = {
        "type": "function_call",
        "name": "submit_solution",
        "call_id": "c3",
        "arguments": json.dumps({"solution_type": "answer_question", "task_report": ""}),
    }
    ev = map_message(msg, no=10, ts="2026-05-21T12:00:00+00:00")
    assert ev is None


def test_submit_solution_no_comment_no_sidecar():
    msg = {
        "type": "function_call",
        "name": "submit_solution",
        "call_id": "c3",
        "arguments": json.dumps(
            {
                "solution_type": "answer_question",
                "task_report": "Done.",
            }
        ),
    }
    ev = map_message(msg, no=10, ts="2026-05-21T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "agent"
    assert "_submit_comment" not in ev.full_payload


def test_intermediate_reply_output_suppressed_by_name():
    ev = map_message(
        {"type": "function_call_output", "name": "intermediate_reply", "call_id": "c1", "output": '{"ok": true}'},
        no=1,
        ts="t",
    )
    assert ev is None


def test_intermediate_reply_output_suppressed_chat_mode_by_tool():
    # chat_completions: у output НЕТ name, но в JSON есть "tool" → тоже глушим
    # (иначе вывод turn_intermediate_reply рендерится паразитной строкой `  |- …`).
    ev = map_message(
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps({"tool": "turn_intermediate_reply", "ok": True, "answer": "x"}),
        },
        no=1,
        ts="t",
    )
    assert ev is None


# --- Phase 2 hot-fix: _summarize_output friendly keys + empty fallback ---


def test_summarize_output_total_lines():
    """read_file output has total_lines — должно мапиться в '312 строк'."""
    msg = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": json.dumps({"tool": "read_file", "ok": True, "total_lines": 312}),
    }
    ev = map_message(msg, no=5, ts="2026-05-21T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "result"
    assert "312 строк" in ev.summary


def test_summarize_output_entries_uses_count():
    """list_files: entries list with explicit count → 'N entries'."""
    msg = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": json.dumps(
            {
                "tool": "list_files",
                "ok": True,
                "count": 7,
                "entries": ["a", "b", "c", "d", "e", "f", "g"],
            }
        ),
    }
    ev = map_message(msg, no=5, ts="2026-05-21T12:00:00+00:00")
    assert ev is not None
    assert "7 entries" in ev.summary


def test_summarize_output_empty_when_no_known_keys():
    """Если нет распознаваемых ключей — summary пустой (widget уберёт `-> ...`)."""
    msg = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": json.dumps({"tool": "skills_load", "ok": True}),
    }
    ev = map_message(msg, no=5, ts="2026-05-21T12:00:00+00:00")
    assert ev is not None
    assert ev.summary == ""


def test_policy_technical_message_is_hidden_system():
    """Bounce-сообщение (<technical type="technical">) → kind=system, hidden=True.
    kind сохраняется для polling/thinking-indicator, но в трейс не рендерится."""
    msg = {
        "role": "user",
        "content": '<technical type="technical">There is no tool call in Agent response.</technical>',
        "id": "policy1",
    }
    ev = map_message(msg, no=10, ts="2026-05-21T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "system"
    assert ev.hidden is True
    assert "There is no tool call" in ev.summary


def test_error_message_maps_to_error_msg():
    """error_message → kind=error_msg, visible, чистый текст без [policy]-префикса."""
    msg = {
        "role": "user",
        "content": '<technical type="error_message">HTTP 500: backend unavailable.</technical>',
        "id": "e1",
    }
    ev = map_message(msg, no=3, ts="t")
    assert ev is not None
    assert ev.kind == "error_msg"
    assert ev.hidden is False
    assert "HTTP 500" in ev.summary
    assert "[policy]" not in ev.summary


def test_regular_user_message_still_kind_user():
    """Обычные user-сообщения без policy-тегов остаются kind=user."""
    msg = {"role": "user", "content": "обычное сообщение", "id": "u1"}
    ev = map_message(msg, no=10, ts="2026-05-21T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "user"
    assert ev.summary == "обычное сообщение"


def test_intermediate_reply_call_maps_to_agent():
    ev = map_message(
        {
            "type": "function_call",
            "name": "intermediate_reply",
            "call_id": "c1",
            "arguments": json.dumps({"answer": "нашёл 3 проблемы, чиню..."}),
        },
        no=1,
        ts="t",
    )
    assert ev is not None
    assert ev.kind == "agent"
    assert ev.tool_name == "intermediate_reply"
    assert ev.summary == "нашёл 3 проблемы, чиню..."


def test_intermediate_reply_empty_answer_filtered():
    ev = map_message(
        {
            "type": "function_call",
            "name": "intermediate_reply",
            "call_id": "c1",
            "arguments": json.dumps({"answer": "   "}),
        },
        no=1,
        ts="t",
    )
    assert ev is None


def test_intermediate_reply_output_filtered():
    ev = map_message(
        {
            "type": "function_call_output",
            "name": "intermediate_reply",
            "call_id": "c1",
            "output": json.dumps({"tool": "intermediate_reply", "ok": True, "answer": "x", "error": None}),
        },
        no=2,
        ts="t",
    )
    assert ev is None


def test_flat_error_code_falls_through_to_the_message():
    """Плоский код ошибки сам по себе ничего не сообщает.

    apply_tool отдаёт internal_tool_error строкой, а текст исключения кладёт
    рядом в "message". Истинная строка коротила `or`, и в строке трейса вместо
    текста была константа — до PR туда доходил текст исключения.
    """
    from gc_tui.services.trace_mapper import _summarize_output

    raw = json.dumps(
        {
            "ok": False,
            "error": "internal_tool_error",
            "exception_type": "TypeError",
            "message": "unexpected keyword argument 'reason'",
            "guidance": "Client runtime failed while executing the tool.",
            "tool": "fs_read_file",
        },
        ensure_ascii=False,
    )

    assert _summarize_output(raw, error=True) == "unexpected keyword argument 'reason'"


def test_nested_error_dict_still_wins_over_message():
    """Регрессия: доменный конверт {type, message} читается как раньше."""
    from gc_tui.services.trace_mapper import _summarize_output

    raw = json.dumps(
        {
            "ok": False,
            "error": {"type": "invalid_argument", "message": "path must be absolute"},
            "message": "не должно использоваться",
        },
        ensure_ascii=False,
    )

    assert _summarize_output(raw, error=True) == "path must be absolute"


def test_function_call_output_invalid_arguments_is_error():
    """Конверт invalid_arguments (вызов с битыми аргументами больше не роняет батч)
    рендерится как error-строка того же вызова."""
    from gc_core.tools.arguments_validation import WrongFunctionArgumentsException

    exc = WrongFunctionArgumentsException(
        "Unexpected field 'bogus'", received_args='{"bogus": 1}', tool_name="session_think"
    )
    ev = map_message(
        {"type": "function_call_output", "name": "session_think", "call_id": "fc_bad", "output": exc.to_tool_output()},
        no=1,
        ts="t",
    )
    assert ev.kind == "error"
    assert ev.call_id == "fc_bad"
    assert ev.summary == "Unexpected field 'bogus'"
