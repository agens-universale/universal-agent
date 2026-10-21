"""Вид ленты P3a одним кадром: колонки, реплика, следствия, сжатие.

Тред пишется настоящими функциями `gc_core.thread_store`: до сжатия — первая
реплика с ответом, затем сжатие и после него — реплика со скиллом,
промежуточная реплика, `Read`, `Edit` с диффом из журнала правок, упавший
`Run`, субагент с вложенными репликой и вызовом, строка `!` без владельца и
итоговый ответ.
"""

from __future__ import annotations

import json

import pytest
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.thread_store import (
    append_new_thread_messages,
    append_subagent_trace_record,
    archive_and_replace_thread_files,
    edits_journal_path,
    load_thread_meta,
    save_thread_meta,
)

_SYS = "cli"
_TID = "7f3a9b1c"
_SUFFIX = "20261021_120000"
_SUB_CALL = "call_sub_1"
_HUNKS = (
    "--- \n+++ \n@@ -11,3 +11,3 @@\n RETRIES = 3\n"
    "-TIMEOUT_SEC = 10\n+TIMEOUT_SEC = 30\n ENDPOINT = '…'\n"
)


def _user(mid: str, text: str, **extra) -> dict:
    return {"id": mid, "role": "user", "content": text, **extra}


def _agent(mid: str, text: str) -> dict:
    return {"id": mid, "role": "assistant", "content": text}


def _call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _output(call_id: str, output: dict) -> dict:
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(output, ensure_ascii=False),
    }


def _summary() -> dict:
    return _user(
        "sum1",
        wrap_technical_user_content(
            "Summarized context from previous dialogue.\n"
            "Цель: поднять таймаут клиента.",
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
    )


def _error_msg(mid: str, text: str) -> dict:
    return _user(mid, f'<technical type="error_message">{text}</technical>')


def _append(tmp_path, msgs: list[dict], start: int) -> int:
    append_new_thread_messages(
        str(tmp_path), _SYS, _TID, msgs, set(), start_seq=start
    )
    return start + len(msgs)


def _after_compaction() -> list[dict]:
    return [
        _user(
            "u2",
            "<skill>\nпридумай варианты ретраев",
            display_content="придумай варианты ретраев",
            skill_loaded="brainstorming",
        ),
        _call(
            "c1",
            "turn_intermediate_reply",
            {"answer": "Поправлю конфиг и проверю модуль субагентом."},
        ),
        _output(
            "c1",
            {
                "tool": "turn_intermediate_reply",
                "ok": True,
                "answer": "Поправлю конфиг и проверю модуль субагентом.",
                "error": None,
            },
        ),
        _call("c2", "fs_read_file", {"path": "config.py"}),
        _output("c2", {"tool": "fs_read_file", "ok": True, "lines": 40}),
        _call("c3", "fs_search_and_replace", {"path": "config.py"}),
        _output("c3", {"tool": "fs_search_and_replace", "ok": True}),
        _call("c4", "exec_command", {"command": "make docs"}),
        _output(
            "c4",
            {
                "tool": "exec_command",
                "ok": False,
                "error": "make: *** No rule to make target 'docs'.",
            },
        ),
        _call(
            _SUB_CALL,
            "subagent_worker",
            {"task": "проверка модуля", "reason": "тесты клиента"},
        ),
        _output(_SUB_CALL, {"ok": True, "result": "готово"}),
        _error_msg("e1", "сеть: соединение прервано — повтор через 5 с"),
        _agent("a2", "Готово: таймаут 30 с, клиент читает его из конфига."),
    ]


def _write_thread(tmp_path) -> None:
    save_thread_meta(str(tmp_path), _SYS, _TID, {})
    _append(
        tmp_path,
        [
            _user("u1", "подними таймаут клиента"),
            _agent(
                "a1",
                "Таймаут клиента задан в config.py и равен 10 с; клиент "
                "читает его при создании сессии, ретраев сейчас нет.",
            ),
        ],
        0,
    )
    meta = load_thread_meta(str(tmp_path), _SYS, _TID)
    archive_and_replace_thread_files(
        str(tmp_path), _SYS, _TID, [_summary()], archive_suffix=_SUFFIX
    )
    save_thread_meta(
        str(tmp_path),
        _SYS,
        _TID,
        {**meta, "last_summary_archive_suffix": _SUFFIX},
    )
    _append(tmp_path, _after_compaction(), 1)


def _write_edit_journal(tmp_path) -> None:
    journal = EditsJournal(edits_journal_path(str(tmp_path), _SYS, _TID))
    journal.append(
        EditRecord(
            path="config.py",
            turn_id="t1",
            ts=1.0,
            call_id="c3",
            op="edit",
            hunks=_HUNKS,
            add=1,
            del_=1,
            before_sha256=None,
            after_sha256=None,
            flags=[],
        )
    )


def _sidecar(tmp_path, kind: str, payload: dict) -> None:
    append_subagent_trace_record(
        str(tmp_path),
        _SYS,
        _TID,
        parent_call_id=_SUB_CALL,
        child_run_id="run_c1",
        kind=kind,
        payload=payload,
    )


def _write_subagent(tmp_path) -> None:
    _sidecar(
        tmp_path,
        "intermediate_reply",
        {"prose": "Смотрю, где клиент берёт таймаут.", "call_id": "ir1"},
    )
    _sidecar(
        tmp_path,
        "call",
        {
            "type": "function_call",
            "name": "fs_read_file",
            "arguments": json.dumps({"path": "client.py"}),
            "call_id": "call_child_1",
        },
    )
    _sidecar(
        tmp_path,
        "result",
        {
            "type": "function_call_output",
            "call_id": "call_child_1",
            "output": "ok",
        },
    )
    _sidecar(
        tmp_path,
        "finished",
        {"ok": True, "finish_reason": "completed", "steps": 2},
    )


def test_trace_look_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.services import terminal_caps

    # Папку в шапке и глиф ❐/⧉ (зависит от терминала) фиксируем — иначе
    # снапшот стал бы машинно-зависимым.
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
    monkeypatch.setattr(terminal_caps, "supports_font_fallback", lambda: False)
    _write_thread(tmp_path)
    _write_edit_journal(tmp_path)
    _write_subagent(tmp_path)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system=_SYS,
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
    )

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.attach_active_task(_TID, is_resume=True)
        # Мигание курсора поля — таймер 0.5 с, фаза в кадре от нагрузки;
        # без мигания курсор виден всегда (приём `test_snapshot_forms`).
        pilot.app.screen.query_one("#cmdinput").cursor_blink = False
        await pilot.pause(0.5)

    assert snap_compare(app, terminal_size=(120, 40), run_before=setup)
