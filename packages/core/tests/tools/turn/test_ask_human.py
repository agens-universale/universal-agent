import builtins
import json

import pytest

from gc_core.tools.turn.ask_human import AskHuman


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


def test_tool_description_focuses_on_asking_not_reply_taxonomy():
    """API spec: how to ask; reply handling is in the result hint only."""
    desc = AskHuman.tool_description()["description"]
    assert "hint" in desc.lower()
    assert "options" in desc.lower() or "a/b/c" in desc.lower()
    assert "delegation" not in desc.lower()
    assert "meta-question" not in desc.lower()
    assert len(desc) < 850


def test_prompt_description_is_full_reference_doc():
    """Legacy tools_descr / future tool_help — aligned with tool_description + hint."""
    desc = AskHuman.prompt_description()
    assert "hint" in desc.lower()
    assert "tool_description" in desc.lower()
    assert "four categories" in desc
    assert "Direct answer" in desc
    assert "Delegation to you" in desc
    assert "black box" in desc.lower()
    assert "Meta-question" in desc or "meta-question" in desc
    assert "4096" in desc
    assert "long material" in desc.lower() or "pacing follows the user" in desc.lower()
    assert "Pacing follows the user" in desc


def test_success_result_includes_hint(monkeypatch):
    answers = iter(["yes", ""])
    monkeypatch.setattr(builtins, "input", lambda: next(answers))
    out = json.loads(AskHuman()(question="Proceed?"))
    assert out["ok"] is True
    assert "hint" in out
    assert "insufficient" in out["hint"].lower()
    assert "delegation" in out["hint"].lower()
    assert "black box" in out["hint"].lower()
    assert "ask_human" in out["hint"].lower()
