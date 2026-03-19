import builtins
import json

import pytest

from tools.simple_tools.ask_human import AskHuman


def test_ask_human_multiline(monkeypatch):
    answers = iter(["first", "second", ""])
    monkeypatch.setattr(builtins, "input", lambda: next(answers))
    out = json.loads(AskHuman()(question="Q?"))
    assert out["ok"] is True
    assert out["response"] == "first\nsecond"


def test_ask_human_empty_and_interrupt(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda: "")
    out = json.loads(AskHuman()(question="Q?"))
    assert out["ok"] is True
    assert out["response"] == "(empty response)"

    def _raise():
        raise KeyboardInterrupt()

    monkeypatch.setattr(builtins, "input", _raise)
    out2 = json.loads(AskHuman()(question="Q?"))
    assert out2["ok"] is False
    assert out2["response"] == "(user interrupted)"


def test_ask_human_validation():
    tool = AskHuman()
    empty = json.loads(tool(question=""))
    assert empty["ok"] is False and empty["error"]["field"] == "question"
    bad_type = json.loads(tool(question=1))  # type: ignore[arg-type]
    assert bad_type["ok"] is False and bad_type["error"]["field"] == "question"
