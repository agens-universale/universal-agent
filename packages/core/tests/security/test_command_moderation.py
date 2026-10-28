import json
import os

import pytest
from gc_core.security import command_moderation as command_moderation
from gc_core.security.command_moderation import (
    DANGEROUS_VERDICT,
    NO_WORKAROUND_NOTICE,
    SAFE_VERDICT,
    JudgeResult,
    JudgeUnavailable,
    ModerationContext,
    aload_command_judge_prompt,
    build_judge_payload,
    decide_command_execution,
    effective_command_cwd,
    make_block_error,
    moderate_command_execution,
    validate_judge_result,
)
from gc_core.security.path_policy import ToolSecurityContext, WriteGuardConfig
from gc_testkit.paths import ASSETS
from gc_testkit.settings import fake_ctx


class _Logger:
    def __init__(self):
        self.messages = []
        self.warnings = []

    def info(self, message, *args):
        self.messages.append(message % args if args else message)

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message % args if args else message)


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


def test_validate_judge_result_accepts_string_confidence():
    """Модели иногда возвращают confidence строкой, например \"0.98\"."""
    result = validate_judge_result(
        {
            "verdict": "safe",
            "reasons": ["read-only import checks"],
            "risk_flags": [],
            "confidence": "0.98",
        }
    )

    assert result.confidence == 0.98


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
    dangerous = JudgeResult(
        "potentially_dangerous", ["bad"], ["privilege_escalation"], 1.0
    )

    assert decide_command_execution("allow", None, False, None).allowed is True
    # Fail-CLOSED в deny; в ask — запрос подтверждения у пользователя.
    deny_no_judge = decide_command_execution("deny", None, False, None)
    assert deny_no_judge.allowed is False
    assert deny_no_judge.reason_code == "judge_unavailable"
    assert deny_no_judge.error_type == "unsafe_command_blocked"
    ask_no_judge = decide_command_execution("ask", None, False, None)
    assert ask_no_judge.allowed is False
    assert ask_no_judge.requires_confirmation is True
    assert ask_no_judge.reason_code == "judge_unavailable"
    assert decide_command_execution("ask", None, False, True).allowed is True
    assert (
        decide_command_execution("ask", None, False, False).error_type
        == "unsafe_command_cancelled_by_user"
    )
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
    assert effective_command_cwd("exec_python", {}, ctx) == os.path.abspath(
        "/repo/lr"
    )


def test_effective_command_cwd_exec_command_defaults_to_getcwd(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    ctx = ToolSecurityContext(
        launch_root=str(tmp_path / "lr"),
        write_guard=WriteGuardConfig(enabled=True, extra_paths=[]),
    )
    assert effective_command_cwd("exec_command", {}, ctx) == os.path.abspath(
        os.getcwd()
    )


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


def test_moderation_context_default_os_name_uses_sys_platform(monkeypatch):
    """ModerationContext.os_name по умолчанию не должен звать platform.system()
    (на Windows это WMI-запрос, способный зависнуть). Значение берётся из sys.platform."""
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    ctx = ModerationContext(tool_name="execute_command", launch_root="/repo")
    assert ctx.os_name == "macOS"


def _judge_prompt(kind: str) -> str:
    from gc_core.agent_store import FileAgentStore
    from gc_testkit import run_coro

    return run_coro(aload_command_judge_prompt(kind, store=FileAgentStore()))


def test_command_judge_prompts_are_loaded_from_files():
    system_prompt = _judge_prompt("system")
    user_prompt = _judge_prompt("user")

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
        },
        prompts={
            "system": _judge_prompt("system"),
            "user": _judge_prompt("user"),
        },
    )

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "exec_command" in messages[1]["content"]
    assert "pytest" in messages[1]["content"]


def test_moderate_command_execution_blocks_dangerous_payload(tmp_path):
    ctx = fake_ctx(
        {
            "launch_root": str(tmp_path),
            "security.command_moderation": {"enabled": True, "mode": "deny"},
        }
    )

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: (
            lambda _payload: {
                "verdict": "potentially_dangerous",
                "reasons": ["uses sudo"],
                "risk_flags": ["privilege_escalation"],
                "confidence": 0.8,
            }
        ),
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("sudo apt update", ctx=ctx))

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert result["error"]["risk_flags"] == ["privilege_escalation"]


def test_moderate_command_execution_fail_closed_when_judge_unavailable_in_deny(
    tmp_path,
):
    """Fail-CLOSED в deny: при недоступности судьи выполнение блокируется."""
    ctx = fake_ctx(
        {
            "launch_root": str(tmp_path),
            "security.command_moderation": {"enabled": True, "mode": "deny"},
        }
    )

    @moderate_command_execution(
        payload_arg="command", tool_name="exec_command"
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("echo ok", ctx=ctx))

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"
    assert (
        "judge" in result["error"].get("reason", "").lower()
        or "fail-closed" in result["error"].get("reason", "").lower()
    )


def test_moderate_command_execution_logs_judge_failure(tmp_path):
    logger = _Logger()
    ctx = fake_ctx(
        config={
            "launch_root": str(tmp_path),
            "security.command_moderation": {"enabled": True, "mode": "deny"},
        },
        logger=logger,
    )

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: (
            lambda _payload: {
                "verdict": "safe",
                "reasons": [],
                "risk_flags": [],
                "confidence": "not-a-number",
            }
        ),
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("echo ok", ctx=ctx))

    assert result["ok"] is False
    assert len(logger.warnings) == 1
    assert "command_moderation_judge_failed" in logger.warnings[0]
    assert '"error_type": "ValueError"' in logger.warnings[0]
    assert "confidence must be numeric" in logger.warnings[0]


def test_moderate_command_execution_asks_when_judge_unavailable_in_ask(
    tmp_path, monkeypatch
):
    """В ask при недоступности судьи запрашиваем подтверждение у пользователя."""
    ctx = fake_ctx(
        {
            "launch_root": str(tmp_path),
            "security.command_moderation": {"enabled": True, "mode": "ask"},
        }
    )
    monkeypatch.setattr(
        command_moderation,
        "_ask_user_confirmation",
        lambda *_args, **_kwargs: True,
    )

    @moderate_command_execution(
        payload_arg="command", tool_name="exec_command"
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("echo ok", ctx=ctx))

    assert result["ok"] is True
    assert result["command"] == "echo ok"


def test_moderate_command_execution_audits_without_payload_leak(tmp_path):
    logger = _Logger()
    ctx = fake_ctx(
        config={
            "launch_root": str(tmp_path),
            "security.command_moderation": {"enabled": True, "mode": "deny"},
        },
        logger=logger,
    )

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: (
            lambda _payload: {
                "verdict": "potentially_dangerous",
                "reasons": ["uses sudo"],
                "risk_flags": ["privilege_escalation"],
                "confidence": 0.8,
            }
        ),
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("sudo apt update", ctx=ctx))

    assert result["ok"] is False
    assert '"decision": "blocked"' in logger.messages[-1]
    assert '"reason_code": "judge_verdict"' in logger.messages[-1]
    assert '"risk_flags": ["privilege_escalation"]' in logger.messages[-1]
    assert "sudo apt update" not in logger.messages[-1]


def test_parse_judge_json_accepts_valid_json():
    parsed = command_moderation._parse_judge_json(
        '{"verdict":"safe","reasons":["ok"],"risk_flags":[],"confidence":0.9}'
    )

    assert parsed["verdict"] == "safe"
    assert parsed["confidence"] == 0.9


def test_parse_judge_json_strips_think_wrapper():
    parsed = command_moderation._parse_judge_json(
        '<think>{"verdict":"dangerous"}</think>{"verdict":"safe","reasons":["ok"],"risk_flags":[],"confidence":0.9}'
    )

    assert parsed["verdict"] == "safe"
    assert parsed["confidence"] == 0.9


def test_parse_judge_json_regex_fallback_safe_on_malformed_json():
    raw = 'Sure! Here\'s the verdict:\n{"verdict":"safe","reasons":["read-only"],"risk_flags":[],"confidence":"0.98"'

    parsed = command_moderation._parse_judge_json(raw)

    assert parsed["verdict"] == SAFE_VERDICT
    assert validate_judge_result(parsed).verdict == SAFE_VERDICT


def test_parse_judge_json_regex_fallback_dangerous_on_malformed_json():
    raw = 'Analysis:\n{"verdict":"potentially_dangerous","reasons":["bad"],"risk_flags":["x"]'

    parsed = command_moderation._parse_judge_json(raw)

    assert parsed["verdict"] == DANGEROUS_VERDICT
    assert validate_judge_result(parsed).risk_flags == [
        "judge_response_malformed"
    ]


def test_parse_judge_json_regex_fallback_prefers_dangerous_when_both_present():
    raw = '{"verdict":"safe","reasons":[]},{"verdict":"potentially_dangerous","reasons":["bad"]'

    parsed = command_moderation._parse_judge_json(raw)

    assert parsed["verdict"] == DANGEROUS_VERDICT


def test_validate_judge_result_rejects_schema_style_verdict():
    with pytest.raises(ValueError, match="verdict"):
        validate_judge_result(
            {
                "verdict": "safe | potentially_dangerous",
                "reasons": ["x"],
                "risk_flags": [],
                "confidence": 1.0,
            }
        )


def test_parse_judge_json_unparseable_response_raises():
    with pytest.raises(JudgeUnavailable, match="unparseable"):
        command_moderation._parse_judge_json("I cannot classify this payload.")


_TWO_FENCED_VERDICTS = (
    "Example of a safe answer:\n```json\n"
    '{"verdict": "safe", "reasons": ["example"], "risk_flags": [], '
    '"confidence": 1.0}\n```\n'
    "My verdict:\n```json\n"
    '{"verdict": "potentially_dangerous", "reasons": ["pushes"], '
    '"risk_flags": ["data_leaving"], "confidence": 0.9}\n```'
)


def test_parse_judge_json_rejects_several_json_blocks():
    """Какой из блоков вердикт, не решить: пример «safe» перед настоящим
    вердиктом прошёл бы за разрешение."""
    with pytest.raises(JudgeUnavailable, match="several JSON blocks"):
        command_moderation._parse_judge_json(_TWO_FENCED_VERDICTS)


def test_parse_judge_json_accepts_one_fenced_block():
    parsed = command_moderation._parse_judge_json(
        '```json\n{"verdict": "safe", "reasons": ["ok"], "risk_flags": [], '
        '"confidence": 0.9}\n```'
    )
    assert parsed["verdict"] == "safe"


def test_several_json_blocks_take_the_no_answer_path(tmp_path):
    """Неоднозначный ответ — путь «судья не ответил»: в deny без человека
    команда блокируется, а не выполняется по примеру «safe»."""
    ctx = fake_ctx(
        {
            "launch_root": str(tmp_path),
            "security.command_moderation": {"enabled": True, "mode": "deny"},
        }
    )

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: (
            lambda _payload: command_moderation._parse_judge_json(
                _TWO_FENCED_VERDICTS
            )
        ),
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("git push origin main", ctx=ctx))

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"


def test_parse_judge_json_logs_regex_fallback(tmp_path):
    logger = _Logger()
    raw = 'oops {"verdict":"safe","reasons":["ok"],"risk_flags":[]'

    parsed = command_moderation._parse_judge_json(
        raw,
        logger=logger,
        tool_name="exec_python",
    )

    assert parsed["verdict"] == SAFE_VERDICT
    assert len(logger.warnings) == 1
    assert "command_moderation_judge_regex_fallback" in logger.warnings[0]
    assert '"matched_verdict": "safe"' in logger.warnings[0]
    assert '"tool_name": "exec_python"' in logger.warnings[0]


def test_moderate_command_execution_blocks_when_json_and_regex_fail(tmp_path):
    ctx = fake_ctx(
        {
            "launch_root": str(tmp_path),
            "security.command_moderation": {"enabled": True, "mode": "deny"},
        }
    )

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_args, **_kwargs: (
            lambda _payload: command_moderation._parse_judge_json(
                "completely unparseable judge output"
            )
        ),
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    result = json.loads(run("echo ok", ctx=ctx))

    assert result["ok"] is False
    assert result["error"]["type"] == "unsafe_command_blocked"


def test_build_judge_payload_includes_denied_examples():
    from gc_core.security.command_moderation import (
        ModerationContext,
        build_judge_payload,
    )

    ctx = ModerationContext(
        tool_name="exec_command",
        launch_root="/work",
        allowed_command_examples=["pytest tests/"],
        denied_command_examples=["rm -rf /"],
    )
    payload = build_judge_payload("exec_command", "rm -rf /", ctx)
    assert payload["denied_command_examples"] == ["rm -rf /"]
    assert payload["allowed_command_examples"] == ["pytest tests/"]


def test_load_settings_maps_denied_examples(tmp_path):
    import yaml
    from gc_core.config import load_settings

    p = tmp_path / "config.yml"
    p.write_text(
        yaml.safe_dump(
            {
                "security": {
                    "command_moderation": {
                        "enabled": True,
                        "mode": "deny",
                        "denied_command_examples": ["rm -rf /"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    moderation = load_settings(str(p)).settings.security.command_moderation
    assert moderation.denied_command_examples == ["rm -rf /"]


def test_judge_prompt_documents_denied_examples_and_flag():
    from pathlib import Path

    from gc_core import config_modules

    src = Path(config_modules.__file__).resolve().parents[1]
    text = (
        ASSETS / "prompts" / "security" / "command_judge_system.md"
    ).read_text(encoding="utf-8")
    assert "How to use denied_command_examples" in text
    assert "operator_denied" in text


# --- запрет на обход блокировки ------------------------------------------------
#
# Судья оценивает конкретную команду, а не намерение. На живом прогоне 17.08.2026
# заблокированное `rm -rf victim` было переписано агентом в
# `rm victim/a.txt && rmdir victim`, судья пропустил, каталог удалён. Инструкция
# «не обходи» жила только в ветках «пользователь отменил», а до ветки «судья
# заблокировал» не доезжала вовсе — и не была закреплена ни одним ассертом.


def _dangerous():
    return JudgeResult(
        "potentially_dangerous", ["bad"], ["privilege_escalation"], 1.0
    )


@pytest.mark.parametrize(
    "mode, judge_result, judge_available, user_confirmation",
    [
        # судья заблокировал — ветка, из-за которой всё и затевалось
        ("deny", _dangerous(), True, None),
        # судья недоступен в deny — fail-closed
        ("deny", None, False, None),
        # пользователь отказал
        ("ask", _dangerous(), True, False),
        ("ask", None, False, False),
    ],
)
def test_every_refusal_carries_no_workaround_guidance(
    mode, judge_result, judge_available, user_confirmation
):
    """Любой отказ доезжает до модели с явным запретом эквивалентной команды."""
    decision = decide_command_execution(
        mode, judge_result, judge_available, user_confirmation
    )
    assert decision.allowed is False
    error = make_block_error(decision, mode)
    assert error["guidance"] == NO_WORKAROUND_NOTICE
    assert "Do NOT retry" in error["guidance"]
    assert "equivalent" in error["guidance"]
    # ...и ровно один раз: ветки «пользователь отказал» приклеивали ту же
    # формулировку ещё и к reason, из-за чего модель получала её в одном
    # словаре дважды. Ассерта на это не было, и дубль жил незамеченным.
    assert "Do NOT retry" not in error["reason"], error["reason"]


def test_guidance_is_separate_from_judge_reason():
    """reason остаётся причинами судьи: его же показывают пользователю в диалоге
    подтверждения, и он зафиксирован примерами в доке."""
    decision = decide_command_execution("deny", _dangerous(), True, None)
    error = make_block_error(decision, "deny")
    assert error["reason"] == "bad"
    assert error["guidance"] != error["reason"]


def test_single_wording_shared_with_command_guard():
    """Формулировка одна на весь периметр — иначе правки разъедутся."""
    from gc_core.security import command_guard

    assert command_guard.NO_WORKAROUND_NOTICE is NO_WORKAROUND_NOTICE
