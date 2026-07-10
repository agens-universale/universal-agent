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
    assert "if only injected `reason`" in prompt.lower() or "treated as `thoughts`" in prompt.lower()


def test_think_invalid_argument_type():
    bad = json.loads(Think()(thoughts=123))  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert bad.get("error") is not None
    assert bad["error"].get("field") == "thoughts"


def test_prepare_args_before_strip_remaps_reason_to_thoughts():
    args = Think.prepare_args_before_strip(
        {"reason": "long reasoning about the next step"}
    )
    assert args["thoughts"] == "long reasoning about the next step"
    assert args["reason"] == "long reasoning about the next step"


def test_prepare_args_before_strip_keeps_explicit_thoughts():
    args = Think.prepare_args_before_strip(
        {"reason": "injected why", "thoughts": "explicit thoughts"}
    )
    assert args["thoughts"] == "explicit thoughts"


def test_prepare_args_before_strip_ignores_blank_reason():
    args = Think.prepare_args_before_strip({"reason": "  ", "critics": ""})
    assert "thoughts" not in args


def test_prepare_args_before_strip_skips_when_any_think_field_set():
    args = Think.prepare_args_before_strip(
        {"reason": "injected why", "critics": "a critique"}
    )
    assert "thoughts" not in args
    assert args["critics"] == "a critique"
