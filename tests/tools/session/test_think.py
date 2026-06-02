import json

from tools.session.think import Think


def test_think_success_single_field():
    data = json.loads(Think()(thoughts="Analyzing the problem..."))
    assert data["ok"] is True
    assert "Thinking added" in (data.get("message") or "")
    msg = (data.get("message") or "").lower()
    assert "thread memory" in msg
    assert "durable" in msg
    assert "decision" in msg
    assert "working_memory" not in msg
    assert "memory_files" not in msg
    assert data.get("error") is None


def test_think_success_multiple_fields():
    data = json.loads(
        Think()(
            thoughts="Plan: do X",
            critics="X might fail if Y",
            doubts="Uncertain about Z",
            reflection="Proceeding with X, will verify Y.",
        )
    )
    assert data["ok"] is True
    assert "Thinking added" in (data.get("message") or "")
    msg = (data.get("message") or "").lower()
    assert "thread memory" in msg
    assert "durable" in msg
    assert "decision" in msg
    assert "working_memory" not in msg
    assert "memory_files" not in msg
    assert data.get("error") is None


def test_think_invalid_all_empty():
    data = json.loads(Think()(thoughts="", critics="", doubts="", reflection=""))
    assert data["ok"] is False
    assert data.get("message") is None
    assert data.get("error") is not None
    assert data["error"]["type"] == "invalid_argument"
    assert "At least one" in data["error"]["message"]


def test_think_descriptions_are_tool_agnostic():
    desc = Think.tool_description()["description"]
    prompt = Think.prompt_description()
    combined = f"{desc}\n{prompt}".lower()
    assert "thread memory" in combined
    assert "working_memory" not in combined
    assert "memory_files" not in combined


def test_think_invalid_argument_type():
    bad = json.loads(Think()(thoughts=123))  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert bad.get("error") is not None
    assert bad["error"].get("field") == "thoughts"
