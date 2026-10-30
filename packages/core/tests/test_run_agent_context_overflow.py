"""run_agent при переполнении контекста: force-компакция + повтор, не краш."""

from gc_core import processing_responses as pr
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
    s.flush_current_user_intent = lambda: None
    return s


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
            force_compact_hook=lambda: calls.__setitem__(
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

    def _notice(messages, *_args, **_kwargs):
        calls["notices"] += 1
        return [
            *messages,
            {"role": "user", "content": "NOTICE", "id": "notice-1"},
        ]

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

    monkeypatch.setattr(pr, "apply_tool_usage_notice_before_llm", _notice)
    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)

    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "сделай", "id": "u1"}],
            _state(),
            _run_ctx(),
            force_compact_hook=lambda: None,
            session_meta={},
        )
    )

    assert result == "готово"
    assert calls["notices"] == 1
    assert [m.get("content") for m in calls["seen"][1]].count("NOTICE") == 1


def test_next_step_collects_notices_again(monkeypatch):
    """Следующий запрос к модели после успешного шага снова собирает подсказки."""
    calls = {"backend": 0, "notices": 0}

    def _notice(messages, *_args, **_kwargs):
        calls["notices"] += 1
        return messages

    async def _fake_process_user_ask(messages, state, ctx):
        calls["backend"] += 1
        return [
            {"role": "assistant", "type": "message", "content": "не тул"}
        ], "resp"

    monkeypatch.setattr(pr, "apply_tool_usage_notice_before_llm", _notice)
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

    def _notice(messages, *_args, **_kwargs):
        calls["notices"] += 1
        return [
            *messages,
            {"role": "user", "content": "NOTICE", "id": "notice-1"},
        ]

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
    monkeypatch.setattr(pr, "apply_tool_usage_notice_before_llm", _notice)
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
    assert [m.get("content") for m in calls["seen"][0]].count("NOTICE") == 0
    assert state.tool_usage_notice_cnt == {}
    assert state.todo_reminder_cnt == 0
    assert notice["project_memory_check_cnt"] == 0
    assert state.mode_announced == "default"


def test_non_overflow_error_still_propagates(monkeypatch):
    import pytest

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
