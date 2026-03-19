import json

from tools.simple_tools.submit_solution import SubmitSolution


def test_submit_solution_success_and_normalize():
    tool = SubmitSolution()
    res = json.loads(tool(task_report="\nDone.\r\nNext  \n"))
    assert res["ok"] is True
    assert res["status"] == "submitted"
    assert res["task_report"] == "Done.\nNext"


def test_submit_solution_errors():
    tool = SubmitSolution()
    bad_type = json.loads(tool(task_report=1))  # type: ignore[arg-type]
    assert bad_type["ok"] is False and bad_type["error"]["field"] == "task_report"
    empty = json.loads(tool(task_report="   "))
    assert empty["ok"] is False and empty["error"]["field"] == "task_report"
    too_long = json.loads(tool(task_report="x" * 10001))
    assert too_long["ok"] is False and too_long["error"]["type"] == "limit_exceeded"
