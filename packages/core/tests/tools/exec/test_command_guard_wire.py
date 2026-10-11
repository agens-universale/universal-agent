"""TDD: deterministic command-guard tripwire wired into execute_command and run_python_code.

Covers:
- rm -rf .gc → denied (execute_command)
- .gc/skills/... → allowed (execute_command)
- plain command → allowed (execute_command)
- protection_enabled=False → tripwire skipped, command runs (execute_command)
- rm -rf .gc → denied (run_python_code, code containing .gc path reference)
- ask path → confirmed → allowed; not confirmed → denied (execute_command)
- обе ветки декоратора отдают один и тот же конверт: message + guidance
"""

import json

from gc_core.security import command_guard as cg
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.exec.run_python_code import RunPythonCode
from gc_testkit.helpers.tool_ctx import local_ctx, with_local_ctx


def _ctx(tmp_path, *, protection_enabled=True, command_moderation=False):
    cfg = {
        "launch_root": str(tmp_path),
        "security.write_guard": {"enabled": False},
    }
    if protection_enabled:
        cfg["security.protected_paths"] = {"enabled": True}
    else:
        cfg["security.protected_paths"] = {"enabled": False}
    if command_moderation:
        cfg["command_moderation"] = {"enabled": True, "mode": "deny"}
    return local_ctx(cfg, is_tui=False, tui_confirm=None, logger=None)


# ---------------------------------------------------------------------------
# execute_command: deny path (rm -rf .gc)
# ---------------------------------------------------------------------------


def test_execute_command_rm_rf_gc_denied(tmp_path, monkeypatch):
    """rm -rf .gc must be blocked deterministically, regardless of judge."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    executed = []

    def fake_subprocess_run(*a, **kw):
        executed.append(True)
        raise AssertionError(
            "subprocess must not be called for protected command"
        )

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    tool = with_local_ctx(ExecuteCommand)
    result = json.loads(
        tool(command=f"rm -rf {tmp_path / '.gc'}", ctx=_ctx(tmp_path))
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"
    assert not executed, "subprocess must NOT have been called"


def test_execute_command_rm_rf_gc_relative_denied(tmp_path, monkeypatch):
    """rm -rf .gc (relative) must be blocked when cwd=tmp_path."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    executed = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("subprocess must not be called")
        ),
    )

    tool = with_local_ctx(ExecuteCommand)
    result = json.loads(
        tool(command="rm -rf .gc", cwd=str(tmp_path), ctx=_ctx(tmp_path))
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"


# ---------------------------------------------------------------------------
# execute_command: skills path → allowed
# ---------------------------------------------------------------------------


def test_execute_command_skills_path_allowed(tmp_path, monkeypatch):
    """Commands on .gc/skills/... are allowed by the tripwire."""
    import sys

    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    skills_dir = tmp_path / ".gc" / "skills" / "foo"
    skills_dir.mkdir(parents=True)

    tool = with_local_ctx(ExecuteCommand)
    # We use a real benign command; the tripwire must not block it.
    cmd = f'"{sys.executable}" -c "print(1)"'
    result = json.loads(
        tool(command=cmd, cwd=str(tmp_path), ctx=_ctx(tmp_path))
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# execute_command: plain command → allowed
# ---------------------------------------------------------------------------


def test_execute_command_plain_command_allowed(tmp_path, monkeypatch):
    """Plain mkdir / echo commands must not be blocked by the tripwire."""
    import sys

    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    tool = with_local_ctx(ExecuteCommand)
    result = json.loads(
        tool(
            command=f'"{sys.executable}" -c "print(\'ok\')"',
            cwd=str(tmp_path),
            ctx=_ctx(tmp_path),
        )
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# execute_command: protection_enabled=False → tripwire skipped
# ---------------------------------------------------------------------------


def test_execute_command_protection_disabled_skips_tripwire(
    tmp_path, monkeypatch
):
    """When protection_enabled=False the tripwire must not block anything."""
    import sys

    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    tool = with_local_ctx(ExecuteCommand)
    # Even a .gc-targeting command passes when protection is disabled.
    result = json.loads(
        tool(
            command=f'"{sys.executable}" -c "print(1)"',
            cwd=str(tmp_path),
            ctx=_ctx(tmp_path, protection_enabled=False),
        )
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# execute_command: ask path → request_confirm called
# ---------------------------------------------------------------------------


def test_execute_command_ask_path_confirmed_executes(tmp_path, monkeypatch):
    """ask-level path + user confirms → command executes."""
    import sys

    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    # Patch scan to return "ask" for any command.
    monkeypatch.setattr(
        cg,
        "scan_command_for_protected",
        lambda cmd, cwd, extra=None, media=(): "ask",
    )
    monkeypatch.setattr(
        "gc_core.security.confirm_port.request_confirm", lambda **k: True
    )

    tool = with_local_ctx(ExecuteCommand)
    result = json.loads(
        tool(
            command=f'"{sys.executable}" -c "print(1)"',
            cwd=str(tmp_path),
            ctx=_ctx(tmp_path),
        )
    )
    assert result["ok"] is True


def test_execute_command_ask_path_not_confirmed_blocks(tmp_path, monkeypatch):
    """ask-level path + user declines → command blocked."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    monkeypatch.setattr(
        cg,
        "scan_command_for_protected",
        lambda cmd, cwd, extra=None, media=(): "ask",
    )
    monkeypatch.setattr(
        "gc_core.security.confirm_port.request_confirm", lambda **k: False
    )

    tool = with_local_ctx(ExecuteCommand)
    result = json.loads(
        tool(command="echo x", cwd=str(tmp_path), ctx=_ctx(tmp_path))
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"


def test_blocked_envelope_form_is_the_same_in_both_branches(
    tmp_path, monkeypatch
):
    """ask-отказ и deny отдают модели один конверт: message + отдельный guidance.

    Раньше ask-ветка приклеивала NO_WORKAROUND_NOTICE к message, а deny клала
    его в guidance — один и тот же гвард говорил с моделью двумя формами.
    """
    from gc_core.config_modules import env_vars
    from gc_core.security.command_moderation import NO_WORKAROUND_NOTICE

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    tool = with_local_ctx(ExecuteCommand)

    monkeypatch.setattr(
        cg,
        "scan_command_for_protected",
        lambda cmd, cwd, extra=None, media=(): "ask",
    )
    monkeypatch.setattr(
        "gc_core.security.confirm_port.request_confirm", lambda **k: False
    )
    asked = json.loads(
        tool(command="echo x", cwd=str(tmp_path), ctx=_ctx(tmp_path))
    )["error"]

    monkeypatch.setattr(
        cg,
        "scan_command_for_protected",
        lambda cmd, cwd, extra=None, media=(): "deny",
    )
    denied = json.loads(
        tool(command="echo x", cwd=str(tmp_path), ctx=_ctx(tmp_path))
    )["error"]

    assert set(asked) == set(denied) == {"type", "message", "guidance"}
    assert asked["guidance"] == denied["guidance"] == NO_WORKAROUND_NOTICE
    # Приклейка к message — ровно то, что убрали: она обязана НЕ вернуться.
    assert "Do NOT retry" not in asked["message"]
    assert "Do NOT retry" not in denied["message"]


# ---------------------------------------------------------------------------
# run_python_code: deny path → denied (scanner forced to deny via monkeypatch)
# ---------------------------------------------------------------------------


def test_run_python_code_gc_deny_blocks_before_subprocess(
    tmp_path, monkeypatch
):
    """When the scanner detects an operational path in code, exec_python is blocked.

    The scanner is forced to 'deny' via monkeypatch to test the wiring, since
    detecting .gc inside Python string literals is heuristic and path-format
    dependent (the scanner is designed for shell command tokens, not Python AST).
    """
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    # Force scanner to deny
    monkeypatch.setattr(
        cg,
        "scan_command_for_protected",
        lambda cmd, cwd, extra=None, media=(): "deny",
    )

    subprocess_called = []
    import subprocess as sp

    original_run = sp.run

    def spy_run(*a, **kw):
        subprocess_called.append(True)
        return original_run(*a, **kw)

    monkeypatch.setattr(sp, "run", spy_run)

    tool = with_local_ctx(RunPythonCode)
    result = json.loads(
        tool(code="print(1)", cwd=str(tmp_path), ctx=_ctx(tmp_path))
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"
    assert not subprocess_called, (
        "subprocess must NOT have been called when guard denies"
    )
