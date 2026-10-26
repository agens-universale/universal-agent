"""Режим шага для батча с техническим хвостом (спека изображений §7.2).

Батч «ответы тулов + технический хвост» — продолжение ответов тулов, а не новый
запрос человека: ``function_call_output``. Реплика человека в батче или вызов в нём
оставляют прежний ``user_ask``. Сообщения собраны теми же фабриками, что и в цикле.
"""

from __future__ import annotations

import json
import logging

from gc_core import processing_responses as pr
from gc_core.loop_detector.constants import LOOP_WARN_KEY, READ_PROFILE
from gc_core.message_policy.no_tool_call_bounce import (
    format_no_tool_call_bounce,
)
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_ERROR_MESSAGE,
    wrap_skill_resolution_technical_instruction,
    wrap_technical_user_content,
)
from gc_core.states import State
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx
from helpers.turn_finish_apply import stub_turn_finish_apply_sync

_LOG = logging.getLogger("test_check_mode_technical_tail")

_LOOP_NOTICE = (
    "Loop detector: you called fs_read_file with the same arguments again "
    '({"path": "a.py"}).\n'
    "The tool still ran; this probably does not produce new information. Use the previous\n"
    "result, change arguments, or try a different tactic."
)


def _fc(call_id: str, name: str = "fs_read_file") -> dict:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": '{"path": "a.py"}',
    }


def _fco(call_id: str) -> dict:
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "output": '{"ok": true, "content": "print(1)"}',
    }


def _tech(text: str, **kwargs) -> dict:
    return pr._make_technical_feedback_user_message(text, **kwargs)


def _human(text: str, msg_id: str = "m_h") -> dict:
    return {"role": "user", "content": text, "id": msg_id}


# --- новый режим: ответы тулов + технический хвост -----------------------------


def test_loop_notice_tail_is_function_call_output():
    batch = [_fco("c1"), _fco("c2"), _tech(_LOOP_NOTICE)]
    assert pr.check_mode(batch, _LOG) == "function_call_output"


def test_rejected_finish_with_loop_notice_is_function_call_output():
    """finish-гейт отклонил сдачу, хвост (напоминание) остался в батче."""
    finish_out = {
        "id": "fco_finish",
        "type": "function_call_output",
        "call_id": "call_finish",
        "output": '{"ok": false, "message": "keep going"}',
    }
    batch = [_fco("c1"), finish_out, _tech(_LOOP_NOTICE)]
    assert pr.check_mode(batch, _LOG) == "function_call_output"


def test_mid_run_skill_without_prompt_after_outputs_is_function_call_output():
    """/skill без промпта посреди запуска (TUI): доставка с display_content="" —
    не реплика человека (граница хода — только непустой extract_real_user_message_text)."""
    delivery = {
        "role": "user",
        "content": "Use skill `review`.\n\nRead the diff first, then comment.",
        "display_content": "",
        "skill_loaded": "review",
    }
    assert (
        pr.check_mode([_fco("c1"), delivery], _LOG) == "function_call_output"
    )


def test_several_technical_messages_after_outputs_are_function_call_output():
    batch = [_fco("c1"), _tech(_LOOP_NOTICE), _tech("second technical line")]
    assert pr.check_mode(batch, _LOG) == "function_call_output"


# --- остаются user_ask ------------------------------------------------------------


def test_session_compact_tail_with_pairs_stays_user_ask():
    """Хвост session_compact несёт пары call/output. Не user_ask — значит режим
    function_call (в батче есть вызов), и aprocess_tool_calls падает на
    'Messages has tool_outputs'."""
    batch = [
        _fc("c1"),
        _fco("c1"),
        _tech(pr._SESSION_COMPACT_RESUME_USER_TEXT),
    ]
    assert pr.check_mode(batch, _LOG) == "user_ask"


def test_session_compact_tail_with_pairs_and_notice_stays_user_ask():
    batch = [
        _fc("c1"),
        _fco("c1"),
        _tech(pr._SESSION_COMPACT_RESUME_USER_TEXT),
        _tech(_LOOP_NOTICE),
    ]
    assert pr.check_mode(batch, _LOG) == "user_ask"


def test_session_compact_resume_alone_stays_user_ask():
    assert (
        pr.check_mode([_tech(pr._SESSION_COMPACT_RESUME_USER_TEXT)], _LOG)
        == "user_ask"
    )


def test_no_tool_call_bounce_stays_user_ask():
    batch = [_tech(format_no_tool_call_bounce(finish_reason=None))]
    assert pr.check_mode(batch, _LOG) == "user_ask"


def test_invalid_arguments_feedback_stays_user_ask():
    batch = [
        _tech(
            "Tool argument validation failed for fs_read_file: 'path' is required.",
            technical_type=TECHNICAL_TYPE_ERROR_MESSAGE,
            call_id="c1",
        )
    ]
    assert pr.check_mode(batch, _LOG) == "user_ask"


def test_tui_at_mention_start_batch_stays_user_ask():
    footer = wrap_technical_user_content(
        "Пользователь прикрепил через «@» (упоминания @<путь> в предыдущем сообщении "
        "ссылаются на эти файлы/папки относительно корня проекта):\n"
        "- @a.py → /work/a.py (file)"
    )
    batch = [
        _human("посмотри @a.py", "m1"),
        {"role": "user", "content": footer},
    ]
    assert pr.check_mode(batch, _LOG) == "user_ask"


def test_cli_skill_start_batch_stays_user_ask():
    batch = [
        _human("сделай ревью", "m1"),
        {
            "role": "user",
            "content": wrap_skill_resolution_technical_instruction("review"),
            "id": "m2",
        },
    ]
    assert pr.check_mode(batch, _LOG) == "user_ask"


def test_cli_wrap_technical_start_batch_stays_user_ask():
    batch = [
        {
            "role": "user",
            "content": wrap_technical_user_content("сделай ревью"),
            "id": "m1",
        }
    ]
    assert pr.check_mode(batch, _LOG) == "user_ask"


def test_mid_run_human_with_footnote_after_outputs_stays_user_ask():
    """Реплика посреди запуска (пара «текст + @-сноска» из очереди TUI) после
    ответов тулов — новый запрос человека: компакция не должна свернуть реплику."""
    footer = wrap_technical_user_content("- @a.py → /work/a.py (file)")
    batch = [
        _fco("c1"),
        _human("а теперь b.py", "m_mid"),
        {"role": "user", "content": footer},
    ]
    assert pr.check_mode(batch, _LOG) == "user_ask"


def test_mid_run_human_after_outputs_stays_user_ask():
    assert (
        pr.check_mode([_fco("c1"), _human("стоп, другое")], _LOG) == "user_ask"
    )


def test_skill_delivery_display_content_counts_as_human():
    """display_content — чистый человеко-текст реплики со скиллом: это реплика."""
    skill_msg = {
        "role": "user",
        "content": "<skill>...</skill>\nпочини тест",
        "display_content": "почини тест",
        "id": "m_s",
    }
    batch = [_fco("c1"), skill_msg, _tech(_LOOP_NOTICE)]
    assert pr.check_mode(batch, _LOG) == "user_ask"


# --- прежние режимы не меняются -----------------------------------------------------


def test_plain_outputs_stay_function_call_output():
    assert (
        pr.check_mode([_fco("c1"), _fco("c2")], _LOG) == "function_call_output"
    )


def test_model_calls_stay_function_call():
    assert pr.check_mode([_fc("c1")], _LOG) == "function_call"


def test_assistant_text_stays_text_response():
    batch = [{"role": "assistant", "type": "message", "content": "готово"}]
    assert pr.check_mode(batch, _LOG) == "text_response"


# --- интеграция: хвост напоминания уходит в function_call_output ------------------


def test_run_agent_sends_loop_notice_tail_as_function_call_output(monkeypatch):
    """Два одинаковых чтения → напоминание loop detector после ответов → следующий
    запрос к модели идёт в режиме function_call_output (хук компакции видит режим)."""

    async def _apply(**kw):
        if kw["tool_name"] == "turn_finish":
            return stub_turn_finish_apply_sync(**kw)
        return json.dumps({"ok": True, "content": "print(1)"})

    monkeypatch.setattr(pr, "aapply_tool", _apply)
    monkeypatch.setattr(
        pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None)
    )

    finish = {
        "id": "fc_finish",
        "type": "function_call",
        "call_id": "call_finish",
        "name": "turn_finish",
        "arguments": json.dumps(
            {
                "solution_type": "full_solution",
                "task_report": "done",
                "handoff": "{}",
            }
        ),
    }
    seen_batches: list = []

    async def _outputs(messages, _state, _ctx):
        seen_batches.append(list(messages))
        return [finish], "resp_2"

    monkeypatch.setattr(pr, "aprocess_tool_outputs", _outputs)
    # Обе точки запроса — одна заглушка: режим различает только хук компакции.
    monkeypatch.setattr(pr, "aprocess_user_ask", _outputs)
    modes: list = []

    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "logging.console_trace": False,
            "agent.no_tool_call_max_count": 5,
            "agent.strict_turn_finish_validation": False,
        },
        tools_data={"fs_read_file": {LOOP_WARN_KEY: READ_PROFILE}},
        logger=_LOG,
        auth_connector=None,
    )

    result = run_coro(
        pr.arun_agent(
            [_fc("c1"), _fc("c2")],
            state,
            ctx,
            pre_backend_compaction_hook=lambda _msgs, mode: modes.append(mode),
        )
    )

    assert result == finish["arguments"]
    assert modes == ["function_call_output"]
    assert [m.get("type") or m.get("role") for m in seen_batches[0]] == [
        "function_call_output",
        "function_call_output",
        "user",
    ]
    assert "Loop detector:" in seen_batches[0][-1]["content"]
