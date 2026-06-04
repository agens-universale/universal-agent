import json

from tools.turn.submit_solution import SubmitSolution

VALID_TYPES = (
    "full_solution",
    "partial_solution",
    "direct_answer",
    "couldnt_solve",
    "need_additional_info",
    "abandoned",
)


def test_turn_finish_success_full_solution():
    tool = SubmitSolution()
    res = json.loads(
        tool(
            solution_type="full_solution",
            task_report="Implemented auth. Created /login. Tests pass.",
            handoff='{"result":"ok"}',
        )
    )
    assert res["ok"] is True
    assert res["status"] == "submitted"
    assert res["solution_type"] == "full_solution"
    assert res["task_report"] == "Implemented auth. Created /login. Tests pass."
    assert "comment" not in res or res.get("comment") == ""


def test_turn_finish_success_with_comment():
    tool = SubmitSolution()
    res = json.loads(
        tool(
            solution_type="partial_solution",
            task_report="Fixed X and Y.",
            comment="Z remains; recommend manual review.",
            handoff='{"progress":"partial"}',
        )
    )
    assert res["ok"] is True
    assert res["solution_type"] == "partial_solution"
    assert res["task_report"] == "Fixed X and Y."
    assert res["comment"] == "Z remains; recommend manual review."


def test_turn_finish_normalize():
    tool = SubmitSolution()
    res = json.loads(
        tool(
            solution_type="full_solution",
            task_report="\nDone.\r\nNext  \n",
            handoff='{"normalized":true}',
        )
    )
    assert res["ok"] is True
    assert res["task_report"] == "Done.\nNext"


def test_turn_finish_all_types():
    tool = SubmitSolution()
    for st in VALID_TYPES:
        res = json.loads(
            tool(solution_type=st, task_report=f"Report for {st}", handoff='{"t":"ok"}')
        )
        assert res["ok"] is True, f"solution_type={st}"
        assert res["solution_type"] == st


def test_turn_finish_invalid_solution_type():
    tool = SubmitSolution()
    bad = json.loads(
        tool(solution_type="invalid", task_report="Some report", handoff='{"x":1}')  # type: ignore[arg-type]
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "solution_type"


def test_turn_finish_invalid_task_report_type():
    tool = SubmitSolution()
    bad = json.loads(tool(solution_type="full_solution", task_report=1, handoff='{"x":1}'))  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert bad["error"]["field"] == "task_report"


def test_turn_finish_empty_task_report():
    tool = SubmitSolution()
    bad = json.loads(
        tool(solution_type="full_solution", task_report="   ", handoff='{"x":1}')
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "task_report"


def test_turn_finish_task_report_too_long():
    tool = SubmitSolution()
    bad = json.loads(
        tool(solution_type="full_solution", task_report="x" * 10001, handoff='{"x":1}')
    )
    assert bad["ok"] is False
    assert bad["error"]["type"] == "limit_exceeded"


def test_turn_finish_invalid_comment_type():
    tool = SubmitSolution()
    bad = json.loads(
        tool(
            solution_type="full_solution",
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


def test_turn_finish_rejects_invalid_handoff_json():
    tool = SubmitSolution()
    bad = json.loads(
        tool(solution_type="full_solution", task_report="Done", handoff='{"x": }')
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "handoff"


def test_turn_finish_rejects_non_object_handoff_json():
    tool = SubmitSolution()
    bad = json.loads(
        tool(solution_type="full_solution", task_report="Done", handoff='["x"]')
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "handoff"
