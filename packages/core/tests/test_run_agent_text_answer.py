"""Решение «принять текст-ответ?» по режиму text_answer_mode (юниты) + интеграция
в run_agent (текст-ответ возвращается как ответ хода). prose+tool не затрагивается:
хук живёт только в ветке mode == text_response. judge активен, только если доступна
машинерия судейства (backend_api_mode == chat_completions) — от тоггла /gate
(strict_turn_finish_validation, finish-gate) НЕ зависит."""

from gc_core.intent_guard.text_answer_judge import ashould_return_text_answer
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx


class _S:
    current_user_intent = []
    user_intent_log = []


def _ctx(mode=None, backend="chat_completions", strict=True):
    cd = {
        "backend.api_mode": backend,
        "agent.strict_turn_finish_validation": strict,
    }
    if mode is not None:
        cd["text_answer_mode"] = mode
    return fake_ctx(cd, logger=None)


# --- решение по режиму (изолированно) -----------------------------------------


def test_allow_returns_true_without_judge(monkeypatch):
    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(
            ashould_return_text_answer(_ctx("allow"), _S(), "привет")
        ).accept
        is True
    )
    assert judged["n"] == 0  # allow → судья не зван


def test_judge_accept_returns_true(monkeypatch):
    from types import SimpleNamespace

    from gc_core.intent_guard import text_answer_judge as taj

    async def _judge(ctx, state, text):
        return SimpleNamespace(action="accept")

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(
            ashould_return_text_answer(_ctx("judge"), _S(), "привет")
        ).accept
        is True
    )


def test_judge_reject_returns_false(monkeypatch):
    from gc_core.intent_guard import text_answer_judge as taj
    from gc_core.intent_guard.validation_result import ValidationResult

    _rej = ValidationResult(
        ok=False,
        action="continue_work",
        trace_ok=False,
        report_ok=True,
        trace_gaps=["more work needed"],
        missing_requirements=[],
        report_mismatches=[],
        conflicts=[],
        feedback_for_agent="",
        coverage_score=0.0,
    )

    async def _judge(ctx, state, text):
        return _rej

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(
            ashould_return_text_answer(_ctx("judge"), _S(), "сделай отчёт")
        ).accept
        is False
    )


def test_deny_returns_false_without_judge(monkeypatch):
    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(
            ashould_return_text_answer(_ctx("deny"), _S(), "привет")
        ).accept
        is False
    )
    assert judged["n"] == 0


def test_no_key_returns_false_without_judge(monkeypatch):
    """Автономка (CLI/server/swarm): нет ключа → строго, судья не зван."""
    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(ashould_return_text_answer(_ctx(None), _S(), "привет")).accept
        is False
    )
    assert judged["n"] == 0


# --- gate: judge зависит только от инфраструктуры (chat_completions), --------
# --- НЕ от тоггла /gate (strict_turn_finish_validation) ------------------------


def test_judge_gate_on_calls_judge(monkeypatch):
    """(a) gate=on (strict=True) + judge → судья зовётся."""
    from types import SimpleNamespace

    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return SimpleNamespace(action="accept")

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(
        ashould_return_text_answer(_ctx("judge", strict=True), _S(), "привет")
    )
    assert judged["n"] == 1 and d.accept is True


def test_judge_gate_off_still_calls_judge(monkeypatch):
    """(b) gate=off (strict=False) + judge → судья ВСЁ РАВНО зовётся (/gate не
    должен молча деградировать judge в deny; было: FinishGateGuard.enabled склеивал
    strict_turn_finish_validation с backend_api_mode)."""
    from types import SimpleNamespace

    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return SimpleNamespace(action="accept")

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(
        ashould_return_text_answer(_ctx("judge", strict=False), _S(), "привет")
    )
    assert judged["n"] == 1 and d.accept is True


def test_judge_without_chat_completions_is_strict(monkeypatch):
    """(e) backend != chat_completions + judge → deny без судьи, при gate=on."""
    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(
            ashould_return_text_answer(
                _ctx("judge", backend="responses", strict=True), _S(), "привет"
            )
        ).accept
        is False
    )
    assert judged["n"] == 0


def test_judge_without_chat_completions_is_strict_even_with_gate_off(
    monkeypatch,
):
    """(e) backend != chat_completions + judge → deny без судьи, тоже при gate=off
    (инфраструктурное условие не подменяется пользовательской ручкой)."""
    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(
            ashould_return_text_answer(
                _ctx("judge", backend="responses", strict=False),
                _S(),
                "привет",
            )
        ).accept
        is False
    )
    assert judged["n"] == 0


def test_allow_accepts_without_judge_regardless_of_gate(monkeypatch):
    """(c) allow → accept без судьи при любом gate."""
    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(
            ashould_return_text_answer(
                _ctx("allow", strict=True), _S(), "привет"
            )
        ).accept
        is True
    )
    assert (
        run_coro(
            ashould_return_text_answer(
                _ctx("allow", strict=False), _S(), "привет"
            )
        ).accept
        is True
    )
    assert judged["n"] == 0


def test_deny_rejects_regardless_of_gate(monkeypatch):
    """(d) deny/нет ключа → reject при любом gate."""
    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(
            ashould_return_text_answer(
                _ctx("deny", strict=True), _S(), "привет"
            )
        ).accept
        is False
    )
    assert (
        run_coro(
            ashould_return_text_answer(
                _ctx("deny", strict=False), _S(), "привет"
            )
        ).accept
        is False
    )
    assert (
        run_coro(
            ashould_return_text_answer(_ctx(None, strict=True), _S(), "привет")
        ).accept
        is False
    )
    assert (
        run_coro(
            ashould_return_text_answer(
                _ctx(None, strict=False), _S(), "привет"
            )
        ).accept
        is False
    )
    assert judged["n"] == 0


def test_empty_text_bounces_in_all_modes(monkeypatch):
    """Пустой/пробельный текст — не ответ хода: bounce даже в always; судья не зван."""
    from gc_core.intent_guard import text_answer_judge as taj

    judged = {"n": 0}

    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert (
        run_coro(ashould_return_text_answer(_ctx("allow"), _S(), "   ")).accept
        is False
    )
    assert (
        run_coro(ashould_return_text_answer(_ctx("judge"), _S(), "")).accept
        is False
    )
    assert judged["n"] == 0


# --- интеграция в run_agent ---------------------------------------------------


def _state():
    s = type("S", (), {})()
    s.messages = []
    s.steps = 0
    s.max_steps = 100
    s.current_user_intent = []
    s.user_intent_log = []
    s.flush_current_user_intent = lambda: None  # finish-accept флашит интент
    return s


def _run_ctx(mode):
    cd = {
        "backend.api_mode": "chat_completions",
        "logging.console_trace": False,
        "agent.no_tool_call_max_count": 3,
        "agent.strict_turn_finish_validation": True,
    }
    if mode is not None:
        cd["text_answer_mode"] = mode
    return fake_ctx(cd, logger=None)


def _text_msgs():
    return [
        {
            "role": "assistant",
            "type": "message",
            "content": "Привет! Чем помочь?",
        }
    ]


def test_run_agent_allow_returns_text():
    """allow: текст-ответ становится ответом хода (реальное решение, без мока)."""
    from gc_core import processing_responses as pr

    result = run_coro(pr.arun_agent(_text_msgs(), _state(), _run_ctx("allow")))
    assert result == "Привет! Чем помочь?"


def test_run_agent_judge_accept_returns_text(monkeypatch):
    """judge + accept: текст возвращается (gate включён, судья замокан)."""
    from types import SimpleNamespace

    from gc_core import processing_responses as pr
    from gc_core.intent_guard import text_answer_judge as taj

    async def _judge(ctx, state, text):
        return SimpleNamespace(action="accept")

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    result = run_coro(pr.arun_agent(_text_msgs(), _state(), _run_ctx("judge")))
    assert result == "Привет! Чем помочь?"


def test_run_agent_does_not_accept_an_echo_of_the_bridge(monkeypatch):
    """Модель переняла из истории наш мост «(tool results received)» и выдала его
    ответом: это не итог хода — напоминание вызвать тул, как при прозе без вызова
    (смоук 2026-10-01: субагент отдал родителю такой «результат»)."""
    from gc_core import processing_responses as pr
    from gc_core.message_policy.chat_completions import BRIDGE_TOOL_RESULTS
    from gc_core.message_policy.no_tool_call_bounce import (
        NO_TOOL_CALL_BOUNCE_MESSAGE,
    )

    captured = {}

    def _fake_make_feedback(content, technical_type=None, call_id=None):
        captured["content"] = content
        return {"role": "user", "id": "m_tech", "content": content}

    def _fake_process_user_ask(messages, state, ctx):
        state.last_finish_reason = "stop"
        return [
            {"role": "assistant", "type": "message", "content": "итог"}
        ], "resp_2"

    monkeypatch.setattr(
        pr, "_make_technical_feedback_user_message", _fake_make_feedback
    )
    monkeypatch.setattr(
        pr, "aprocess_user_ask", as_coro(_fake_process_user_ask)
    )
    monkeypatch.setattr(
        pr, "amaybe_record_final_answer_intent", as_coro(lambda **_kw: None)
    )

    echo = [
        {
            "role": "assistant",
            "type": "message",
            "content": BRIDGE_TOOL_RESULTS,
        }
    ]
    state = _state()
    state.last_finish_reason = "stop"
    result = run_coro(pr.arun_agent(echo, state, _run_ctx("allow")))
    assert result == "итог"
    assert captured["content"] == NO_TOOL_CALL_BOUNCE_MESSAGE


def test_run_agent_content_filter_bounces_even_in_allow(monkeypatch):
    """content_filter — отказ провайдера, не финальный текст; allow не принимает."""
    from gc_core import processing_responses as pr
    from gc_core.message_policy.no_tool_call_bounce import (
        CONTENT_FILTER_BOUNCE_MESSAGE,
    )

    captured = {}

    def _fake_make_feedback(content, technical_type=None, call_id=None):
        captured["content"] = content
        return {"role": "user", "id": "m_tech", "content": content}

    def _fake_process_user_ask(messages, state, ctx):
        state.last_finish_reason = "stop"
        return [
            {"role": "assistant", "type": "message", "content": "ok"}
        ], "resp_2"

    monkeypatch.setattr(
        pr, "_make_technical_feedback_user_message", _fake_make_feedback
    )
    monkeypatch.setattr(
        pr, "aprocess_user_ask", as_coro(_fake_process_user_ask)
    )
    monkeypatch.setattr(
        pr, "amaybe_record_final_answer_intent", as_coro(lambda **_kw: None)
    )

    state = _state()
    state.last_finish_reason = "content_filter"
    result = run_coro(pr.arun_agent(_text_msgs(), state, _run_ctx("allow")))
    assert result == "ok"
    assert captured["content"] == CONTENT_FILTER_BOUNCE_MESSAGE


def test_run_agent_refusal_bounces_even_in_allow(monkeypatch):
    """refusal/safety — тот же blocked-kind, не финал в allow."""
    from gc_core import processing_responses as pr
    from gc_core.message_policy.no_tool_call_bounce import (
        PROVIDER_BLOCKED_BOUNCE_MESSAGE,
    )

    captured = {}

    def _fake_make_feedback(content, technical_type=None, call_id=None):
        captured["content"] = content
        return {"role": "user", "id": "m_tech", "content": content}

    def _fake_process_user_ask(messages, state, ctx):
        state.last_finish_reason = "stop"
        return [
            {"role": "assistant", "type": "message", "content": "ok"}
        ], "resp_2"

    monkeypatch.setattr(
        pr, "_make_technical_feedback_user_message", _fake_make_feedback
    )
    monkeypatch.setattr(
        pr, "aprocess_user_ask", as_coro(_fake_process_user_ask)
    )
    monkeypatch.setattr(
        pr, "amaybe_record_final_answer_intent", as_coro(lambda **_kw: None)
    )

    for blocked in ("refusal", "safety"):
        captured.clear()
        state = _state()
        state.last_finish_reason = blocked
        result = run_coro(
            pr.arun_agent(_text_msgs(), state, _run_ctx("allow"))
        )
        assert result == "ok"
        assert captured["content"] == PROVIDER_BLOCKED_BOUNCE_MESSAGE


def test_run_agent_length_text_uses_truncation_bounce(monkeypatch):
    """length без tool call — truncation nudge; allow тоже не принимает финалом."""
    from gc_core import processing_responses as pr
    from gc_core.message_policy.no_tool_call_bounce import (
        LENGTH_BOUNCE_MESSAGE,
    )

    captured = {}

    def _fake_make_feedback(content, technical_type=None, call_id=None):
        captured["content"] = content
        return {"role": "user", "id": "m_tech", "content": content}

    def _fake_process_user_ask(messages, state, ctx):
        # Следующий ход — валидный финал; иначе deny снова bounce'ит «ok».
        ctx.runtime = ctx.runtime.with_values(text_answer_mode="allow")
        state.last_finish_reason = "stop"
        return [
            {"role": "assistant", "type": "message", "content": "ok"}
        ], "resp_2"

    monkeypatch.setattr(
        pr, "_make_technical_feedback_user_message", _fake_make_feedback
    )
    monkeypatch.setattr(
        pr, "aprocess_user_ask", as_coro(_fake_process_user_ask)
    )
    monkeypatch.setattr(
        pr, "amaybe_record_final_answer_intent", as_coro(lambda **_kw: None)
    )

    for mode in ("deny", "allow"):
        captured.clear()
        state = _state()
        state.last_finish_reason = "length"
        result = run_coro(pr.arun_agent(_text_msgs(), state, _run_ctx(mode)))
        assert result == "ok"
        assert captured["content"] == LENGTH_BOUNCE_MESSAGE


def test_run_agent_empty_batch_content_filter_uses_filter_nudge(monkeypatch):
    """Пустой output после content_filter — тот же rewrite-надж, не «нет tool call»."""
    from gc_core import processing_responses as pr
    from gc_core.message_policy.no_tool_call_bounce import (
        CONTENT_FILTER_BOUNCE_MESSAGE,
    )

    captured = {}

    def _fake_make_feedback(content, technical_type=None, call_id=None):
        captured["content"] = content
        return {"role": "user", "id": "m_tech", "content": content}

    def _fake_process_user_ask(messages, state, ctx):
        state.last_finish_reason = "stop"
        return [
            {"role": "assistant", "type": "message", "content": "ok"}
        ], "resp_2"

    monkeypatch.setattr(
        pr, "_make_technical_feedback_user_message", _fake_make_feedback
    )
    monkeypatch.setattr(
        pr, "aprocess_user_ask", as_coro(_fake_process_user_ask)
    )
    monkeypatch.setattr(
        pr, "amaybe_record_final_answer_intent", as_coro(lambda **_kw: None)
    )

    state = _state()
    state.last_finish_reason = "content_filter"
    result = run_coro(pr.arun_agent([], state, _run_ctx("allow")))
    assert result == "ok"
    assert captured["content"] == CONTENT_FILTER_BOUNCE_MESSAGE


def test_run_agent_empty_batch_length_uses_truncation_nudge(monkeypatch):
    from gc_core import processing_responses as pr
    from gc_core.message_policy.no_tool_call_bounce import (
        LENGTH_BOUNCE_MESSAGE,
    )

    captured = {}

    def _fake_make_feedback(content, technical_type=None, call_id=None):
        captured["content"] = content
        return {"role": "user", "id": "m_tech", "content": content}

    def _fake_process_user_ask(messages, state, ctx):
        state.last_finish_reason = "stop"
        return [
            {"role": "assistant", "type": "message", "content": "ok"}
        ], "resp_2"

    monkeypatch.setattr(
        pr, "_make_technical_feedback_user_message", _fake_make_feedback
    )
    monkeypatch.setattr(
        pr, "aprocess_user_ask", as_coro(_fake_process_user_ask)
    )
    monkeypatch.setattr(
        pr, "amaybe_record_final_answer_intent", as_coro(lambda **_kw: None)
    )

    state = _state()
    state.last_finish_reason = "length"
    result = run_coro(pr.arun_agent([], state, _run_ctx("allow")))
    assert result == "ok"
    assert captured["content"] == LENGTH_BOUNCE_MESSAGE
