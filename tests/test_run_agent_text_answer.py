"""Решение «принять текст-ответ?» по режиму text_answer_mode (юниты) + интеграция
в run_agent (текст-ответ возвращается как ответ хода). prose+tool не затрагивается:
хук живёт только в ветке mode == text_response."""
from intent_guard.text_answer_judge import should_return_text_answer


class _S:
    current_user_intent = []
    user_intent_log = []


def _ctx():
    return type("C", (), {"config_dict": {}, "logger": None})()


# --- решение по режиму (изолированно) -----------------------------------------

def test_always_returns_true_without_judge(monkeypatch):
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer({"text_answer_mode": "always"}, _ctx(), _S(), "привет") is True
    assert judged["n"] == 0                              # always → судья не зван


def test_judge_accept_returns_true(monkeypatch):
    import intent_guard.text_answer_judge as taj
    monkeypatch.setattr(taj, "judge_text_answer", lambda ctx, state, text: True)
    assert should_return_text_answer({"text_answer_mode": "judge"}, _ctx(), _S(), "привет") is True


def test_judge_reject_returns_false(monkeypatch):
    import intent_guard.text_answer_judge as taj
    monkeypatch.setattr(taj, "judge_text_answer", lambda ctx, state, text: False)
    assert should_return_text_answer({"text_answer_mode": "judge"}, _ctx(), _S(), "сделай отчёт") is False


def test_strict_returns_false_without_judge(monkeypatch):
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer({"text_answer_mode": "strict"}, _ctx(), _S(), "привет") is False
    assert judged["n"] == 0


def test_no_key_returns_false_without_judge(monkeypatch):
    """Автономка (CLI/server/swarm): нет ключа → строго, судья не зван."""
    import intent_guard.text_answer_judge as taj
    judged = {"n": 0}
    monkeypatch.setattr(taj, "judge_text_answer",
                        lambda ctx, state, text: judged.__setitem__("n", judged["n"] + 1) or True)
    assert should_return_text_answer({}, _ctx(), _S(), "привет") is False
    assert judged["n"] == 0


# --- интеграция в run_agent ---------------------------------------------------

def _state():
    s = type("S", (), {})()
    s.messages = []
    s.steps = 0
    s.max_steps = 100
    s.current_user_intent = []
    s.user_intent_log = []
    return s


def _run_ctx(mode):
    cd = {"backend_api_mode": "chat_completions", "console_trace": False,
          "no_tool_call_max_count": 3}
    if mode is not None:
        cd["text_answer_mode"] = mode
    return type("C", (), {"config_dict": cd, "logger": None})()


def _text_msgs():
    return [{"role": "assistant", "type": "message", "content": "Привет! Чем помочь?"}]


def test_run_agent_always_returns_text():
    """always: текст-ответ становится ответом хода (реальное решение, без мока)."""
    import processing_responses as pr
    result = pr.run_agent(_text_msgs(), _state(), _run_ctx("always"))
    assert result == "Привет! Чем помочь?"


def test_run_agent_judge_accept_returns_text(monkeypatch):
    """judge + accept: текст возвращается (через реальный should_return_text_answer)."""
    import processing_responses as pr
    import intent_guard.text_answer_judge as taj
    monkeypatch.setattr(taj, "judge_text_answer", lambda ctx, state, text: True)
    result = pr.run_agent(_text_msgs(), _state(), _run_ctx("judge"))
    assert result == "Привет! Чем помочь?"
