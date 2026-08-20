"""run_agent при переполнении контекста: force-компакция + повтор, не краш."""
from gc_core import processing_responses as pr


def _state():
    s = type("S", (), {})()
    s.messages = []
    s.steps = 0
    s.max_steps = 100
    s.current_user_intent = []
    s.user_intent_log = []
    s.flush_current_user_intent = lambda: None
    return s


def _run_ctx():
    cd = {"backend_api_mode": "chat_completions", "console_trace": False,
          "no_tool_call_max_count": 3, "strict_turn_finish_validation": True,
          "text_answer_mode": "allow"}
    return type("C", (), {"config_dict": cd, "logger": None})()


def test_context_overflow_triggers_compaction_and_retries(monkeypatch):
    calls = {"backend": 0, "compact": 0}

    async def _fake_process_user_ask(messages, state, ctx):
        calls["backend"] += 1
        if calls["backend"] == 1:
            raise ValueError(
                "Requested token count exceeds the model's maximum context "
                "length of 131072 tokens. Please reduce the number of tokens"
            )
        return [{"role": "assistant", "type": "message", "content": "готово"}], "resp_2"

    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)

    result = pr.run_agent(
        [{"role": "user", "content": "сделай", "id": "u1"}],
        _state(),
        _run_ctx(),
        force_compact_hook=lambda: calls.__setitem__("compact", calls["compact"] + 1),
    )

    assert result == "готово"          # прогон восстановился, не краш
    assert calls["backend"] == 2        # 1-й overflow, 2-й успех
    assert calls["compact"] == 1        # force-компакция вызвана один раз


def test_non_overflow_error_still_propagates(monkeypatch):
    import pytest

    async def _boom(messages, state, ctx):
        raise ValueError("some other backend failure")

    monkeypatch.setattr(pr, "aprocess_user_ask", _boom)

    with pytest.raises(ValueError, match="some other backend failure"):
        pr.run_agent(
            [{"role": "user", "content": "x", "id": "u1"}],
            _state(),
            _run_ctx(),
            force_compact_hook=lambda: None,   # не должен вызваться
        )
