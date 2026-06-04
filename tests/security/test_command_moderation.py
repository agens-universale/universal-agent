import json
import os
from types import SimpleNamespace

import pytest

from security.command_moderation import (
    JudgeResult,
    ModerationContext,
    build_judge_payload,
    decide_command_execution,
    effective_command_cwd,
    load_command_judge_prompt,
    moderate_command_execution,
    validate_judge_result,
)
from security.path_policy import WriteGuardConfig, ToolSecurityContext
import security.command_moderation as command_moderation


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(message % args if args else message)


def test_validate_judge_result_accepts_safe_payload():
    result = validate_judge_result(
        {
            "verdict": "safe",
            "reasons": ["read-only command"],
            "risk_flags": [],
            "confidence": 0.9,
        }
    )

    assert result == JudgeResult(
        verdict="safe",
        reasons=["read-only command"],
        risk_flags=[],
        confidence=0.9,
    )


def test_validate_judge_result_requires_risk_flags_for_dangerous_verdict():
    with pytest.raises(ValueError, match="requires at least one risk flag"):
        validate_judge_result(
            {
                "verdict": "potentially_dangerous",
                "reasons": ["writes outside project"],
                "risk_flags": [],
                "confidence": 0.7,
            }
        )


def test_decide_command_execution_matrix():
    safe = JudgeResult("safe", ["ok"], [], 1.0)
    dangerous = JudgeResult("potentially_dangerous", ["bad"], ["privilege_escalation"], 1.0)

    assert decide_command_execution("allow", None, False, None).allowed is True
    # Fail-CLOSED: при недоступности судьи в режимах ask/deny — блокируем.
    deny_no_judge = decide_command_execution("deny", None, False, None)
    assert deny_no_judge.allowed is False
    assert deny_no_judge.reason_code == "judge_unavailable"
    assert deny_no_judge.error_type == "unsafe_command_blocked"
    ask_no_judge = decide_command_execution("ask", None, False, None)
    assert ask_no_judge.allowed is False
    assert ask_no_judge.error_type == "unsafe_command_blocked"
    assert decide_command_execution("deny", safe, True, None).allowed is True

    denied = decide_command_execution("deny", dangerous, True, None)
    assert denied.allowed is False
    assert denied.error_type == "unsafe_command_blocked"

    ask = decide_command_execution("ask", dangerous, True, None)
    assert ask.allowed is False
    assert ask.requires_confirmation is True

    confirmed = decide_command_execution("ask", dangerous, True, True)
    cancelled = decide_command_execution("ask", dangerous, True, False)
    assert confirmed.allowed is True
    assert cancelled.error_type == "unsafe_command_cancelled_by_user"


def test_effective_command_cwd_exec_python_defaults_to_launch_root():
    ctx = ToolSecurityContext(
        launch_root="/repo/lr",
        write_guard=WriteGuardConfig(enabled=True, extra_paths=[]),
    )
    assert effective_command_cwd("exec_python", {}, ctx) == os.path.abspath("/repo/lr")


def test_effective_command_cwd_exec_command_defaults_to_getcwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = ToolSecurityContext(
        launch_root=str(tmp_path / "lr"),
        write_guard=WriteGuardConfig(enabled=True, extra_paths=[]),
    )
    assert effective_command_cwd("exec_command", {}, ctx) == os.path.abspath(os.getcwd())


def test_build_judge_payload_contains_runtime_context():
    context = ModerationContext(
        tool_name="exec_command",
        launch_root="/repo",
        extra_paths=["/tmp/cache"],
        mode="deny",
        cwd="/repo",
        os_name="Linux",
        shell="/bin/bash",
        has_venv=True,
        allowed_command_examples=["pytest"],
    )

    payload = build_judge_payload("exec_command", "pytest", context)

    assert payload["launch_root"] == "/repo"
    assert payload["allowed_write_paths"] == ["/repo", "/tmp/cache"]
    assert "extra_paths" not in payload
    assert payload["runtime"]["shell"] == "/bin/bash"
    assert payload["allowed_command_examples"] == ["pytest"]
    assert "mode" not in payload


def test_command_judge_prompts_are_loaded_from_files():
    system_prompt = load_command_judge_prompt("system")
    user_prompt = load_command_judge_prompt("user")

    assert "security judge" in system_prompt
    assert "allowed_command_examples are positive examples" in system_prompt
    assert "not a strict executable allowlist" in system_prompt
    assert "{judge_payload_json}" in user_prompt


def test_build_judge_messages_uses_external_prompt_templates():
    messages = command_moderation._build_judge_messages(
        {
            "tool_name": "exec_command",
            "payload": "pytest",
            "launch_root": "/repo",
            "allowed_write_paths": ["/repo"],
        }
    )

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "exec_command" in messages[1]["content"]
    assert "pytest" in messages[1]["content"]


def test_moderate_command_execution_blocks_dangerous_payload(tmp_path):
    config_dict = {
        "launch_root": str(tmp_path),
        "command_moderation": {"enabled": True, "mode": "deny"},
    }

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: lambda _payload: {
            "verdict": "potentially_dangerous",
            "reasons": ["uses sudo"],
            "risk_flags": ["privilege_escalation"],
            "confidence": 0.8,
        },
    )
    def run(command: str, config_dict: dict) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("sudo apt update", config_dict=config_dict))

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert result["error"]["risk_flags"] == ["privilege_escalation"]


def test_moderate_command_execution_fail_closed_when_judge_unavailable(tmp_path):
    """
    Fail-CLOSED: при недоступности судьи в режимах ask/deny выполнение
    блокируется. Это изменение от AS-IS (которое было fail-open) — см.
    T114-finding в hardening_findings_20260518.md.
    """
    config_dict = {
        "launch_root": str(tmp_path),
        "command_moderation": {"enabled": True, "mode": "deny"},
    }

    @moderate_command_execution(payload_arg="command", tool_name="exec_command")
    def run(command: str, config_dict: dict) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("echo ok", config_dict=config_dict))

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    # envelope error от make_block_error содержит mode/reason/risk_flags,
    # но reason_code остаётся в audit-логе, а не в envelope. Проверяем reason.
    assert "judge" in result["error"].get("reason", "").lower() \
        or "fail-closed" in result["error"].get("reason", "").lower()


def test_moderate_command_execution_audits_without_payload_leak(tmp_path):
    logger = _Logger()
    ctx = SimpleNamespace(
        config_dict={
            "launch_root": str(tmp_path),
            "command_moderation": {"enabled": True, "mode": "deny"},
        },
        logger=logger,
    )

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: lambda _payload: {
            "verdict": "potentially_dangerous",
            "reasons": ["uses sudo"],
            "risk_flags": ["privilege_escalation"],
            "confidence": 0.8,
        },
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("sudo apt update", ctx=ctx))

    assert result["ok"] is False
    assert '"decision": "blocked"' in logger.messages[-1]
    assert '"reason_code": "judge_verdict"' in logger.messages[-1]
    assert '"risk_flags": ["privilege_escalation"]' in logger.messages[-1]
    assert "sudo apt update" not in logger.messages[-1]
