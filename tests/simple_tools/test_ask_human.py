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


def test_tool_description_includes_meta_response_handling():
    """Agent sees tool_description in API spec."""
    desc = AskHuman.tool_description()["description"]
    assert "meta-question" in desc.lower()
    assert "delegation" in desc.lower()
    assert "confirm" in desc.lower()
    assert "ask_human" in desc.lower()
    assert "insufficient" in desc.lower()


def test_prompt_description_includes_meta_response_handling():
    """Full text kept for legacy tools_descr / future tool_help."""
    desc = AskHuman.prompt_description()
    assert "Handling user's response" in desc
    assert "four categories" in desc
    assert "Direct answer" in desc
    assert "Delegation to you" in desc
    assert "black box" in desc.lower()
    assert "Meta-question" in desc or "meta-question" in desc
    assert "Ambiguous" in desc
    assert "DO NOT" in desc and "advance" in desc.lower()
    assert "Re-call `ask_human`" in desc or "re-call ask_human" in desc.lower()
    assert "Pacing follows the user" in desc


def test_success_result_includes_hint(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda: "yes")
    out = json.loads(AskHuman()(question="Proceed?"))
    assert out["ok"] is True
    assert "hint" in out
    assert "insufficient" in out["hint"].lower()
    assert "delegation" in out["hint"].lower()
    assert "black box" in out["hint"].lower()
    assert "ask_human" in out["hint"].lower()
