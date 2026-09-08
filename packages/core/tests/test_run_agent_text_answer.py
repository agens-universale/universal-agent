"""Решение «принять текст-ответ?» по режиму text_answer_mode (юниты) + интеграция
в run_agent (текст-ответ возвращается как ответ хода). prose+tool не затрагивается:
хук живёт только в ветке mode == text_response. judge активен, только если доступна
машинерия судейства (backend_api_mode == chat_completions) — от тоггла /gate
(strict_turn_finish_validation, finish-gate) НЕ зависит."""
from gc_core.intent_guard.text_answer_judge import ashould_return_text_answer
from gc_testkit import run_coro


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
    from gc_core.intent_guard import text_answer_judge as taj
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("allow"), _S(), "привет")).accept is True
    assert judged["n"] == 0                              # allow → судья не зван


def test_judge_accept_returns_true(monkeypatch):
    from types import SimpleNamespace
    from gc_core.intent_guard import text_answer_judge as taj
    async def _judge(ctx, state, text):
        return SimpleNamespace(action="accept")
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("judge"), _S(), "привет")).accept is True


def test_judge_reject_returns_false(monkeypatch):
    from gc_core.intent_guard.validation_result import ValidationResult
    from gc_core.intent_guard import text_answer_judge as taj
    _rej = ValidationResult(ok=False, action="continue_work", trace_ok=False, report_ok=True,
                            trace_gaps=["more work needed"], missing_requirements=[],
                            report_mismatches=[], conflicts=[], feedback_for_agent="", coverage_score=0.0)
    async def _judge(ctx, state, text):
        return _rej
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("judge"), _S(), "сделай отчёт")).accept is False


def test_deny_returns_false_without_judge(monkeypatch):
    from gc_core.intent_guard import text_answer_judge as taj
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("deny"), _S(), "привет")).accept is False
    assert judged["n"] == 0


def test_no_key_returns_false_without_judge(monkeypatch):
    """Автономка (CLI/server/swarm): нет ключа → строго, судья не зван."""
    from gc_core.intent_guard import text_answer_judge as taj
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx(None), _S(), "привет")).accept is False
    assert judged["n"] == 0


# --- gate: judge зависит только от инфраструктуры (chat_completions), --------
# --- НЕ от тоггла /gate (strict_turn_finish_validation) ------------------------

def test_judge_gate_on_calls_judge(monkeypatch):
    """(a) gate=on (strict=True) + judge → судья зовётся."""
    from gc_core.intent_guard import text_answer_judge as taj
    from types import SimpleNamespace
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return SimpleNamespace(action="accept")
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(ashould_return_text_answer(_ctx("judge", strict=True), _S(), "привет"))
    assert judged["n"] == 1 and d.accept is True


def test_judge_gate_off_still_calls_judge(monkeypatch):
    """(b) gate=off (strict=False) + judge → судья ВСЁ РАВНО зовётся (/gate не
    должен молча деградировать judge в deny; было: FinishGateGuard.enabled склеивал
    strict_turn_finish_validation с backend_api_mode)."""
    from gc_core.intent_guard import text_answer_judge as taj
    from types import SimpleNamespace
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return SimpleNamespace(action="accept")
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    d = run_coro(ashould_return_text_answer(_ctx("judge", strict=False), _S(), "привет"))
    assert judged["n"] == 1 and d.accept is True


def test_judge_without_chat_completions_is_strict(monkeypatch):
    """(e) backend != chat_completions + judge → deny без судьи, при gate=on."""
    from gc_core.intent_guard import text_answer_judge as taj
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("judge", backend="responses", strict=True), _S(), "привет")).accept is False
    assert judged["n"] == 0


def test_judge_without_chat_completions_is_strict_even_with_gate_off(monkeypatch):
    """(e) backend != chat_completions + judge → deny без судьи, тоже при gate=off
    (инфраструктурное условие не подменяется пользовательской ручкой)."""
    from gc_core.intent_guard import text_answer_judge as taj
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("judge", backend="responses", strict=False), _S(), "привет")).accept is False
    assert judged["n"] == 0


def test_allow_accepts_without_judge_regardless_of_gate(monkeypatch):
    """(c) allow → accept без судьи при любом gate."""
    from gc_core.intent_guard import text_answer_judge as taj
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("allow", strict=True), _S(), "привет")).accept is True
    assert run_coro(ashould_return_text_answer(_ctx("allow", strict=False), _S(), "привет")).accept is True
    assert judged["n"] == 0


def test_deny_rejects_regardless_of_gate(monkeypatch):
    """(d) deny/нет ключа → reject при любом gate."""
    from gc_core.intent_guard import text_answer_judge as taj
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("deny", strict=True), _S(), "привет")).accept is False
    assert run_coro(ashould_return_text_answer(_ctx("deny", strict=False), _S(), "привет")).accept is False
    assert run_coro(ashould_return_text_answer(_ctx(None, strict=True), _S(), "привет")).accept is False
    assert run_coro(ashould_return_text_answer(_ctx(None, strict=False), _S(), "привет")).accept is False
    assert judged["n"] == 0


def test_empty_text_bounces_in_all_modes(monkeypatch):
    """Пустой/пробельный текст — не ответ хода: bounce даже в always; судья не зван."""
    from gc_core.intent_guard import text_answer_judge as taj
    judged = {"n": 0}
    async def _judge(ctx, state, text):
        judged["n"] += 1
        return True
    monkeypatch.setattr(taj, "ajudge_text_answer", _judge)
    assert run_coro(ashould_return_text_answer(_ctx("allow"), _S(), "   ")).accept is False
    assert run_coro(ashould_return_text_answer(_ctx("judge"), _S(), "")).accept is False
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
