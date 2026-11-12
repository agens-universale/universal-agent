"""run_agent при переполнении контекста: force-компакция + повтор, не краш."""

import asyncio

import pytest

from gc_core import processing_responses as pr
from gc_core import run_summary_coordinator as rsc
from gc_core.execution.contracts import ExecutionFailure
from gc_core.message_policy import nudges as nudge_mod
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.run_summary_coordinator import RunSummaryCoordinator
from gc_core.summarize import compaction
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _state():
    s = type("S", (), {})()
    s.mode_announced = None
    s.mark_turn_start = lambda: None
    s.messages = []
    s.steps = 0
    s.max_steps = 100
    s.current_user_intent = []
    s.user_intent_log = []
    s.user_turn_seq = 0
    s.flush_current_user_intent = lambda: None
    return s


def test_rejected_compaction_preserves_original_context_overflow(monkeypatch):
    overflow = ValueError(
        "Requested token count exceeds the model's maximum context length of 131072 tokens"
    )
    attempts = []

    async def fail_request(*_args):
        attempts.append("request")
        raise overflow

    async def empty_summary(**_kwargs):
        return {"raw_summary": ""}

    monkeypatch.setattr(pr, "aprocess_user_ask", fail_request)
    monkeypatch.setattr(
        compaction, "asummarize_dialog_via_chat_completions", empty_summary
    )
    state, ctx = _state(), _run_ctx({"logging.save_logs": False})

    async def compact(_messages, _mode):
        await compaction.arun_summary_compaction(
            ctx=ctx, state=state, trigger_reason="overflow", is_cli_mode=False
        )

    with pytest.raises(ValueError) as caught:
        run_coro(
            pr.arun_agent(
                [{"role": "user", "content": "Keep this", "id": "u1"}],
                state,
                ctx,
                force_compact_hook=compact,
            )
        )
    assert caught.value is overflow
    assert attempts == ["request"]
    assert any(message["content"] == "Keep this" for message in state.messages)


def _run_ctx(overrides=None):
    cd = {
        "backend.api_mode": "chat_completions",
        "logging.console_trace": False,
        "agent.no_tool_call_max_count": 3,
        "agent.strict_turn_finish_validation": True,
        "text_answer_mode": "allow",
    }
    if overrides:
        cd.update(overrides)
    return fake_ctx(cd, logger=None)


def test_context_overflow_triggers_compaction_and_retries(monkeypatch):
    calls = {"backend": 0, "compact": 0}

    async def _fake_process_user_ask(messages, state, ctx):
        calls["backend"] += 1
        if calls["backend"] == 1:
            raise ValueError(
                "Requested token count exceeds the model's maximum context "
                "length of 131072 tokens. Please reduce the number of tokens"
            )
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp_2"

    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "сделай", "id": "u1"}],
            _state(),
            _run_ctx(),
            force_compact_hook=lambda *_args, **_kwargs: calls.__setitem__(
                "compact", calls["compact"] + 1
            ),
        )
    )

    assert result == "готово"  # прогон восстановился, не краш
    assert calls["backend"] == 2  # 1-й overflow, 2-й успех
    assert calls["compact"] == 1  # force-компакция вызвана один раз


def test_overflow_retry_collects_notices_once(monkeypatch):
    """Повтор после сжатия — тот же шаг: подсказка уже в сообщениях."""
    calls = {"backend": 0, "notices": 0, "seen": []}

    def _notice(*_args, **_kwargs):
        calls["notices"] += 1
        return ["NOTICE"]

    async def _fake_process_user_ask(messages, state, ctx):
        calls["backend"] += 1
        calls["seen"].append(list(messages))
        if calls["backend"] == 1:
            raise ValueError(
                "Requested token count exceeds the model's maximum context "
                "length of 131072 tokens. Please reduce the number of tokens"
            )
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp_2"

    monkeypatch.setattr(
        "gc_core.message_policy.nudges.notice_texts_before_llm", _notice
    )
    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "сделай", "id": "u1"}],
            _state(),
            _run_ctx(),
            force_compact_hook=lambda *_args, **_kwargs: None,
            session_meta={},
        )
    )

    assert result == "готово"
    assert calls["notices"] == 1
    assert (
        sum(
            1
            for message in calls["seen"][1]
            if "NOTICE" in str(message.get("content"))
        )
        == 1
    )


def test_next_step_collects_notices_again(monkeypatch):
    """Следующий запрос к модели после успешного шага снова собирает подсказки."""
    calls = {"backend": 0, "notices": 0}

    def _notice(*_args, **_kwargs):
        calls["notices"] += 1
        return []

    async def _fake_process_user_ask(messages, state, ctx):
        calls["backend"] += 1
        return [
            {"role": "assistant", "type": "message", "content": "не тул"}
        ], "resp"

    monkeypatch.setattr(
        "gc_core.message_policy.nudges.notice_texts_before_llm", _notice
    )
    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)
    state = _state()
    state.tick_signals = set()

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "сделай", "id": "u1"}],
            state,
            _run_ctx(
                {
                    "text_answer_mode": "deny",
                    "agent.no_tool_call_max_count": 1,
                }
            ),
            session_meta={},
        )
    )

    assert result == "Agent tools call errors."
    assert calls["backend"] == 2
    assert calls["notices"] == 2


def test_summary_hook_drops_nudges_for_this_request(monkeypatch):
    """Хук сводки вернул истину — цикл выбрасывает подсказки этого запроса."""
    calls = {"notices": 0, "seen": []}

    def _notice(*_args, **_kwargs):
        calls["notices"] += 1
        return ["NOTICE"]

    async def _summary_applied(_messages, _mode):
        return True

    async def _fake_process_user_ask(messages, state, ctx):
        calls["seen"].append(list(messages))
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp"

    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 4}
    state.todo_reminder_cnt = 3
    state.mode_announced = "default"
    monkeypatch.setattr(
        "gc_core.message_policy.nudges.notice_texts_before_llm", _notice
    )
    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)
    notice = {"project_memory_check_cnt": 4}

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "сделай", "id": "u1"}],
            state,
            _run_ctx(),
            pre_backend_compaction_hook=_summary_applied,
            session_meta=notice,
        )
    )

    assert result == "готово"
    assert calls["notices"] == 0
    assert not any(
        "NOTICE" in str(message.get("content")) for message in calls["seen"][0]
    )
    assert state.tool_usage_notice_cnt == {}
    assert state.todo_reminder_cnt == 0
    assert notice["project_memory_check_cnt"] == 0
    assert state.mode_announced == "default"


def test_overflow_retry_does_not_glue_old_batch_onto_summary(monkeypatch):
    """После аварийной сводки повтор не дописывает исходный батч поверх неё."""
    seen = []
    state = _state()

    async def _fake_process_user_ask(messages, st, _ctx):
        seen.append(
            {
                "batch": [m.get("id") for m in messages],
                "state": [m.get("id") for m in st.messages],
            }
        )
        if len(seen) == 1:
            raise ValueError(
                "Requested token count exceeds the model's maximum context "
                "length of 131072 tokens. Please reduce the number of tokens"
            )
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp_2"

    def _compact(*_args, **_kwargs):
        state.messages = [{"role": "user", "content": "SUMMARY", "id": "sum"}]
        return True

    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "сделай", "id": "u1"}],
            state,
            _run_ctx(),
            force_compact_hook=_compact,
        )
    )

    assert result == "готово"
    assert seen[1]["batch"] == ["sum"]
    assert seen[1]["state"] == ["sum"]


def test_tool_output_step_compacts_while_output_is_held(monkeypatch):
    """Ответ инструмента уже в истории — сводка перед моделью его забирает."""
    output = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "x" * 400,
        "id": "fco",
    }
    compacted = {"yes": False}

    async def _fake_tools(
        _messages, _state, _ctx, batch_trace=None, **_kwargs
    ):
        if isinstance(batch_trace, dict):
            batch_trace.setdefault("executed", []).append("c1")
        return [output]

    async def _fake_outputs(messages, _state, _ctx):
        compacted["sent"] = [m.get("id") for m in messages]
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp"

    async def _compact(**kwargs):
        compacted["yes"] = True
        compacted["source"] = [m.get("id") for m in kwargs["state"].messages]
        kept = [m.get("id") for m in kwargs["kept_messages"]]
        compacted["kept"] = kept
        kwargs["state"].messages = [
            {"role": "user", "content": "SUMMARY", "id": "sum"}
        ]

    monkeypatch.setattr(pr, "aprocess_tool_calls", _fake_tools)
    monkeypatch.setattr(pr, "aprocess_tool_outputs", _fake_outputs)
    monkeypatch.setattr(rsc, "arun_summary_compaction", _compact)

    state = _state()
    state.backend_api_mode = "chat_completions"
    ctx = _run_ctx()
    coord = RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=50,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )

    result = run_coro(
        pr.arun_agent(
            [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "fs_read",
                    "arguments": "{}",
                    "id": "fc",
                }
            ],
            state,
            ctx,
            pre_backend_compaction_hook=coord.apre_backend_compaction_hook,
        )
    )

    assert result == "готово"
    assert compacted["yes"] is True
    assert "fco" in compacted["source"]
    assert "fco" not in compacted["kept"]
    assert "fc" not in compacted["kept"]
    assert "fco" not in compacted["sent"]


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_summary_applied_then_failed_does_not_republish_held_output(
    monkeypatch, failure
):
    """Сводка уже подменила историю — старый ответ в новый трейс не возвращается.

    Обычное исключение ход продолжает по сводке. Отмена ход останавливает.
    В обоих случаях сырого ``function_call_output`` нет ни в state, ни на диске.
    """
    output = _held_output()
    persisted: list[dict] = []
    state = _state()
    state.backend_api_mode = "chat_completions"

    async def _fake_tools(_messages, _st, _ctx, batch_trace=None, **_kwargs):
        if isinstance(batch_trace, dict):
            batch_trace.setdefault("executed", []).append("c1")
        return [output]

    async def _fake_outputs(messages, _st, _ctx):
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp"

    async def _compact(**kwargs):
        kwargs["state"].messages = [
            {"role": "user", "content": "SUMMARY", "id": "sum"}
        ]
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("trace failed after summary")

    monkeypatch.setattr(pr, "aprocess_tool_calls", _fake_tools)
    monkeypatch.setattr(pr, "aprocess_tool_outputs", _fake_outputs)
    monkeypatch.setattr(rsc, "arun_summary_compaction", _compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    ctx = _run_ctx()
    coord = RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )

    def _run():
        return run_coro(
            pr.arun_agent(
                _tool_call_batch(),
                state,
                ctx,
                pre_backend_compaction_hook=coord.apre_backend_compaction_hook,
                persist_messages_callback=persisted.extend,
            )
        )

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            _run()
        assert [message.get("id") for message in state.messages] == ["sum"]
    else:
        assert _run() == "готово"
        ids = [message.get("id") for message in state.messages]
        assert ids[0] == "sum"
        assert "fco" not in ids
        assert "fc" not in ids

    assert all(message.get("id") != "fco" for message in persisted)
    assert all(
        message.get("type") != "function_call_output"
        for message in state.messages
    )


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_summary_failed_before_replace_persists_held_output(
    monkeypatch, failure
):
    """Сбоя до подмены списка носитель ещё не в сводке — его пишем один раз.

    Если ``finally`` снимет ссылку безусловно, отложенный ответ пропадёт из трейса.
    """
    output = _held_output()
    persisted: list[dict] = []
    state = _state()
    state.backend_api_mode = "chat_completions"

    async def _fake_tools(_messages, _st, _ctx, batch_trace=None, **_kwargs):
        if isinstance(batch_trace, dict):
            batch_trace.setdefault("executed", []).append("c1")
        return [output]

    async def _fake_outputs(_messages, _st, _ctx):
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp"

    async def _compact(**_kwargs):
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("trace failed before summary")

    monkeypatch.setattr(pr, "aprocess_tool_calls", _fake_tools)
    monkeypatch.setattr(pr, "aprocess_tool_outputs", _fake_outputs)
    monkeypatch.setattr(rsc, "arun_summary_compaction", _compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    ctx = _run_ctx()
    coord = RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )

    def _run():
        return run_coro(
            pr.arun_agent(
                _tool_call_batch(),
                state,
                ctx,
                pre_backend_compaction_hook=coord.apre_backend_compaction_hook,
                persist_messages_callback=persisted.extend,
            )
        )

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            _run()
    else:
        assert _run() == "готово"

    assert [message.get("id") for message in persisted].count("fco") == 1
    assert any(message.get("id") == "fco" for message in state.messages)
    assert all(message.get("id") != "sum" for message in state.messages)


def test_force_compact_applied_then_failed_does_not_republish_held_output(
    monkeypatch,
):
    """Аварийная сводка подменила историю и затем упала — старый ответ не дописываем.

    Переполнение приходит из подготовки запроса, пока ответ ещё отложен.
    Исключение хука уходит как есть, без ``UnboundLocalError`` на ``applied``.
    """
    output = _held_output()
    persisted: list[dict] = []
    state = _state()

    async def _fake_tools(_messages, _st, _ctx, batch_trace=None, **_kwargs):
        if isinstance(batch_trace, dict):
            batch_trace.setdefault("executed", []).append("c1")
        return [output]

    async def _overflow(_state, _ctx):
        raise ValueError(
            "Requested token count exceeds the model's maximum context "
            "length of 131072 tokens. Please reduce the number of tokens"
        )

    async def _compact(*_args, **_kwargs):
        state.messages = [{"role": "user", "content": "SUMMARY", "id": "sum"}]
        raise RuntimeError("trace failed after summary")

    monkeypatch.setattr(pr, "aprocess_tool_calls", _fake_tools)
    monkeypatch.setattr(pr, "aprepare_main_call", _overflow)

    with pytest.raises(RuntimeError, match="trace failed after summary"):
        run_coro(
            pr.arun_agent(
                _tool_call_batch(),
                state,
                _run_ctx(),
                force_compact_hook=_compact,
                persist_messages_callback=persisted.extend,
            )
        )

    assert [message.get("id") for message in state.messages] == ["sum"]
    assert all(message.get("id") != "fco" for message in persisted)
    assert all(
        message.get("type") != "function_call_output"
        for message in state.messages
    )


def _held_output():
    return {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "тело ответа",
        "id": "fco",
    }


def _tool_call_batch():
    return [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "fs_read",
            "arguments": "{}",
            "id": "fc",
        }
    ]


def test_fill_failure_persists_held_tool_output(monkeypatch):
    """Отказ сбора подсказок не оставляет ответ инструмента только в памяти."""
    output = _held_output()
    persisted: list[dict] = []

    async def _fake_tools(
        _messages, _state, _ctx, batch_trace=None, **_kwargs
    ):
        if isinstance(batch_trace, dict):
            batch_trace.setdefault("executed", []).append("c1")
        return [output]

    def _memory_down(*_args, **_kwargs):
        raise ExecutionFailure("memory store down")

    monkeypatch.setattr(pr, "aprocess_tool_calls", _fake_tools)
    monkeypatch.setattr(nudge_mod, "notice_texts_before_llm", _memory_down)

    with pytest.raises(ExecutionFailure, match="memory store down"):
        run_coro(
            pr.arun_agent(
                _tool_call_batch(),
                _state(),
                _run_ctx(),
                persist_messages_callback=persisted.extend,
                session_meta={},
            )
        )

    assert [message.get("id") for message in persisted].count("fco") == 1


def test_check_mode_bounce_persists_held_tool_output(monkeypatch):
    """Отскок из-за неразобранного батча всё равно пишет отложенный ответ."""
    output = _held_output()
    persisted: list[dict] = []

    async def _fake_tools(
        _messages, _state, _ctx, batch_trace=None, **_kwargs
    ):
        if isinstance(batch_trace, dict):
            batch_trace.setdefault("executed", []).append("c1")
        return [output]

    async def _done(messages, state, ctx):
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp"

    real_check = pr.check_mode

    def _check(messages, logger):
        if any(
            isinstance(message, dict) and message.get("id") == "fco"
            for message in messages
        ):
            raise ValueError("Undefined mode")
        return real_check(messages, logger)

    monkeypatch.setattr(pr, "aprocess_tool_calls", _fake_tools)
    monkeypatch.setattr(pr, "aprocess_user_ask", _done)
    monkeypatch.setattr(pr, "check_mode", _check)

    result = run_coro(
        pr.arun_agent(
            _tool_call_batch(),
            _state(),
            _run_ctx(),
            persist_messages_callback=persisted.extend,
            session_meta={},
        )
    )

    assert result == "готово"
    assert [message.get("id") for message in persisted].count("fco") == 1


_OVERFLOW = (
    "Requested token count exceeds the model's maximum context "
    "length of 131072 tokens. Please reduce the number of tokens"
)

_TOOL_IMAGE = {
    "id": "3f9a1c27b0e4d5a6",
    "kind": "image",
    "mime": "image/png",
    "origin": "tool",
    "label": "Image from fs_view_image",
    "path": "/threads/images/shot.png",
}


def _overflow_coordinator(monkeypatch, state, kept):
    """Аварийная сводка подменяется: история становится одной сводкой."""

    async def _compact(**kwargs):
        kept.append([m.get("id") for m in kwargs.get("kept_messages") or []])
        kwargs["state"].messages = [
            {"role": "user", "content": "SUMMARY", "id": "sum"}
        ]

    monkeypatch.setattr(rsc, "arun_summary_compaction", _compact)
    state.backend_api_mode = "chat_completions"
    return RunSummaryCoordinator(
        ctx=_run_ctx(),
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )


def test_overflow_keeps_tool_image_and_drops_output(monkeypatch):
    """Аварийная сводка оставляет картинку тула и не оставляет ответ без вызова."""
    output = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "тело",
        "id": "fco",
    }
    image = {
        "role": "user",
        "content": wrap_technical_user_content(
            "Image from fs_view_image: /work/shot.png"
        ),
        "id": "m_i",
        "attachments": [_TOOL_IMAGE],
    }
    seen = []
    kept = []
    state = _state()
    state.messages = [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "fs_view_image",
            "arguments": "{}",
            "id": "fc",
        }
    ]

    async def _fake_main(messages, _st, _ctx):
        seen.append([m.get("id") for m in messages])
        if len(seen) == 1:
            raise ValueError(_OVERFLOW)
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp_2"

    monkeypatch.setattr(pr, "aprocess_tool_outputs", _fake_main)
    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_main)
    coord = _overflow_coordinator(monkeypatch, state, kept)

    result = run_coro(
        pr.arun_agent(
            [output, image],
            state,
            _run_ctx(),
            force_compact_hook=coord.aforce_compact_once,
        )
    )

    assert result == "готово"
    assert seen[1] == ["sum", "m_i"]
    assert "fco" not in seen[1]
    assert kept == [["m_i"]]


def test_overflow_keeps_user_image(monkeypatch):
    """Картинка свежей реплики переживает аварийную сводку, старая история — нет."""
    user = {
        "role": "user",
        "content": "посмотри",
        "id": "u_img",
        "attachments": [
            {
                "id": "aabbccddeeff0011",
                "kind": "image",
                "mime": "image/png",
                "origin": "user",
                "label": "скрин",
                "path": "/threads/images/user.png",
            }
        ],
    }
    seen = []
    kept = []
    state = _state()
    state.messages = [{"role": "user", "content": "давно", "id": "old"}]

    async def _fake_main(messages, _st, _ctx):
        seen.append([m.get("id") for m in messages])
        if len(seen) == 1:
            raise ValueError(_OVERFLOW)
        return [
            {"role": "assistant", "type": "message", "content": "готово"}
        ], "resp_2"

    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_main)
    coord = _overflow_coordinator(monkeypatch, state, kept)

    result = run_coro(
        pr.arun_agent(
            [user],
            state,
            _run_ctx(),
            force_compact_hook=coord.aforce_compact_once,
        )
    )

    assert result == "готово"
    assert seen[1] == ["sum", "u_img"]
    assert seen[1].count("old") == 0
    assert kept == [["u_img"]]
    resumed = next(m for m in state.messages if m.get("id") == "u_img")
    assert resumed["attachments"][0]["origin"] == "user"


def test_non_overflow_error_still_propagates(monkeypatch):
    async def _boom(messages, state, ctx):
        raise ValueError("some other backend failure")

    monkeypatch.setattr(pr, "aprocess_user_ask", _boom)

    with pytest.raises(ValueError, match="some other backend failure"):
        run_coro(
            pr.arun_agent(
                [{"role": "user", "content": "x", "id": "u1"}],
                _state(),
                _run_ctx(),
                force_compact_hook=lambda: None,  # не должен вызваться
            )
        )
