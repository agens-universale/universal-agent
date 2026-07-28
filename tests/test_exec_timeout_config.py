"""agent.exec_timeout_sec / exec_output_capture_max_chars → config_dict → tools/backends."""
import json
from pathlib import Path

import pytest
import yaml

from config import load_config
from config_modules.agent import (
    DEFAULT_EXEC_TIMEOUT_SEC,
    AgentConfig,
    exec_output_capture_max_chars_from_config_dict,
)
from backends.factory import resolve_os_backend
from backends.protocol import DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS, ExecResult
from config_modules.root import AppConfig
from tests.helpers.tool_ctx import with_local_ctx
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


def test_agent_config_capture_max_default():
    assert AgentConfig().exec_output_capture_max_chars == DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    assert DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS == 60_000


def test_agent_config_capture_max_custom():
    assert AgentConfig(exec_output_capture_max_chars=1000).exec_output_capture_max_chars == 1000


def test_agent_config_capture_max_rejects_negative():
    with pytest.raises(Exception):
        AgentConfig(exec_output_capture_max_chars=-1)


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


def test_load_config_maps_exec_output_capture_max_chars(tmp_path: Path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.dump(
            {
                "backend": {"completions_url": "http://localhost/v1/chat/completions"},
                "agent": {"exec_output_capture_max_chars": 12_000},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg["exec_output_capture_max_chars"] == 12_000


def test_load_config_timeout_default_when_omitted(tmp_path: Path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.dump({"backend": {"completions_url": "http://localhost/v1/chat/completions"}}),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg["exec_timeout_sec"] == DEFAULT_EXEC_TIMEOUT_SEC
    assert cfg["exec_output_capture_max_chars"] == DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    assert AppConfig.model_validate({}).agent.exec_timeout_sec == DEFAULT_EXEC_TIMEOUT_SEC


def test_factory_applies_capture_max_from_config():
    backend = resolve_os_backend({"exec_output_capture_max_chars": 1500})
    assert backend.exec_output_capture_max_chars == 1500
    assert exec_output_capture_max_chars_from_config_dict({}) == DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS


def test_local_ctx_applies_capture_max_from_config():
    tool = with_local_ctx(ExecuteCommand, config_dict={"exec_output_capture_max_chars": 777})
    assert tool._backend.exec_output_capture_max_chars == 777


def test_execute_command_uses_config_default(monkeypatch):
    seen = {}

    def _fake_exec(self, command, *, cwd=None, timeout=None, env=None):
        seen["timeout"] = timeout
        return ExecResult(stdout="ok", stderr="", return_code=0, timed_out=False)

    monkeypatch.setattr(
        "backends.local.LocalFsBackend.exec",
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
        "backends.local.LocalFsBackend.run_python",
        _fake_run_python,
    )
    tool = with_local_ctx(RunPythonCode, config_dict={"exec_timeout_sec": 600})
    env = json.loads(tool(code="print(1)"))
    assert env["ok"] is True
    assert seen["timeout"] == 600


def test_tool_descriptions_stay_lean():
    for cls in (ExecuteCommand, RunPythonCode):
        desc = cls.tool_description()
        props = desc["parameters"]["properties"]
        assert "max_output_chars" not in props
        text = desc["description"]
        assert "stdout_output_ref" not in text
        assert "session_read_stored_tool_output" not in text
        assert "max_output_chars" not in cls.prompt_description()
