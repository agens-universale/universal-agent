import builtins

import pytest

from tools.simple_tools.ask_human import AskHuman


def test_ask_human_multiline(monkeypatch):
    answers = iter(["first", "second", ""])
    monkeypatch.setattr(builtins, "input", lambda: next(answers))
    out = AskHuman()(question="Q?")
    assert out["response"] == "first\nsecond"


def test_ask_human_empty_and_interrupt(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda: "")
    out = AskHuman()(question="Q?")
    assert out["response"] == "(empty response)"

    def _raise():
        raise KeyboardInterrupt()

    monkeypatch.setattr(builtins, "input", _raise)
    out2 = AskHuman()(question="Q?")
    assert out2["response"] == "(user interrupted)"


def test_ask_human_validation():
    tool = AskHuman()
    with pytest.raises(ValueError):
        tool(question="")
    with pytest.raises(ValueError):
        tool(question=1)  # type: ignore[arg-type]
