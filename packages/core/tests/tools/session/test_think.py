import json

from gc_core.tools.session.think import Think
from gc_testkit.helpers.tool_ctx import CallableTool


def test_think_success_single_field():
    data = json.loads(
        CallableTool(Think())(thoughts="Analyzing the problem...")
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


def test_think_success_multiple_fields():
    data = json.loads(
        CallableTool(Think())(
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
    data = json.loads(
        CallableTool(Think())(
            thoughts="", critics="", doubts="", reflection=""
        )
    )
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
    assert (
        "if only injected `reason`" in prompt.lower()
        or "treated as `thoughts`" in prompt.lower()
    )


def test_think_invalid_argument_type():
    bad = json.loads(CallableTool(Think())(thoughts=123))  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert bad.get("error") is not None
    assert bad["error"].get("field") == "thoughts"


def test_prepare_folds_every_key_into_thoughts_known_first():
    """Всё, что модель передала, — мысль: четыре поля по порядку, затем
    любые другие ключи; лишних ключей после хука нет."""
    args = Think.prepare_args_before_strip(
        {"plan": "п", "reflection": "в", "thoughts": "м", "critics": " "}
    )
    assert args == {"thoughts": "м\n\nв\n\nп"}


def test_prepare_takes_values_of_any_type_as_text():
    args = Think.prepare_args_before_strip(
        {"steps": ["а", "б"], "n": 3, "thoughts": None}
    )
    assert args == {"thoughts": '["а", "б"]\n\n3'}


def test_prepare_uses_injected_field_only_when_nothing_else():
    reason = {"reason": "почему вызов"}
    only = Think.prepare_args_before_strip(reason, injected_fields={"reason"})
    both = Think.prepare_args_before_strip(
        {**reason, "doubts": "д"}, injected_fields={"reason"}
    )
    assert only == {"reason": "почему вызов", "thoughts": "почему вызов"}
    assert both == {"reason": "почему вызов", "thoughts": "д"}


def test_prepare_blank_call_gives_no_thoughts():
    args = Think.prepare_args_before_strip(
        {"reason": "  ", "critics": "", "x": []}, injected_fields={"reason"}
    )
    assert args == {"reason": "  "}
