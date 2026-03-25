import json

from tools.simple_tools.submit_solution import SubmitSolution

VALID_TYPES = (
    "full_solution",
    "partial_solution",
    "answer_question",
    "couldnt_solve",
    "need_additional_info",
    "abandoned",
)


def test_submit_solution_success_full_solution():
    tool = SubmitSolution()
    res = json.loads(
        tool(
            solution_type="full_solution",
            task_report="Implemented auth. Created /login. Tests pass.",
        )
    )
    assert res["ok"] is True
    assert res["status"] == "submitted"
    assert res["solution_type"] == "full_solution"
    assert res["task_report"] == "Implemented auth. Created /login. Tests pass."
    assert "comment" not in res or res.get("comment") == ""


def test_submit_solution_success_with_comment():
    tool = SubmitSolution()
    res = json.loads(
        tool(
            solution_type="partial_solution",
            task_report="Fixed X and Y.",
            comment="Z remains; recommend manual review.",
        )
    )
    assert res["ok"] is True
    assert res["solution_type"] == "partial_solution"
    assert res["task_report"] == "Fixed X and Y."
    assert res["comment"] == "Z remains; recommend manual review."


def test_submit_solution_normalize():
    tool = SubmitSolution()
    res = json.loads(
        tool(
            solution_type="full_solution",
            task_report="\nDone.\r\nNext  \n",
        )
    )
    assert res["ok"] is True
    assert res["task_report"] == "Done.\nNext"


def test_submit_solution_all_types():
    tool = SubmitSolution()
    for st in VALID_TYPES:
        res = json.loads(
            tool(solution_type=st, task_report=f"Report for {st}")
        )
        assert res["ok"] is True, f"solution_type={st}"
        assert res["solution_type"] == st


def test_submit_solution_invalid_solution_type():
    tool = SubmitSolution()
    bad = json.loads(
        tool(solution_type="invalid", task_report="Some report")  # type: ignore[arg-type]
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "solution_type"


def test_submit_solution_invalid_task_report_type():
    tool = SubmitSolution()
    bad = json.loads(tool(solution_type="full_solution", task_report=1))  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert bad["error"]["field"] == "task_report"


def test_submit_solution_empty_task_report():
    tool = SubmitSolution()
    bad = json.loads(
        tool(solution_type="full_solution", task_report="   ")
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "task_report"


def test_submit_solution_task_report_too_long():
    tool = SubmitSolution()
    bad = json.loads(
        tool(solution_type="full_solution", task_report="x" * 10001)
    )
    assert bad["ok"] is False
    assert bad["error"]["type"] == "limit_exceeded"


def test_submit_solution_invalid_comment_type():
    tool = SubmitSolution()
    bad = json.loads(
        tool(
            solution_type="full_solution",
            task_report="Done",
            comment=123,  # type: ignore[arg-type]
        )
    )
    assert bad["ok"] is False
    assert bad["error"]["field"] == "comment"
