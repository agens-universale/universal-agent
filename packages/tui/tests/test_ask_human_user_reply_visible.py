"""Видимость user-ответа в ask_human-режиме (live + replay) — Фикс A+B."""

from __future__ import annotations

from gc_testkit.tui_support import tui_settings
import json

import pytest
from gc_tui.services.trace_mapper import map_message

# ---------- Unit-тесты Фикса A: mapper для ask_human function_call_output ----------


def _ah_output_payload(
    *, response: str, call_id: str = "call_ah_1", mid: str = "ah_out_1"
) -> dict:
    """Минимальный payload function_call_output для ask_human в формате thread_store."""
    output = json.dumps(
        {
            "tool": "ask_human",
            "ok": True,
            "error": None,
            "question": "Готовы продолжать?",
            "response": response,
        },
        ensure_ascii=False,
    )
    return {
        "id": mid,
        "type": "function_call_output",
        "name": "ask_human",
        "call_id": call_id,
        "output": output,
    }


def test_ask_human_output_non_empty_response_mapped_to_user_band():
    """Непустой response → kind='user' с summary=response."""
    msg = _ah_output_payload(response="да, продолжаем")
    ev = map_message(msg, no=42, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "user"
    assert ev.summary == "да, продолжаем"
    assert ev.tool_name == "ask_human"
    assert ev.call_id == "call_ah_1"
    assert ev.no == 42


def test_ask_human_output_empty_response_mapped_to_system_meta():
    """Пустой/cancel-response (`(empty response)` sentinel) → kind='system'
    с tool_name='ask_human', чтобы banner-cleanup сработал и в replay-е."""
    msg = _ah_output_payload(response="(empty response)")
    ev = map_message(msg, no=7, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "system"
    assert ev.title == "ask_human"
    assert "(без ответа)" in ev.summary
    assert ev.tool_name == "ask_human"
    assert ev.call_id == "call_ah_1"


def test_ask_human_output_blank_response_mapped_to_system_meta():
    """Полностью пустой response (на случай если тул когда-то вернёт ""). Тот же путь."""
    msg = _ah_output_payload(response="")
    ev = map_message(msg, no=3, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "system"
    assert ev.tool_name == "ask_human"


def test_ask_human_output_broken_json_mapped_to_system_meta():
    """output невалидный JSON — не падаем, эмитим system-метку, banner сможет уйти."""
    msg = {
        "id": "ah_broken",
        "type": "function_call_output",
        "name": "ask_human",
        "call_id": "call_ah_bad",
        "output": "this-is-not-json{{",
    }
    ev = map_message(msg, no=1, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "system"
    assert ev.tool_name == "ask_human"


def test_ask_human_output_user_interrupted_mapped_to_system_meta():
    """KeyboardInterrupt в тул-е: response='(user interrupted)', ok=False.
    Структурный сигнал ok=False → kind='system', не user-band."""
    output = json.dumps(
        {
            "tool": "ask_human",
            "ok": False,
            "error": {
                "type": "interrupted",
                "message": "Interaction interrupted by user.",
            },
            "question": "Готовы продолжать?",
            "response": "(user interrupted)",
        },
        ensure_ascii=False,
    )
    msg = {
        "id": "ah_interrupted",
        "type": "function_call_output",
        "name": "ask_human",
        "call_id": "call_ah_int",
        "output": output,
    }
    ev = map_message(msg, no=5, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "system"
    assert ev.tool_name == "ask_human"
    # Текст '(user interrupted)' НЕ должен попасть в трейс как user-реплика.
    assert "(user interrupted)" not in ev.summary


def test_ask_human_output_read_error_mapped_to_system_meta():
    """Exception в тул-е: response='(error reading input: ...)', ok=False.
    Тот же структурный путь через ok=False."""
    output = json.dumps(
        {
            "tool": "ask_human",
            "ok": False,
            "error": {"type": "read_failed", "message": "I/O error"},
            "question": "Готовы?",
            "response": "(error reading input: I/O error)",
        },
        ensure_ascii=False,
    )
    msg = {
        "id": "ah_read_err",
        "type": "function_call_output",
        "name": "ask_human",
        "call_id": "call_ah_err",
        "output": output,
    }
    ev = map_message(msg, no=6, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "system"
    assert ev.tool_name == "ask_human"
    assert "error reading input" not in ev.summary


# Регрессия: chat_completions-формат function_call_output для ask_human
# не имеет `name` на верхнем уровне — идентификатор тула живёт в JSON `output`.
# Без этого test-кейса мапер ловил только responses-API формат, а в проде
# (где chat_completions) banner-cleanup не срабатывал ни разу.


def _ah_output_payload_chat_completions(
    *,
    response: str,
    ok: bool = True,
    call_id: str = "call_cc_1",
    mid: str = "ah_cc_1",
) -> dict:
    """Реальный chat_completions формат function_call_output для ask_human:
    верхний `name=None` (отсутствует), идентификатор тула — в JSON-output-е."""
    output = json.dumps(
        {
            "tool": "ask_human",
            "ok": ok,
            "error": None if ok else {"type": "interrupted", "message": "..."},
            "question": "Готовы?",
            "response": response,
        },
        ensure_ascii=False,
    )
    return {
        "id": mid,
        "type": "function_call_output",
        # NO "name" key — exactly как пишет append_*_for_batch в chat_completions.
        "call_id": call_id,
        "output": output,
    }


def test_ask_human_output_chat_completions_format_non_empty_response():
    """chat_completions-payload (name отсутствует, tool=ask_human в JSON) с
    непустым response → kind='user'. Без этого банер не снимается на проде."""
    msg = _ah_output_payload_chat_completions(response="A, B, D")
    ev = map_message(msg, no=10, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "user"
    assert ev.summary == "A, B, D"
    assert ev.tool_name == "ask_human"
    assert ev.call_id == "call_cc_1"


def test_ask_human_output_chat_completions_format_empty_response():
    """chat_completions-payload + пустой response → kind='system' с меткой."""
    msg = _ah_output_payload_chat_completions(response="(empty response)")
    ev = map_message(msg, no=11, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "system"
    assert ev.tool_name == "ask_human"
    assert "(без ответа)" in ev.summary


def test_ask_human_output_chat_completions_format_ok_false():
    """chat_completions-payload + ok=False (cancel/interrupt) → kind='system'."""
    msg = _ah_output_payload_chat_completions(
        response="(user interrupted)", ok=False
    )
    ev = map_message(msg, no=12, ts="2026-05-28T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "system"
    assert ev.tool_name == "ask_human"


# ---------- Integration-тесты Фикса B: видимость live-ответа + replay ----------

from pathlib import Path

from gc_tui.app import TuiApp
from gc_tui.models import TraceEvent
from gc_tui.services.stores.file_store import FileTaskStore


class _FakeRunner:
    """Минимальный stub `AgentRunner` для тестов submit-pipeline-а в ask_human.

    Хранит только то, что юзер отправил через ask_human."""

    def __init__(self):
        self.submitted: list[str] = []

    def is_running(self) -> bool:
        return True

    def submit_ask_human_reply(self, text: str) -> None:
        self.submitted.append(text)


def _write_thread(
    tmp_path: Path, *, thread_id: str, records: list[dict]
) -> Path:
    """Записать persisted-тред в формате thread_store (jsonl)."""
    threads = tmp_path / "threads"
    threads.mkdir(exist_ok=True)
    data_path = threads / f"data_cli_{thread_id}.jsonl"
    meta_path = threads / f"meta_cli_{thread_id}.json"
    with open(data_path, "w", encoding="utf-8") as f:
        for i, payload in enumerate(records, start=1):
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "thread_id": thread_id,
                        "system": "cli",
                        "seq": i,
                        "ts": f"2026-05-28T10:0{i}:00+00:00",
                        "payload": payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    meta_path.write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-05-28T10:10:00+00:00",
                "messages_count": len(records),
                "tui": {},
            }
        ),
        encoding="utf-8",
    )
    return threads


@pytest.mark.asyncio
async def test_replay_renders_ask_human_dialog(tmp_path):
    """При attach старого треда с парой ask_human-call + ask_human-output
    обе записи видны: вопрос как banner снимается через output → meta-метку,
    ответ показывается как user-реплика."""
    thread_id = "ahreplay00000000000000000000000a"
    records = [
        {"role": "user", "content": "привет"},
        {
            "id": "ah_call_1",
            "type": "function_call",
            "name": "ask_human",
            "call_id": "call_ah_1",
            "arguments": json.dumps({"question": "Готовы продолжать?"}),
        },
        _ah_output_payload(response="да, продолжаем", call_id="call_ah_1"),
    ]
    threads_dir = _write_thread(tmp_path, thread_id=thread_id, records=records)

    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        # Banner снят (output-meta его убрал в replay).
        assert screen._ask_human_banner is None
        # В трассе есть user-реплика с текстом ответа.
        from gc_tui.widgets.user_reply import UserReplyContainer

        replies = list(screen.query(UserReplyContainer))
        texts = [r._text for r in replies]
        assert "да, продолжаем" in texts


@pytest.mark.asyncio
async def test_replay_empty_response_notes_question_and_unmounts_banner(
    tmp_path,
):
    """Replay output-а с пустым ответом → banner снят, под вопросом в его
    рамке — строка `└ без ответа` (спека P3 §3). User-реплика НЕ появляется."""
    thread_id = "ahreplay00000000000000000000000b"
    records = [
        {
            "id": "ah_call_2",
            "type": "function_call",
            "name": "ask_human",
            "call_id": "call_ah_2",
            "arguments": json.dumps({"question": "Подтвердите?"}),
        },
        _ah_output_payload(response="(empty response)", call_id="call_ah_2"),
    ]
    threads_dir = _write_thread(tmp_path, thread_id=thread_id, records=records)

    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        assert screen._ask_human_banner is None
        from gc_tui.widgets.user_reply import UserReplyContainer

        assert not list(screen.query(UserReplyContainer))
        # Следствие — внутри рамки вопроса, отдельной meta-строки нет.
        from gc_tui.widgets.trace_row import QuestionRow, TraceRow

        (question,) = screen.query(QuestionRow)
        assert str(question._content) == "? Подтвердите?"
        assert [b.plain for b in question._consequences] == ["без ответа"]
        trace_text = " ".join(
            f"{w._prefix}{w._content}" for w in screen.query(TraceRow)
        )
        assert "спрашивал" not in trace_text


@pytest.mark.asyncio
async def test_live_submit_mounts_pending_user_reply(tmp_path):
    """В live-ask_human submit Enter-ом монтирует pending user-реплику,
    сразу снимает баннер и показывает thinking-индикатор."""
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({}),
    )
    async with app.run_test() as pilot:
        screen = app.screen
        # Подготовка: создаём задачу + банер + fake runner.
        thread_id = "ahlive00000000000000000000000001"
        (tmp_path / "threads").mkdir(exist_ok=True)
        screen._active_task_id = thread_id
        app.tui_state.active_task_id = thread_id
        screen._store = FileTaskStore(
            threads_dir=str(tmp_path / "threads"), system="cli"
        )
        runner = _FakeRunner()
        app.active_runner = runner
        # Mount banner через прямой вызов (как сделал бы _render_event для kind=ask_human).
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-05-28T10:00:00+00:00",
            kind="ask_human",
            title="ask_human",
            summary="Готовы?",
            full_payload={},
            message_id="ah_1",
            call_id="call_live_1",
            tool_name="ask_human",
        )
        # live=True: плашка из live-поллинга → владелец = active_runner (как в проде).
        screen._on_ask_human_event(ev, live=True)
        await pilot.pause()
        assert screen._ask_human_banner is not None
        # Симулируем submit Enter-ом: вписываем текст и дёргаем on_command_text_area_submitted.
        from gc_tui.widgets.command_input import CommandTextArea

        inp = screen.query_one("#cmdinput", CommandTextArea)
        inp.text = "да, поехали"
        # Имитируем submit event Textual'а напрямую — в Pilot Enter в TextArea
        # обработывается, но дёргаем хендлер явно ради изоляции.
        submitted = CommandTextArea.Submitted(value="да, поехали")
        await screen.on_command_text_area_submitted(submitted)
        await pilot.pause()
        # Runner получил текст.
        assert runner.submitted == ["да, поехали"]
        # Баннер снят сразу (не ждём function_call_output).
        assert screen._ask_human_banner is None
        assert app.tui_state.ask_human_state is None
        # В трассе есть pending user-реплика.
        from gc_tui.widgets.user_reply import UserReplyContainer

        replies = list(screen.query(UserReplyContainer))
        assert replies, "user-reply виджет должен быть смонтирован"
        assert replies[-1]._state == "pending"
        # Филлер «думаю…» уже виден — агент снова работает, ответа юзера не ждём.
        from gc_tui.widgets.thinking_indicator import ThinkingIndicator

        indicators = list(screen.query(ThinkingIndicator))
        assert indicators, (
            "thinking-индикатор должен появиться сразу после ответа"
        )
        assert not indicators[0].has_class("hidden")
        assert indicators[0]._state == "thinking"


@pytest.mark.asyncio
async def test_live_function_call_output_confirms_pending(tmp_path):
    """После submit-а в live-ask_human приходит function_call_output (с тем же
    response-текстом) → pending переводится в normal через _pending_wire_match,
    дубля user-реплики нет, banner снят."""
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({}),
    )
    async with app.run_test() as pilot:
        screen = app.screen
        thread_id = "ahlive00000000000000000000000002"
        (tmp_path / "threads").mkdir(exist_ok=True)
        screen._active_task_id = thread_id
        app.tui_state.active_task_id = thread_id
        screen._store = FileTaskStore(
            threads_dir=str(tmp_path / "threads"), system="cli"
        )
        runner = _FakeRunner()
        app.active_runner = runner
        # Banner + submit.
        ev_call = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-05-28T10:00:00+00:00",
            kind="ask_human",
            title="ask_human",
            summary="Готовы?",
            full_payload={},
            message_id="ah_call",
            call_id="call_live_2",
            tool_name="ask_human",
        )
        # live=True: плашка из live-поллинга → владелец = active_runner (как в проде).
        screen._on_ask_human_event(ev_call, live=True)
        await pilot.pause()
        from gc_tui.widgets.command_input import CommandTextArea

        inp = screen.query_one("#cmdinput", CommandTextArea)
        inp.text = "go"
        await screen.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="go")
        )
        await pilot.pause()
        # Симулируем приход output-а через тот же путь, что и polling.
        ev_out = map_message(
            _ah_output_payload(response="go", call_id="call_live_2"),
            no=2,
            ts="2026-05-28T10:00:01+00:00",
        )
        assert ev_out is not None
        screen._render_event(ev_out)
        await pilot.pause()
        # Banner снят.
        assert screen._ask_human_banner is None
        # User-реплика одна и в state=normal.
        from gc_tui.widgets.user_reply import UserReplyContainer

        replies = list(screen.query(UserReplyContainer))
        assert len(replies) == 1, (
            f"должна быть ровно 1 user-реплика, получили {len(replies)}"
        )
        assert replies[0]._state == "normal"
