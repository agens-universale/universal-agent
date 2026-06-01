import json
import sys

from tools.exec.execute_command import ExecuteCommand


def test_execute_success_and_non_zero():
    tool = ExecuteCommand()
    ok = json.loads(tool(command=f'"{sys.executable}" -c "print(123)"'))
    assert ok["ok"] is True and "123" in ok["stdout"]
    bad = json.loads(tool(command=f'"{sys.executable}" -c "import sys; sys.exit(7)"'))
    assert bad["ok"] is False and bad["exit_code"] == 7


def test_execute_env_timeout_and_truncation(tmp_path):
    tool = ExecuteCommand()
    env = json.loads(
        tool(
            command=f'"{sys.executable}" -c "import os; print(os.getenv(\'X\'))"',
            env_vars=[{"name": "X", "value": "v"}],
        )
    )
    assert env["ok"] is True and env["stdout"] == "v"
    timeout = json.loads(tool(command=f'"{sys.executable}" -c "import time; time.sleep(2)"', timeout_seconds=1))
    assert timeout["ok"] is False and timeout["timed_out"] is True
    trunc = json.loads(tool(command=f'"{sys.executable}" -c "print(\'A\'*100)"', max_output_chars=10))
    assert trunc["truncated"] is True and len(trunc["stdout"]) == 10


def test_execute_validation(tmp_path):
    tool = ExecuteCommand()
    assert json.loads(tool(command=""))["error"]["field"] == "command"
    assert json.loads(tool(command="echo 1", timeout_seconds=0))["error"]["field"] == "timeout_seconds"
    assert json.loads(tool(command="echo 1", cwd=str(tmp_path / "none")))["error"]["field"] == "cwd"


def test_execute_cwd_and_env_conversion(tmp_path):
    tool = ExecuteCommand()
    marker = tmp_path / "marker.txt"
    marker.write_text("ok", encoding="utf-8")
    cmd = f'"{sys.executable}" -c "from pathlib import Path; print(Path(\'marker.txt\').exists())"'
    in_cwd = json.loads(tool(command=cmd, cwd=str(tmp_path)))
    assert in_cwd["ok"] is True and in_cwd["stdout"] == "True"

    env_casted = json.loads(
        tool(
            command=f'"{sys.executable}" -c "import os; print(os.getenv(\'K\')); print(os.getenv(\'NONE_V\'))"',
            env_vars=[{"name": "K", "value": "123"}, {"name": "NONE_V", "value": ""}],
        )
    )
    assert env_casted["ok"] is True
    assert "123" in env_casted["stdout"]


def test_execute_env_vars_validation():
    tool = ExecuteCommand()
    bad_not_list = json.loads(tool(command="echo 1", env_vars="X=1"))  # type: ignore[arg-type]
    assert bad_not_list["ok"] is False
    assert bad_not_list["error"]["field"] == "env_vars"

    bad_item = json.loads(tool(command="echo 1", env_vars=[{"name": "", "value": "1"}]))
    assert bad_item["ok"] is False
    assert bad_item["error"]["field"] == "env_vars[0].name"

    bad_value = json.loads(tool(command="echo 1", env_vars=[{"name": "X", "value": 1}]))  # type: ignore[list-item]
    assert bad_value["ok"] is False
    assert bad_value["error"]["field"] == "env_vars[0].value"
