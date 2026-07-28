import json
import sys

import security.command_moderation as command_moderation
from tools.exec.execute_command import ExecuteCommand
from tools.exec.run_python_code import RunPythonCode


from tests.helpers.tool_ctx import with_local_ctx

def _config(tmp_path, mode):
    return {
        "launch_root": str(tmp_path),
        "command_moderation": {
            "enabled": True,
            "mode": mode,
            "allowed_command_examples": [],
        },
        "write_guard": {"enabled": True, "extra_paths": []},
    }


def _judge(verdict, risk_flags=None):
    return lambda _payload: {
        "verdict": verdict,
        "reasons": ["test judge"],
        "risk_flags": risk_flags or [],
        "confidence": 0.9,
    }


def test_exec_command_allow_mode_skips_judge(tmp_path, monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("judge should not be called in allow mode")

    monkeypatch.setattr(command_moderation, "_default_judge_client", fail_if_called)

    result = json.loads(
        with_local_ctx(ExecuteCommand, config_dict=_config(tmp_path, "allow"))(
            command=f'"{sys.executable}" -c "print(123)"'
        )
    )

    assert result["ok"] is True
    assert result["stdout"].rstrip("\n") == "123"


def test_exec_command_deny_mode_executes_safe_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(
        command_moderation,
        "_default_judge_client",
        lambda *_args, **_kwargs: _judge("safe"),
    )

    result = json.loads(
        with_local_ctx(ExecuteCommand, config_dict=_config(tmp_path, "deny"))(
            command=f'"{sys.executable}" -c "print(456)"'
        )
    )

    assert result["ok"] is True
    assert result["stdout"].rstrip("\n") == "456"


def test_exec_command_deny_mode_blocks_dangerous_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(
        command_moderation,
        "_default_judge_client",
        lambda *_args, **_kwargs: _judge(
            "potentially_dangerous",
            ["privilege_escalation"],
        ),
    )

    result = json.loads(
        with_local_ctx(ExecuteCommand, config_dict=_config(tmp_path, "deny"))(
            command="sudo apt update"
        )
    )

    assert result["ok"] is False
    assert result["exit_code"] is None
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert result["error"]["mode"] == "deny"
    assert result["error"]["risk_flags"] == ["privilege_escalation"]
    assert result["stdout"] == ""


def test_run_python_code_deny_mode_blocks_dangerous_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(
        command_moderation,
        "_default_judge_client",
        lambda *_args, **_kwargs: _judge(
            "potentially_dangerous",
            ["writes_outside_launch_root"],
        ),
    )

    target = tmp_path / "outside.txt"
    code = f"from pathlib import Path; Path({str(target)!r}).write_text('bad')"
    result = json.loads(with_local_ctx(RunPythonCode, config_dict=_config(tmp_path, "deny"))(code=code))

    assert result["ok"] is False
    assert result["return_code"] is None
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert result["error"]["mode"] == "deny"
    assert not target.exists()


def test_exec_command_ask_mode_executes_when_user_confirms(tmp_path):
    marker = tmp_path / "confirmed.txt"
    command = f'"{sys.executable}" -c "from pathlib import Path; Path({str(marker)!r}).write_text(\'ok\')"'

    tool = with_local_ctx(ExecuteCommand, config_dict=_config(tmp_path, "ask"))
    original_call = tool.__call__.__wrapped__.__wrapped__
    guarded_call = command_moderation.moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: _judge(
            "potentially_dangerous",
            ["writes_outside_launch_root"],
        ),
        user_confirmation_extractor=lambda *_args, **_kwargs: True,
    )(original_call)

    result = json.loads(guarded_call(tool, command=command))

    assert result["ok"] is True
    assert marker.read_text(encoding="utf-8") == "ok"


def test_exec_command_ask_mode_cancels_when_user_rejects(tmp_path):
    marker = tmp_path / "cancelled.txt"
    command = f'"{sys.executable}" -c "from pathlib import Path; Path({str(marker)!r}).write_text(\'bad\')"'

    tool = with_local_ctx(ExecuteCommand, config_dict=_config(tmp_path, "ask"))
    original_call = tool.__call__.__wrapped__
    guarded_call = command_moderation.moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: _judge(
            "potentially_dangerous",
            ["writes_outside_launch_root"],
        ),
        user_confirmation_extractor=lambda *_args, **_kwargs: False,
    )(original_call)

    result = json.loads(guarded_call(tool, command=command))

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_cancelled_by_user"
    assert result["error"]["mode"] == "ask"
    assert not marker.exists()


def test_command_moderation_fail_closed_when_judge_unavailable_in_deny(tmp_path):
    """Fail-CLOSED в deny: при недоступности судьи выполнение блокируется."""
    result = json.loads(
        with_local_ctx(ExecuteCommand, config_dict=_config(tmp_path, "deny"))(
            command=f'"{sys.executable}" -c "print(789)"'
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert result["error"]["mode"] == "deny"


def test_command_moderation_asks_when_judge_unavailable_in_ask(tmp_path, monkeypatch):
    """В ask при недоступности судьи запрашиваем подтверждение у пользователя."""
    marker = tmp_path / "judge_unavailable.txt"
    command = f'"{sys.executable}" -c "from pathlib import Path; Path({str(marker)!r}).write_text(\'ok\')"'

    tool = with_local_ctx(ExecuteCommand, config_dict=_config(tmp_path, "ask"))
    original_call = tool.__call__.__wrapped__.__wrapped__
    guarded_call = command_moderation.moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        user_confirmation_extractor=lambda *_args, **_kwargs: True,
    )(original_call)

    result = json.loads(guarded_call(tool, command=command))

    assert result["ok"] is True
    assert marker.read_text(encoding="utf-8") == "ok"
