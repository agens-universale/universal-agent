"""TUI adaptive bounce: судья текст-ответа переиспользует finish-gate-судью."""


class _S:
    current_user_intent = []
    user_intent_log = []


def _ctx():
    return type("C", (), {"config_dict": {}, "logger": None})()


def test_judge_accepts_when_action_accept(monkeypatch):
    from intent_guard import text_answer_judge as taj
    monkeypatch.setattr(taj, "validate_final_report_vs_user_intent",
                        lambda **kw: type("R", (), {"action": "accept"})())
    assert taj.judge_text_answer(_ctx(), _S(), "Привет! Чем помочь?") is True


def test_judge_bounces_when_action_continue(monkeypatch):
    from intent_guard import text_answer_judge as taj
    monkeypatch.setattr(taj, "validate_final_report_vs_user_intent",
                        lambda **kw: type("R", (), {"action": "continue_work"})())
    assert taj.judge_text_answer(_ctx(), _S(), "Сейчас сделаю...") is False


def test_judge_bounces_on_validator_error(monkeypatch):
    from intent_guard import text_answer_judge as taj
    def boom(**kw):
        raise RuntimeError("validator down")
    monkeypatch.setattr(taj, "validate_final_report_vs_user_intent", boom)
    assert taj.judge_text_answer(_ctx(), _S(), "что-то") is False   # строгий fallback


def test_judge_passes_text_as_finish_arguments(monkeypatch):
    """Текст подаётся судье как finish_handoff_arguments (полный контекст — изнутри)."""
    from intent_guard import text_answer_judge as taj
    seen = {}
    def cap(**kw):
        seen.update(kw)
        return type("R", (), {"action": "accept"})()
    monkeypatch.setattr(taj, "validate_final_report_vs_user_intent", cap)
    taj.judge_text_answer(_ctx(), _S(), "мой текст-ответ")
    assert seen["finish_handoff_arguments"] == "мой текст-ответ"
