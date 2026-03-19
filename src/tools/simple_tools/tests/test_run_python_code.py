import json

from tools.simple_tools.run_python_code import RunPythonCode, _sanitize_env


def test_run_python_success_non_zero_and_timeout():
    tool = RunPythonCode()
    ok = json.loads(tool(code="print('ok')"))
    assert ok["ok"] is True and ok["stdout"] == "ok"
    bad = json.loads(tool(code="import sys; sys.exit(3)"))
    assert bad["ok"] is False and bad["return_code"] == 3
    timeout = json.loads(tool(code="import time; time.sleep(2)", timeout_seconds=1))
    assert timeout["ok"] is False and timeout["timed_out"] is True


def test_run_python_validation_and_truncation():
    tool = RunPythonCode()
    assert json.loads(tool(code=""))["error"]["field"] == "code"
    assert json.loads(tool(code="print(1)", max_output_chars=0))["error"]["field"] == "max_output_chars"
    trunc = json.loads(tool(code="print('A'*100)", max_output_chars=10))
    assert trunc["truncated"] is True and len(trunc["stdout"]) == 10
    bad_cwd = json.loads(tool(code="print(1)", cwd=""))
    assert bad_cwd["ok"] is False and bad_cwd["error"]["field"] == "cwd"


def test_run_python_env_and_cwd(tmp_path):
    tool = RunPythonCode()
    code = "import os; print(os.getenv('A')); print(os.getenv('B')); print(os.getenv('PYTHONPATH'))"
    out = json.loads(tool(code=code, env={"A": 1, "B": None}, cwd=str(tmp_path)))
    assert out["ok"] is True
    assert "1" in out["stdout"]


def test_sanitize_env():
    env = _sanitize_env({"A": 1, "B": None})
    assert env["A"] == "1"
    assert env["B"] == ""
    assert env["PYTHONPATH"] == ""
