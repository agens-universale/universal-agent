"""TUI adaptive bounce: судья текст-ответа переиспользует finish-gate-судью."""
from types import SimpleNamespace
import intent_guard.text_answer_judge as taj
from intent_guard.text_answer_judge import should_return_text_answer, TextAnswerDecision
from intent_guard.validation_result import ValidationResult


def _ctx(mode=None):
    cfg = {"text_answer_mode": mode} if mode is not None else {}
    return SimpleNamespace(config_dict=cfg, logger=None)


class _S:
    current_user_intent = []
    user_intent_log = []


# ── новые тесты (Step 1) ──────────────────────────────────────────────────────

def test_empty_text_rejects_any_mode():
    d = should_return_text_answer(_ctx("allow"), object(), "   ")
    assert d == TextAnswerDecision(False, None)


def test_allow_accepts_without_judge():
    d = should_return_text_answer(_ctx("allow"), object(), "hi")
    assert d.accept is True and d.reject_feedback is None


def test_deny_rejects_generic():
    d = should_return_text_answer(_ctx("deny"), object(), "hi")
    assert d.accept is False and d.reject_feedback is None


def test_judge_accept(monkeypatch):
    monkeypatch.setattr(taj, "FinishGateGuard", lambda **kw: SimpleNamespace(enabled=True))
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda *a, **k: SimpleNamespace(action="accept"))
    d = should_return_text_answer(_ctx("judge"), object(), "answer")
    assert d.accept is True and d.reject_feedback is None


def test_judge_reject_carries_feedback(monkeypatch):
    monkeypatch.setattr(taj, "FinishGateGuard", lambda **kw: SimpleNamespace(enabled=True))
    res = ValidationResult(ok=False, action="continue_work", trace_ok=False, report_ok=True,
                           trace_gaps=["need to run tests"], missing_requirements=[],
                           report_mismatches=[], conflicts=[], coverage_score=0.0,
                           feedback_for_agent="")
    monkeypatch.setattr(taj, "judge_text_answer", lambda *a, **k: res)
    d = should_return_text_answer(_ctx("judge"), object(), "answer")
    assert d.accept is False
    assert d.reject_feedback is not None
    assert "need to run tests" in d.reject_feedback         # причина судьи присутствует
    assert "turn_finish" not in d.reject_feedback           # tool-agnostic


def test_judge_error_falls_back_to_generic(monkeypatch):
    monkeypatch.setattr(taj, "FinishGateGuard", lambda **kw: SimpleNamespace(enabled=True))
    monkeypatch.setattr(taj, "judge_text_answer", lambda *a, **k: None)
    d = should_return_text_answer(_ctx("judge"), object(), "answer")
    assert d.accept is False and d.reject_feedback is None


# ── старые тесты переведены на новую сигнатуру ───────────────────────────────

def test_judge_accepts_when_action_accept(monkeypatch):
    monkeypatch.setattr(taj, "validate_final_report_vs_user_intent",
                        lambda **kw: type("R", (), {"action": "accept"})())
    result = taj.judge_text_answer(_ctx(), _S(), "Привет! Чем помочь?")
    assert getattr(result, "action", None) == "accept"


def test_judge_bounces_when_action_continue(monkeypatch):
    monkeypatch.setattr(taj, "validate_final_report_vs_user_intent",
                        lambda **kw: type("R", (), {"action": "continue_work"})())
    result = taj.judge_text_answer(_ctx(), _S(), "Сейчас сделаю...")
    assert getattr(result, "action", None) == "continue_work"


def test_judge_bounces_on_validator_error(monkeypatch):
    def boom(**kw):
        raise RuntimeError("validator down")
    monkeypatch.setattr(taj, "validate_final_report_vs_user_intent", boom)
    result = taj.judge_text_answer(_ctx(), _S(), "что-то")
    assert result is None   # строгий fallback → None


def test_judge_passes_text_as_finish_arguments(monkeypatch):
    """Текст подаётся судье как finish_handoff_arguments (полный контекст — изнутри)."""
    seen = {}
    def cap(**kw):
        seen.update(kw)
        return type("R", (), {"action": "accept"})()
    monkeypatch.setattr(taj, "validate_final_report_vs_user_intent", cap)
    taj.judge_text_answer(_ctx(), _S(), "мой текст-ответ")
    assert seen["finish_handoff_arguments"] == "мой текст-ответ"
