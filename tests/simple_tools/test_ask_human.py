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


def test_prompt_description_includes_meta_response_handling():
    """
    REGRESSION: the agent-facing prompt_description must instruct on
    meta-question handling. Without this, the agent treats every user
    reply as a direct answer and advances its methodology prematurely
    even when the user is asking back (typical in brainstorming).
    """
    desc = AskHuman.prompt_description()
    # The "Handling user's response" section must exist.
    assert "Handling user's response" in desc
    # Three categories must be mentioned.
    assert "Direct answer" in desc
    assert "Meta-question" in desc or "meta-question" in desc
    assert "Ambiguous" in desc
    # Imperative not to advance reasoning is critical.
    assert "DO NOT" in desc and "advance" in desc.lower()
    # Re-ask via the same tool must be suggested.
    assert "Re-call `ask_human`" in desc or "re-call ask_human" in desc.lower()
    # Pacing follows the user, not the agent's internal plan.
    assert "Pacing follows the user" in desc
