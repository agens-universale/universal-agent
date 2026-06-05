import json
import os
import stat

import pytest

from tools.exec.run_python_code import RunPythonCode, _sanitize_env


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
    out = json.loads(
        tool(
            code=code,
            env_vars=[{"name": "A", "value": "1"}, {"name": "B", "value": ""}],
            cwd=str(tmp_path),
        )
    )
    assert out["ok"] is True
    assert "1" in out["stdout"]


def test_run_python_env_vars_validation(tmp_path):
    tool = RunPythonCode()
    bad_not_list = json.loads(tool(code="print(1)", env_vars="A=1"))  # type: ignore[arg-type]
    assert bad_not_list["ok"] is False
    assert bad_not_list["error"]["field"] == "env_vars"

    bad_name = json.loads(tool(code="print(1)", env_vars=[{"name": "", "value": "1"}]))
    assert bad_name["ok"] is False
    assert bad_name["error"]["field"] == "env_vars[0].name"

    bad_value = json.loads(tool(code="print(1)", env_vars=[{"name": "A", "value": 1}]))  # type: ignore[list-item]
    assert bad_value["ok"] is False
    assert bad_value["error"]["field"] == "env_vars[0].value"


def test_sanitize_env():
    env = _sanitize_env({"A": 1, "B": None})
    assert env["A"] == "1"
    assert env["B"] == ""
    assert env["PYTHONPATH"] == ""


def _allow_config(project_dir):
    return {
        "launch_root": project_dir,
        "command_moderation": {"mode": "allow"},
        "write_guard": {"enabled": True, "extra_paths": []},
    }


def test_run_python_default_cwd_is_launch_root(tmp_path, monkeypatch):
    work = tmp_path / "somewhere_else"
    work.mkdir()
    monkeypatch.chdir(work)
    project = tmp_path / "project"
    project.mkdir()
    tool = RunPythonCode(config_dict=_allow_config(str(project)))
    marker = project / "from_root.txt"
    r = json.loads(tool(code=f"from pathlib import Path; Path({str(marker.name)!r}).write_text('x')"))
    assert r["ok"] is True
    assert marker.read_text() == "x"


def test_run_python_cwd_invalid_returns_allowed_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extra = tmp_path / "extra_write"
    extra.mkdir()
    tool = RunPythonCode(
        config_dict={
            "launch_root": str(workspace),
            "command_moderation": {"mode": "allow"},
            "write_guard": {"enabled": True, "extra_paths": [str(extra)]},
        }
    )
    missing = str(workspace / "nope_missing")
    r = json.loads(tool(code="print(1)", cwd=missing))
    assert r["ok"] is False
    assert r["error"]["type"] == "cwd_invalid"
    assert r["error"]["cwd"] == os.path.abspath(missing)
    paths = set(r["error"]["allowed_write_paths"])
    assert os.path.abspath(str(workspace)) in paths
    assert os.path.abspath(str(extra)) in paths


@pytest.mark.skipif(os.name == "nt", reason="directory writability via chmod is POSIX-oriented here")
def test_run_python_cwd_invalid_omits_non_writable_launch_root(tmp_path):
    ro_launch = tmp_path / "ro_launch"
    ro_launch.mkdir()
    old_mode = stat.S_IMODE(ro_launch.stat().st_mode)
    try:
        ro_launch.chmod(0o555)
        extra = tmp_path / "writable_extra"
        extra.mkdir()
        tool = RunPythonCode(
            config_dict={
                "launch_root": str(ro_launch),
                "command_moderation": {"mode": "allow"},
                "write_guard": {"enabled": True, "extra_paths": [str(extra)]},
            }
        )
        missing = str(ro_launch / "no_such_subdir")
        r = json.loads(tool(code="print(1)", cwd=missing))
        assert r["ok"] is False
        paths = r["error"]["allowed_write_paths"]
        assert os.path.abspath(str(ro_launch)) not in paths
        assert os.path.abspath(str(extra)) in paths
    finally:
        ro_launch.chmod(old_mode)


def test_sanitize_env_allowlist_filters_parent(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/corp.pem")
    out = _sanitize_env(None)
    assert "OPENAI_API_KEY" not in out
    assert out["HOME"] == "/home/tester"
    assert out["SSL_CERT_FILE"] == "/etc/ssl/corp.pem"
    assert out["PYTHONPATH"] == ""
