import json
import sys

from gc_core.security import command_moderation as command_moderation
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.exec.run_python_code import RunPythonCode
from gc_testkit.helpers.tool_ctx import with_local_ctx


def _config(tmp_path):
    return {
        "launch_root": str(tmp_path),
        "command_moderation": {"enabled": True, "mode": "deny"},
        "write_guard": {"enabled": True, "extra_paths": []},
    }


def _dangerous_judge(*risk_flags):
    return lambda _payload: {
        "verdict": "potentially_dangerous",
        "reasons": ["security regression scenario"],
        "risk_flags": list(risk_flags),
        "confidence": 0.95,
    }


def test_blocks_system_package_install_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        command_moderation,
        "_default_judge_client",
        lambda *_args, **_kwargs: _dangerous_judge(
            "installs_software",
            "privilege_escalation",
        ),
    )

    result = json.loads(
        with_local_ctx(ExecuteCommand, _config(tmp_path))(
            command="sudo apt install nginx"
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert result["error"]["risk_flags"] == [
        "installs_software",
        "privilege_escalation",
    ]


def test_blocks_shell_profile_modification_command(tmp_path, monkeypatch):
    profile_path = tmp_path / ".bashrc"
    command = f'"{sys.executable}" -c "from pathlib import Path; Path({str(profile_path)!r}).write_text(\'export BAD=1\')"'
    monkeypatch.setattr(
        command_moderation,
        "_default_judge_client",
        lambda *_args, **_kwargs: _dangerous_judge("modifies_environment"),
    )

    result = json.loads(
        with_local_ctx(ExecuteCommand, _config(tmp_path))(command=command)
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert not profile_path.exists()


def test_blocks_python_code_writing_outside_launch_root(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "outside.txt"
    code = (
        f"from pathlib import Path; Path({str(outside)!r}).write_text('bad')"
    )
    monkeypatch.setattr(
        command_moderation,
        "_default_judge_client",
        lambda *_args, **_kwargs: _dangerous_judge(
            "writes_outside_launch_root"
        ),
    )

    result = json.loads(
        with_local_ctx(RunPythonCode, _config(launch_root))(code=code)
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert not outside.exists()


def test_blocks_obfuscated_command_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        command_moderation,
        "_default_judge_client",
        lambda *_args, **_kwargs: _dangerous_judge("obfuscated_intent"),
    )

    result = json.loads(
        with_local_ctx(ExecuteCommand, _config(tmp_path))(
            command="echo cm0gLXJmIC8= | base64 -d"
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert result["error"]["risk_flags"] == ["obfuscated_intent"]
