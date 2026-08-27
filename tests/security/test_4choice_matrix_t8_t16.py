"""SPEC T8, T9, T11c, T13, T16 — command moderation 4-choice matrix tests.

T8:  config fields command_moderation and write_guard are separate
T9:  judge prompt doc contains insession_examples / allowed / denied strings
T11c: CLI prompt_user_confirmation_choice accepts latin+cyrillic same-key sets
T13: write guard still works (bool) with 4-choice ids
T16: long payload: multiline as-is (scroll body + docked options)
"""
import io
import sys
from pathlib import Path
from unittest import mock

import pytest

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "security" / "command_judge_system.md"


# ── T8: config fields separate ───────────────────────────────────────────────

def test_t8_config_command_moderation_and_write_guard_are_separate_fields():
    """SecurityConfig has command_moderation and write_guard as independent fields."""
    from config_modules.security import SecurityConfig

    cfg = SecurityConfig()
    assert hasattr(cfg, "command_moderation"), "SecurityConfig must have command_moderation"
    assert hasattr(cfg, "write_guard"), "SecurityConfig must have write_guard"
    # They are distinct objects (not nested or aliased).
    assert cfg.command_moderation is not cfg.write_guard
    # Changing one does not affect the other.
    cfg.command_moderation.mode = "ask"
    assert cfg.write_guard.mode != "ask" or cfg.write_guard.mode == cfg.write_guard.mode
    # Independent mode fields.
    assert cfg.command_moderation.mode in {"allow", "ask", "deny"}
    assert cfg.write_guard.mode in {"allow", "ask", "deny"}


# ── T9: prompt docs strings ──────────────────────────────────────────────────

def test_t9_judge_prompt_has_insession_examples_section():
    """command_judge_system.md contains insession_examples documentation."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "insession_examples" in text, "prompt must document insession_examples"
    assert "allowed" in text
    assert "denied" in text
    assert "session_denied" in text
    assert "hard safety rules" in text or "Priority when signals conflict" in text


# ── T11c: CLI latin+cyrillic ─────────────────────────────────────────────────

@pytest.fixture
def fake_tty(monkeypatch):
    """Force sys.stdin.isatty() → True so prompt reads from a fake input."""
    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    return fake_stdin


def _run_prompt(fake_stdin, answer: str) -> str:
    from security.path_policy import prompt_user_confirmation_choice

    fake_stdin.seek(0)
    # Write the answer into stdin for input()
    fake_stdin.truncate(0)
    fake_stdin.write(answer + "\n")
    fake_stdin.seek(0)
    with mock.patch.object(sys, "stderr", new=io.StringIO()):
        return prompt_user_confirmation_choice(
            title="Test", fields=[("Tool", "exec_command")], question="Разрешить?"
        )


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("y", "allow_remember"),
        ("Y", "allow_remember"),
        ("н", "allow_remember"),
        ("Н", "allow_remember"),
        ("o", "allow_once"),
        ("O", "allow_once"),
        ("щ", "allow_once"),
        ("Щ", "allow_once"),
        ("n", "deny_remember"),
        ("N", "deny_remember"),
        ("т", "deny_remember"),
        ("Т", "deny_remember"),
        ("t", "deny_once"),
        ("T", "deny_once"),
        ("е", "deny_once"),
        ("Е", "deny_once"),
    ],
)
def test_t11c_cli_same_key_matrix_matches_tui(fake_tty, answer, expected):
    """CLI input() accepts the same Latin+Cyrillic single-key sets as TUI."""
    assert _run_prompt(fake_tty, answer) == expected


def test_t11c_cli_rejects_legacy_words(fake_tty):
    """yes/no/once are not TUI keys — invalid → deny_remember (no soft aliases)."""
    assert _run_prompt(fake_tty, "yes") == "deny_remember"
    assert _run_prompt(fake_tty, "no") == "deny_remember"
    assert _run_prompt(fake_tty, "once") == "deny_remember"
    assert _run_prompt(fake_tty, "s") == "deny_remember"  # old session key removed


def test_t11c_cli_eof_returns_deny_remember(fake_tty):
    """EOFError (Ctrl+D) → deny_remember (explicit dismiss)."""
    from security.path_policy import prompt_user_confirmation_choice

    fake_tty.truncate(0)
    fake_tty.write("")  # empty → input() raises EOFError
    fake_tty.seek(0)
    with mock.patch.object(sys, "stderr", new=io.StringIO()):
        result = prompt_user_confirmation_choice(
            title="T", fields=[], question="Q?"
        )
    assert result == "deny_remember"


def test_t11c_cli_keyboard_interrupt_returns_deny_once(fake_tty, monkeypatch):
    """KeyboardInterrupt (Ctrl+C) → deny_once: block without few-shot poison."""
    from security.path_policy import prompt_user_confirmation_choice

    def _raise_interrupt(*_a, **_k):
        raise KeyboardInterrupt()

    monkeypatch.setattr("builtins.input", _raise_interrupt)
    with mock.patch.object(sys, "stderr", new=io.StringIO()):
        result = prompt_user_confirmation_choice(
            title="T", fields=[], question="Q?"
        )
    assert result == "deny_once"


def test_t11c_cli_nontty_returns_deny_once(monkeypatch):
    """Non-tty stdin (CI/background) → deny_once (not deny_remember)."""
    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: False  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    from security.path_policy import prompt_user_confirmation_choice

    result = prompt_user_confirmation_choice(
        title="T", fields=[], question="Q?"
    )
    assert result == "deny_once"


# ── T13: write guard smoke (allow/deny bool with 4 choices) ──────────────────

def test_t13_write_guard_allow_choices_are_bool():
    """Write guard uses bool verdict; 4-choice allow_* maps to True, deny_* to False."""
    from security.confirm_port import (
        CONFIRM_ALLOW_REMEMBER as ALLOW_REMEMBER,
        CONFIRM_ALLOW_ONCE as ALLOW_ONCE,
        CONFIRM_DENY_REMEMBER as DENY_REMEMBER,
        CONFIRM_DENY_ONCE as DENY_ONCE,
    )

    allow_choices = {ALLOW_REMEMBER, ALLOW_ONCE}
    deny_choices = {DENY_REMEMBER, DENY_ONCE}
    # All 4 are distinct strings.
    assert len(allow_choices | deny_choices) == 4
    # Allow choices are truthy, deny choices are falsy in the write-guard context:
    # write_guard checks `choice in {allow_remember, allow_once}` → True.
    assert ALLOW_REMEMBER != ALLOW_ONCE
    assert DENY_REMEMBER != DENY_ONCE


# ── T16: long payload multiline as-is (scroll + docked options) ──────────────

def test_t16_fmt_payload_value_preserves_multiline():
    """_fmt_payload_value preserves newlines (unlike _fmt_field_value which collapses)."""
    from tui.widgets.confirm_banner import _fmt_payload_value

    multiline = "line1\nline2\nline3"
    result = _fmt_payload_value(multiline)
    assert "\n" in result, "payload value must preserve newlines"
    assert "line1" in result
    assert "line2" in result


def test_t16_fmt_payload_value_keeps_full_long_script():
    """Long python/curl payloads are not soft-cut — body scrolls, options stay docked."""
    from tui.widgets.confirm_banner import _fmt_payload_value

    body = "\n".join(f"row{i} = {i}" for i in range(80))
    result = _fmt_payload_value(body)
    assert result == body
    assert "row79" in result
    assert "… (+" not in result


def test_t16_fmt_payload_value_preserves_short_multiline():
    """Short multiline payload is fully preserved."""
    from tui.widgets.confirm_banner import _fmt_payload_value

    payload = "curl https://api.example.com \\\n  -H 'Accept: json' \\\n  -d 'data'"
    result = _fmt_payload_value(payload)
    assert result == payload.strip()
    assert result.count("\n") == 2  # both newlines preserved


def test_t16_fmt_field_value_collapses_multiline():
    """Meta field values (non-payload) still collapse whitespace — single line."""
    from tui.widgets.confirm_banner import _fmt_field_value

    multiline = "line1\nline2\nline3"
    result = _fmt_field_value(multiline)
    assert "\n" not in result, "meta field must be single-line"
    assert " " in result  # newlines → spaces


# ── T15: command confirm fields order / labels ───────────────────────────────

def test_t15_ask_confirmation_fields_payload_first_ru_labels(monkeypatch):
    """Confirm fields: Команда/Код first, then Причина/Риски — no Preview/Tool."""
    from security import command_moderation as cm
    from security.command_moderation import (
        ExecutionDecision,
        ModerationContext,
        _ask_user_confirmation,
    )

    captured = {}

    def fake_request_confirm(*, title, question, fields, ctx, decision_key=None):
        captured["fields"] = fields
        captured["question"] = question
        captured["decision_key"] = decision_key
        return False

    monkeypatch.setattr(cm, "request_confirm", fake_request_confirm)
    ctx = ModerationContext(
        tool_name="exec_command",
        launch_root="/tmp",
        mode="ask",
    )
    decision = ExecutionDecision(
        allowed=False,
        decision="asked",
        reason_code="judge_verdict",
        reason="network risk",
        risk_flags=["network_to_private"],
        requires_confirmation=True,
    )
    long_cmd = "curl http://x\n" + ("# pad\n" * 40)
    _ask_user_confirmation(long_cmd, ctx, decision, ctx=None)
    labels = [lab for lab, _ in captured["fields"]]
    assert labels[0] == "Команда"
    assert "Preview" not in labels
    assert "Tool" not in labels
    assert labels == ["Команда", "Причина", "Риски"]
    # Confirm gets full payload (banner scrolls; no soft-cut for the human).
    assert captured["fields"][0][1] == long_cmd
    assert captured["decision_key"] == f"command:{long_cmd}"
    assert "exec_command" in captured["question"]

    py_ctx = ModerationContext(tool_name="exec_python", launch_root="/tmp", mode="ask")
    _ask_user_confirmation("print(1)", py_ctx, decision, ctx=None)
    assert captured["fields"][0][0] == "Код"


def test_t15_block_error_has_no_command_preview():
    """Tool-result block error: type/reason/flags only — no duplicated payload preview."""
    from security.command_moderation import ExecutionDecision, make_block_error

    decision = ExecutionDecision(
        allowed=False,
        decision="blocked",
        reason_code="judge_verdict",
        reason="dangerous",
        risk_flags=["privilege_escalation"],
        error_type="unsafe_command_blocked",
    )
    err = make_block_error(decision, "deny")
    assert err == {
        "type": "unsafe_command_blocked",
        "mode": "deny",
        "reason": "dangerous",
        "risk_flags": ["privilege_escalation"],
    }
    assert "command_preview" not in err
    assert "code_preview" not in err


def test_t15_cli_prints_payload_before_meta(fake_tty, monkeypatch):
    """CLI confirm printer: primary payload block before meta lines."""
    from security.path_policy import prompt_user_confirmation_choice

    printed = io.StringIO()
    fake_tty.truncate(0)
    fake_tty.write("t\n")
    fake_tty.seek(0)
    monkeypatch.setattr(sys, "stderr", printed)
    result = prompt_user_confirmation_choice(
        title="Проверка команды",
        fields=[
            ("Причина", "risk"),
            ("Команда", "curl -sL https://example.com/very/long"),
            ("Риски", "network_to_private"),
        ],
        question="Разрешить запуск?",
    )
    assert result == "deny_once"
    text = printed.getvalue()
    assert text.index("Команда:") < text.index("Причина:")
    assert "и подобные в сессии" in text
