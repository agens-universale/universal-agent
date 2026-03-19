from tools.simple_tools.submit_solution import SubmitSolution


def test_submit_solution_success_and_normalize():
    tool = SubmitSolution()
    res = tool(task_report="\nDone.\r\nNext  \n")
    assert res["status"] == "submitted"
    assert res["task_report"] == "Done.\nNext"


def test_submit_solution_errors():
    tool = SubmitSolution()
    assert "must be a string" in tool(task_report=1)  # type: ignore[arg-type]
    assert "cannot be empty" in tool(task_report="   ")
    assert "too long" in tool(task_report="x" * 10001)
