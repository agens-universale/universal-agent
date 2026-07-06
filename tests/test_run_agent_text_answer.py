"""Решение «принять текст-ответ?» по режиму text_answer_mode (юниты) + интеграция
в run_agent (текст-ответ возвращается как ответ хода). prose+tool не затрагивается:
хук живёт только в ветке mode == text_response. judge активен только при включённом
finish-gate (chat_completions + strict_turn_finish_validation)."""
from intent_guard.text_answer_judge import should_return_text_answer


class _S:
    current_user_intent = []
    user_intent_log = []


def _ctx(mode=None, backend="chat_completions", strict=True):
    cd = {"backend_api_mode": backend, "strict_turn_finish_validation": strict}
    if mode is not None:
        cd["text_answer_mode"] = mode
    return type("C", (), {"config_dict": cd, "logger": None})()


# --- решение по режиму (изолированно) -----------------------------------------

def test_allow_returns_true_without_judge(monkeypatch):
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer(_ctx("allow"), _S(), "привет").accept is True
    assert judged["n"] == 0                              # allow → судья не зван


def test_judge_accept_returns_true(monkeypatch):
    from types import SimpleNamespace
    import intent_guard.text_answer_judge as taj
    monkeypatch.setattr(taj, "judge_text_answer", lambda ctx, state, text: SimpleNamespace(action="accept"))
    assert should_return_text_answer(_ctx("judge"), _S(), "привет").accept is True


def test_judge_reject_returns_false(monkeypatch):
    from intent_guard.validation_result import ValidationResult
    import intent_guard.text_answer_judge as taj
    _rej = ValidationResult(ok=False, action="continue_work", trace_ok=False, report_ok=True,
                            trace_gaps=["more work needed"], missing_requirements=[],
                            report_mismatches=[], conflicts=[], feedback_for_agent="", coverage_score=0.0)
    monkeypatch.setattr(taj, "judge_text_answer", lambda ctx, state, text: _rej)
    assert should_return_text_answer(_ctx("judge"), _S(), "сделай отчёт").accept is False


def test_deny_returns_false_without_judge(monkeypatch):
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer(_ctx("deny"), _S(), "привет").accept is False
    assert judged["n"] == 0


def test_no_key_returns_false_without_judge(monkeypatch):
    """Автономка (CLI/server/swarm): нет ключа → строго, судья не зван."""
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer(_ctx(None), _S(), "привет").accept is False
    assert judged["n"] == 0


# --- gate: judge активен только при включённом finish-gate --------------------

def test_judge_without_chat_completions_is_strict(monkeypatch):
    """responses-режим: finish-gate выключен → судья не зван → строго."""
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer(_ctx("judge", backend="responses"), _S(), "привет").accept is False
    assert judged["n"] == 0


def test_judge_with_strict_validation_off_is_strict(monkeypatch):
    """strict_turn_finish_validation=false → судью не на чём строить → строго."""
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer(_ctx("judge", strict=False), _S(), "привет").accept is False
    assert judged["n"] == 0


def test_empty_text_bounces_in_all_modes(monkeypatch):
    """Пустой/пробельный текст — не ответ хода: bounce даже в always; судья не зван."""
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer(_ctx("allow"), _S(), "   ").accept is False
    assert should_return_text_answer(_ctx("judge"), _S(), "").accept is False
    assert judged["n"] == 0


# --- интеграция в run_agent ---------------------------------------------------

def _state():
    s = type("S", (), {})()
    s.messages = []
    s.steps = 0
    s.max_steps = 100
    s.current_user_intent = []
    s.user_intent_log = []
    s.flush_current_user_intent = lambda: None          # finish-accept флашит интент
    return s


def _run_ctx(mode):
    cd = {"backend_api_mode": "chat_completions", "console_trace": False,
          "no_tool_call_max_count": 3, "strict_turn_finish_validation": True}
    if mode is not None:
        cd["text_answer_mode"] = mode
    return type("C", (), {"config_dict": cd, "logger": None})()


def _text_msgs():
    return [{"role": "assistant", "type": "message", "content": "Привет! Чем помочь?"}]


def test_run_agent_allow_returns_text():
    """allow: текст-ответ становится ответом хода (реальное решение, без мока)."""
    import processing_responses as pr
    result = pr.run_agent(_text_msgs(), _state(), _run_ctx("allow"))
    assert result == "Привет! Чем помочь?"


def test_run_agent_judge_accept_returns_text(monkeypatch):
    """judge + accept: текст возвращается (gate включён, судья замокан)."""
    from types import SimpleNamespace
    import processing_responses as pr
    import intent_guard.text_answer_judge as taj
    monkeypatch.setattr(taj, "judge_text_answer", lambda ctx, state, text: SimpleNamespace(action="accept"))
    result = pr.run_agent(_text_msgs(), _state(), _run_ctx("judge"))
    assert result == "Привет! Чем помочь?"
