import json

from tools.simple_tools.think import Think


def test_think_success_single_field():
    data = json.loads(Think()(thoughts="Analyzing the problem..."))
    assert data["ok"] is True
    assert "Thinking added" in (data.get("message") or "")
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
    assert data.get("error") is None


def test_think_invalid_all_empty():
    data = json.loads(Think()(thoughts="", critics="", doubts="", reflection=""))
    assert data["ok"] is False
    assert data.get("message") is None
    assert data.get("error") is not None
    assert data["error"]["type"] == "invalid_argument"
    assert "At least one" in data["error"]["message"]


def test_think_invalid_argument_type():
    bad = json.loads(Think()(thoughts=123))  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert bad.get("error") is not None
    assert bad["error"].get("field") == "thoughts"
