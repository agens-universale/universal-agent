"""TUI adaptive bounce: судья текст-ответа переиспользует finish-gate-судью."""

from types import SimpleNamespace

from gc_core.intent_guard import text_answer_judge as taj
from gc_core.intent_guard.text_answer_judge import (
    TextAnswerDecision,
    ashould_return_text_answer,
)
from gc_core.intent_guard.validation_result import ValidationResult
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _ctx(mode=None, backend="chat_completions"):
    cfg = {"backend_api_mode": backend}
    if mode is not None:
        cfg["text_answer_mode"] = mode
    return fake_ctx(cfg, logger=None)


class _S:
    current_user_intent = []
    user_intent_log = []


# ── новые тесты (Step 1) ──────────────────────────────────────────────────────


def test_empty_text_rejects_any_mode():
    d = run_coro(ashould_return_text_answer(_ctx("allow"), object(), "   "))
    assert d == TextAnswerDecision(False, None)


def test_allow_accepts_without_judge():
    d = run_coro(ashould_return_text_answer(_ctx("allow"), object(), "hi"))
    assert d.accept is True and d.reject_feedback is None


def test_deny_rejects_generic():
    d = run_coro(ashould_return_text_answer(_ctx("deny"), object(), "hi"))
    assert d.accept is False and d.reject_feedback is None


def test_judge_accept(monkeypatch):
    async def _judge(*a, **k):
        return SimpleNamespace(action="accept")

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(ashould_return_text_answer(_ctx("judge"), object(), "answer"))
    assert d.accept is True and d.reject_feedback is None


def test_judge_reject_carries_feedback(monkeypatch):
    res = ValidationResult(
        ok=False,
        action="continue_work",
        trace_ok=False,
        report_ok=True,
        trace_gaps=["need to run tests"],
        missing_requirements=[],
        report_mismatches=[],
        conflicts=[],
        coverage_score=0.0,
        feedback_for_agent="",
        is_question=False,
    )

    async def _judge(*a, **k):
        return res

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(ashould_return_text_answer(_ctx("judge"), object(), "answer"))
    assert d.accept is False
    assert d.reject_feedback is not None
    assert (
        "need to run tests" in d.reject_feedback
    )  # причина судьи присутствует
    assert "turn_finish" not in d.reject_feedback  # tool-agnostic
    assert (
        "next step" in d.reject_feedback.lower()
    )  # tool hint при is_question=false


def test_judge_reject_is_question_omits_tool_hint(monkeypatch):
    res = ValidationResult(
        ok=False,
        action="continue_work",
        trace_ok=False,
        report_ok=True,
        trace_gaps=["need user choice"],
        missing_requirements=[],
        report_mismatches=[],
        conflicts=[],
        coverage_score=0.0,
        feedback_for_agent="",
        is_question=True,
    )

    async def _judge(*a, **k):
        return res

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(
        ashould_return_text_answer(_ctx("judge"), object(), "1 или 2?")
    )
    assert d.accept is False
    assert d.reject_feedback is not None
    assert "need user choice" in d.reject_feedback
    assert "next step" not in d.reject_feedback.lower()
    assert "ask_human" not in d.reject_feedback.lower()


def test_judge_error_falls_back_to_generic(monkeypatch):
    async def _judge(*a, **k):
        return None

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(ashould_return_text_answer(_ctx("judge"), object(), "answer"))
    assert d.accept is False and d.reject_feedback is None


def test_judge_not_available_without_chat_completions_backend(monkeypatch):
    """judge_available инфраструктурный: backend != chat_completions → deny без судьи."""
    called = {"n": 0}

    async def _judge(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(
        ashould_return_text_answer(
            _ctx("judge", backend="responses"), object(), "answer"
        )
    )
    assert d.accept is False and called["n"] == 0


# ── старые тесты переведены на новую сигнатуру ───────────────────────────────


def test_judge_accepts_when_action_accept(monkeypatch):
    async def _validate(**kw):
        return type("R", (), {"action": "accept"})()

    monkeypatch.setattr(
        taj, "avalidate_final_report_vs_user_intent", _validate
    )
    result = run_coro(
        taj.ajudge_text_answer(_ctx(), _S(), "Привет! Чем помочь?")
    )
    assert getattr(result, "action", None) == "accept"


def test_judge_bounces_when_action_continue(monkeypatch):
    async def _validate(**kw):
        return type("R", (), {"action": "continue_work"})()

    monkeypatch.setattr(
        taj, "avalidate_final_report_vs_user_intent", _validate
    )
    result = run_coro(taj.ajudge_text_answer(_ctx(), _S(), "Сейчас сделаю..."))
    assert getattr(result, "action", None) == "continue_work"


def test_judge_bounces_on_validator_error(monkeypatch):
    async def boom(**kw):
        raise RuntimeError("validator down")

    monkeypatch.setattr(taj, "avalidate_final_report_vs_user_intent", boom)
    result = run_coro(taj.ajudge_text_answer(_ctx(), _S(), "что-то"))
    assert result is None  # строгий fallback → None


def test_judge_passes_text_as_finish_arguments(monkeypatch):
    """Текст подаётся судье как finish_handoff_arguments (полный контекст — изнутри)."""
    seen = {}

    async def cap(**kw):
        seen.update(kw)
        return type("R", (), {"action": "accept"})()

    monkeypatch.setattr(taj, "avalidate_final_report_vs_user_intent", cap)
    run_coro(taj.ajudge_text_answer(_ctx(), _S(), "мой текст-ответ"))
    assert seen["finish_handoff_arguments"] == "мой текст-ответ"
