"""C3: run_agent при WrongFunctionArgumentsException добавляет truncation-hint,
если finish_reason последнего backend-ответа == "length", и не добавляет иначе."""

from gc_core import processing_responses as pr
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _state():
    s = type("S", (), {})()
    s.messages = []
    s.steps = 0
    s.max_steps = 100
    s.current_user_intent = []
    s.user_intent_log = []
    s.flush_current_user_intent = lambda: None
    s.last_finish_reason = None
    s.tick_signals = set()
    s.mark_turn_start = lambda: None
    return s


def _run_ctx():
    cd = {
        "backend.api_mode": "chat_completions",
        "logging.console_trace": False,
        "agent.no_tool_call_max_count": 3,
        "agent.strict_turn_finish_validation": True,
        "text_answer_mode": "allow",
    }
    return fake_ctx(cd, logger=None)


def _run_with_finish_reason(monkeypatch, finish_reason, extra_hint=None):
    """Запускает run_agent: 1-й backend-ответ → function_call → apply_tool падает
    WrongFunctionArgumentsException; 2-й backend-ответ → assistant text (принят).
    Возвращает feedback, переданный в _make_technical_feedback_user_message."""
    captured_feedback = {}

    async def _fake_process_user_ask(messages, state, ctx):
        if not captured_feedback.get("backend_called"):
            captured_feedback["backend_called"] = True
            # finish_reason первого ответа — тот, что тестируем для truncation-hint
            # на WrongFunctionArgumentsException.
            state.last_finish_reason = finish_reason
            return [
                {
                    "role": "assistant",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "fs_write_file",
                    "arguments": '{"path": "/tmp/x.md", "content": "hello"}',
                }
            ], "resp_1"
        # Recovery-текст: length/blocked не принимаются как финал (finish_prevents_text_final).
        state.last_finish_reason = "stop"
        return [
            {
                "role": "assistant",
                "type": "message",
                "content": "готово",
            }
        ], "resp_2"

    async def _fake_process_tool_calls(messages, state, ctx, **_kwargs):
        raise pr.WrongFunctionArgumentsException(
            message="некорректный JSON: Expecting property name enclosed in double quotes",
            received_args='{"path": "/tmp/x.md", "content": "hello"}',
            expected_schema={"parameters": {"properties": {}, "required": []}},
            tool_name="fs_write_file",
            extra_hint=extra_hint,
        )

    def _fake_make_feedback(content, technical_type=None, call_id=None):
        # Первый feedback (WrongFunctionArguments) — то, что проверяем; bounce'ы
        # после recovery не должны его затирать.
        if "content" not in captured_feedback:
            captured_feedback["content"] = content
        return {"role": "user", "id": "m_tech", "content": content}

    monkeypatch.setattr(pr, "aprocess_user_ask", _fake_process_user_ask)
    monkeypatch.setattr(pr, "aprocess_tool_calls", _fake_process_tool_calls)
    monkeypatch.setattr(
        pr, "_make_technical_feedback_user_message", _fake_make_feedback
    )

    # Избегаем дёрганья LLM-summarizer для intent-log при финальном ответе.
    # Заглушка корутинная: цикл ждёт хук нативно, синхронное имя ушло вместе
    # с лишним прыжком через поток и портал.
    async def _no_final_answer_intent(**_kw):
        return None

    monkeypatch.setattr(
        pr, "amaybe_record_final_answer_intent", _no_final_answer_intent
    )

    state = _state()
    state.last_finish_reason = finish_reason
    result = run_coro(
        pr.arun_agent(
            [{"role": "user", "content": "сделай", "id": "u1"}],
            state,
            _run_ctx(),
        )
    )
    captured_feedback["result"] = result
    return captured_feedback


def test_truncation_hint_added_when_finish_reason_length(monkeypatch):
    fb = _run_with_finish_reason(monkeypatch, finish_reason="length")
    assert "truncated" in fb["content"]
    assert "length" in fb["content"]
    assert fb["result"] == "готово"


def test_no_truncation_hint_when_finish_reason_stop(monkeypatch):
    fb = _run_with_finish_reason(monkeypatch, finish_reason="stop")
    assert "truncated" not in fb["content"]
    assert fb["result"] == "готово"


def test_no_truncation_hint_when_finish_reason_none(monkeypatch):
    fb = _run_with_finish_reason(monkeypatch, finish_reason=None)
    assert "truncated" not in fb["content"]
    assert fb["result"] == "готово"


def test_truncation_hint_keeps_the_tool_hint(monkeypatch):
    fb = _run_with_finish_reason(
        monkeypatch, "length", extra_hint="TOOL HINT: path must be absolute"
    )
    assert "TOOL HINT: path must be absolute" in fb["content"]
    assert "truncated" in fb["content"]
