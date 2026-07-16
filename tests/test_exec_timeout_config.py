"""agent.exec_timeout_sec → config_dict[exec_timeout_sec] → execute_command / run_python_code."""
import json
from pathlib import Path

import pytest
import yaml

from config import load_config
from config_modules.agent import DEFAULT_EXEC_TIMEOUT_SEC, AgentConfig
from config_modules.root import AppConfig
from tests.helpers.tool_ctx import with_local_ctx
from tools.backends.protocol import ExecResult
from tools.exec.execute_command import ExecuteCommand
from tools.exec.run_python_code import RunPythonCode


def test_agent_config_timeout_default():
    assert AgentConfig().exec_timeout_sec == DEFAULT_EXEC_TIMEOUT_SEC
    assert DEFAULT_EXEC_TIMEOUT_SEC == 120


def test_agent_config_timeout_custom():
    assert AgentConfig(exec_timeout_sec=600).exec_timeout_sec == 600


def test_agent_config_timeout_must_be_positive():
    with pytest.raises(Exception):
        AgentConfig(exec_timeout_sec=0)
    with pytest.raises(Exception):
        AgentConfig(exec_timeout_sec=-1)


def test_load_config_maps_exec_timeout_sec(tmp_path: Path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.dump(
            {
                "backend": {"completions_url": "http://localhost/v1/chat/completions"},
                "agent": {"exec_timeout_sec": 600},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg["exec_timeout_sec"] == 600


def test_load_config_timeout_default_when_omitted(tmp_path: Path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.dump({"backend": {"completions_url": "http://localhost/v1/chat/completions"}}),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg["exec_timeout_sec"] == DEFAULT_EXEC_TIMEOUT_SEC
    assert AppConfig.model_validate({}).agent.exec_timeout_sec == DEFAULT_EXEC_TIMEOUT_SEC


def test_execute_command_uses_config_default(monkeypatch):
    seen = {}

    def _fake_exec(self, command, *, cwd=None, timeout=None, env=None):
        seen["timeout"] = timeout
        return ExecResult(stdout="ok", stderr="", return_code=0, timed_out=False)

    monkeypatch.setattr(
        "tools.backends.local.LocalFsBackend.exec",
        _fake_exec,
    )
    tool = with_local_ctx(ExecuteCommand, config_dict={"exec_timeout_sec": 600})
    env = json.loads(tool(command="true"))
    assert env["ok"] is True
    assert seen["timeout"] == 600


def test_run_python_uses_config_default(monkeypatch):
    seen = {}

    def _fake_run_python(self, code, *, cwd=None, timeout=None, env=None):
        seen["timeout"] = timeout
        return ExecResult(stdout="ok", stderr="", return_code=0, timed_out=False)

    monkeypatch.setattr(
        "tools.backends.local.LocalFsBackend.run_python",
        _fake_run_python,
    )
    tool = with_local_ctx(RunPythonCode, config_dict={"exec_timeout_sec": 600})
    env = json.loads(tool(code="print(1)"))
    assert env["ok"] is True
    assert seen["timeout"] == 600
