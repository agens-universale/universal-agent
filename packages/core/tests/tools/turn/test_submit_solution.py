import json

from gc_core.tools.turn.submit_solution import SubmitSolution
from gc_testkit.helpers.tool_ctx import CallableTool

VALID_TYPES = (
    "full_solution",
    "partial_solution",
    "direct_answer",
    "couldnt_solve",
    "need_additional_info",
    "abandoned",
)


def test_turn_finish_success_full_solution():
    tool = CallableTool(SubmitSolution())
    res = json.loads(
        tool(
            solution_type="full_solution",
            task="Implement auth",
            task_report="Implemented auth. Created /login. Tests pass.",
            handoff='{"result":"ok"}',
        )
    )
    assert res == {
        "tool": "turn_finish",
        "ok": True,
        "status": "submitted",
    }


def test_turn_finish_success_with_comment():
    tool = CallableTool(SubmitSolution())
    res = json.loads(
        tool(
            solution_type="partial_solution",
            task="Fix X, Y, and Z",
            task_report="Fixed X and Y.",
            comment="Z remains; recommend manual review.",
            handoff='{"progress":"partial"}',
        )
    )
    assert res == {
        "tool": "turn_finish",
        "ok": True,
        "status": "submitted",
    }


def test_turn_finish_normalize():
    tool = CallableTool(SubmitSolution())
def test_turn_finish_success_does_not_duplicate_normalized_arguments():
    tool = CallableTool(SubmitSolution())
    res = json.loads(
        tool(
            solution_type="full_solution",
            task="\nImplement auth\r\nsafely  \n",
            task_report="\nDone.\r\nNext  \n",
            handoff='{"normalized":true}',
        )
    )
    assert res == {
        "tool": "turn_finish",
        "ok": True,
        "status": "submitted",
    }


def test_turn_finish_all_types():
    tool = CallableTool(SubmitSolution())
    for st in VALID_TYPES:
        res = json.loads(
            tool(
                solution_type=st,
                task=f"Task for {st}",
                task_report=f"Report for {st}",
                handoff='{"t":"ok"}',
            )
        )
        assert res == {
            "tool": "turn_finish",
            "ok": True,
            "status": "submitted",
        }, f"solution_type={st}"


def test_turn_finish_invalid_solution_type():
    tool = CallableTool(SubmitSolution())
    bad = json.loads(
        tool(
            solution_type="invalid",
            task="Test invalid type",
            task_report="Some report",
            handoff='{"x":1}',
        )  # type: ignore[arg-type]
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "solution_type"


def test_turn_finish_invalid_task_report_type():
    tool = CallableTool(SubmitSolution())
    bad = json.loads(
        tool(
            solution_type="full_solution",
            task="Test report",
            task_report=1,
            handoff='{"x":1}',
        )
    )  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert bad["error"]["field"] == "task_report"


def test_turn_finish_empty_task_report():
    tool = CallableTool(SubmitSolution())
    bad = json.loads(
        tool(
            solution_type="full_solution",
            task="Test report",
            task_report="   ",
            handoff='{"x":1}',
        )
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "task_report"


def test_turn_finish_task_report_too_long():
    tool = CallableTool(SubmitSolution())
    bad = json.loads(
        tool(
            solution_type="full_solution",
            task="Test report",
            task_report="x" * 10001,
            handoff='{"x":1}',
        )
    )
    assert bad["ok"] is False
    assert bad["error"]["type"] == "limit_exceeded"
    assert bad["error"]["message"] == "task_report exceeds 10000 chars, got 10001"


def test_turn_finish_task_report_schema_names_char_budget():
    spec = SubmitSolution.tool_description()
    desc = spec["parameters"]["properties"]["task_report"]["description"]
    assert "10000" in desc
    prompt = SubmitSolution.prompt_description()
    assert "10000" in prompt
    required = spec["parameters"]["required"]
    assert "task" in required


def test_turn_finish_invalid_comment_type():
    tool = CallableTool(SubmitSolution())
    bad = json.loads(
        tool(
            solution_type="full_solution",
            task="Test comment",
            task_report="Done",
            comment=123,  # type: ignore[arg-type]
            handoff='{"x":1}',
        )
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "comment"


def test_turn_finish_handoff_schema_is_strict_friendly():
    spec = SubmitSolution.tool_description()
    handoff = spec["parameters"]["properties"]["handoff"]
    assert handoff["type"] == "string"


def test_turn_finish_schema_declares_task():
    spec = SubmitSolution.tool_description()
    props = spec["parameters"]["properties"]
    assert props["task"]["type"] == "string"
    assert "task" in spec["parameters"]["required"]
    prompt = SubmitSolution.prompt_description()
    assert '"task": "string (required)"' in prompt


def test_turn_finish_task_is_optional_passthrough():
    """task — подсказка модели: отсутствие/пустота не отклоняют ход."""
    tool = CallableTool(SubmitSolution())
    out = json.loads(
        tool(solution_type="full_solution", task_report="Done", handoff='{"x":1}')
    )
    assert out["ok"] is True
    assert "task" not in out
    out = json.loads(
        tool(solution_type="full_solution", task="  ", task_report="Done", handoff='{"x":1}')
    )
    assert out["ok"] is True
    assert "task" not in out


def test_turn_finish_rejects_invalid_handoff_json():
    tool = CallableTool(SubmitSolution())
    bad = json.loads(
        tool(
            solution_type="full_solution",
            task="Test handoff",
            task_report="Done",
            handoff='{"x": }',
        )
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "handoff"


def test_turn_finish_rejects_non_object_handoff_json():
    tool = CallableTool(SubmitSolution())
    bad = json.loads(
        tool(
            solution_type="full_solution",
            task="Test handoff",
            task_report="Done",
            handoff='["x"]',
        )
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "handoff"
