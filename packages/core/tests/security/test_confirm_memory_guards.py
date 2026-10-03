"""
Tests: decision_key caching + instructive deny messages across security guards.

Covers Task 4: each guard passes decision_key to request_confirm so repeated
denies within a run don't re-prompt, and declined messages tell the agent not
to retry or silently substitute.
"""

import json

from gc_core.security.permission_store import PermissionStore
from gc_testkit.settings import fake_ctx, make_settings


def _tui_ctx(choice):
    """Build a minimal TUI ctx with a counting tui_confirm stub."""
    calls = {"n": 0}

    def tui_confirm(*, title, question, fields):
        calls["n"] += 1
        return choice

    ctx = fake_ctx(
        is_tui=True,
        tui_confirm=tui_confirm,
        permission_store=PermissionStore(),
    )
    return ctx, calls


def _configure(ctx, config):
    """Подменить настройки рана у готового ctx (тул уже держит ссылку на ctx)."""
    loaded = make_settings(config)
    ctx.settings = loaded.settings
    ctx.runtime = loaded.runtime


# ---------------------------------------------------------------------------
# write_guard (path_policy) — representative test per brief
# ---------------------------------------------------------------------------


def test_write_guard_decline_caches_run_and_instructs(monkeypatch, tmp_path):
    """[deny] write vне периметра: повтор не переспрашивает; сообщение инструктивно."""
    from gc_core.config_modules import env_vars
    from gc_core.security.path_policy import enforce_write_guard

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    ctx, calls = _tui_ctx("deny")
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    # Outside the launch_root
    outside = str(tmp_path / "outside.txt")

    captured = {}

    class _Tool:
        _ctx = ctx

        @enforce_write_guard(path_arg="path")
        def write(self, path, content="x"):
            return json.dumps({"ok": True})

        def _write_guard_blocked_result(self, err, *a, **kw):
            captured["err"] = err
            return json.dumps({"ok": False, "error": err})

    # Включить write_guard в режиме ask с нашим launch_root
    _configure(
        ctx,
        {
            "launch_root": str(launch_root),
            "write_guard": {"enabled": True, "mode": "ask"},
        },
    )

    tool = _Tool()

    # First call — tui_confirm must be invoked, result must be blocked
    res1 = tool.write(path=outside)
    assert json.loads(res1)["ok"] is False, "First call should be blocked"
    assert calls["n"] == 1, "tui_confirm should be called once on first ask"

    # Second call — same path, run-cache must prevent re-prompt
    res2 = tool.write(path=outside)
    assert json.loads(res2)["ok"] is False, (
        "Second call should still be blocked"
    )
    assert calls["n"] == 1, (
        "tui_confirm must NOT be called again (run-cache hit)"
    )

    # Instructive message check
    err_msg = captured["err"]["message"]
    assert "Do NOT retry" in err_msg, (
        f"Expected 'Do NOT retry' in: {err_msg!r}"
    )
    assert "ask the user" in err_msg, (
        f"Expected 'ask the user' in: {err_msg!r}"
    )


# ---------------------------------------------------------------------------
# url_policy — decline caches and instructs
# ---------------------------------------------------------------------------


def test_url_policy_decline_caches_and_instructs(monkeypatch, tmp_path):
    """url_policy ask: declined URL не переспрашивает; сообщение инструктивно."""
    from gc_core.config_modules import env_vars
    from gc_core.security.url_policy import enforce_url_policy

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    ctx, calls = _tui_ctx("deny")
    _configure(
        ctx,
        {
            "launch_root": str(tmp_path),
            "url_policy": {
                "enabled": True,
                "mode": "ask",
                "allowed_schemes": ["https"],
                "url_blacklist": ["example.com"],
            },
        },
    )

    captured = {}

    class _Tool:
        _ctx = ctx

        @enforce_url_policy(url_arg="url")
        def fetch(self, url):
            return json.dumps({"ok": True})

        def _url_policy_blocked_result(self, err, *a, **kw):
            captured["err"] = err
            return json.dumps({"ok": False, "error": err})

    tool = _Tool()
    blocked_url = "https://example.com/page"

    res1 = tool.fetch(url=blocked_url)
    assert json.loads(res1)["ok"] is False
    assert calls["n"] == 1, "First call should prompt"

    res2 = tool.fetch(url=blocked_url)
    assert json.loads(res2)["ok"] is False
    assert calls["n"] == 1, "Second call must use cache, no re-prompt"

    # The captured error message from url decline
    err = captured.get("err", {})
    # error type must reflect user cancellation
    assert err.get("type") == "url_blocked_by_policy_cancelled_by_user"
    # Instructive message check
    err_msg = err.get("message", "")
    assert "Do NOT retry" in err_msg, (
        f"Expected 'Do NOT retry' in: {err_msg!r}"
    )
    assert "ask the user" in err_msg, (
        f"Expected 'ask the user' in: {err_msg!r}"
    )


# ---------------------------------------------------------------------------
# read_guard — decline caches and instructs
# ---------------------------------------------------------------------------


def test_read_guard_decline_caches_and_instructs(monkeypatch, tmp_path):
    """read_guard ask: declined path не переспрашивает; сообщение инструктивно."""
    from gc_core.config_modules import env_vars
    from gc_core.security.read_guard import enforce_read_guard

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    ctx, calls = _tui_ctx("deny")
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = str(tmp_path / "secret.txt")

    _configure(
        ctx,
        {
            "launch_root": str(launch_root),
            "read_guard": {
                "enabled": True,
                "mode": "ask",
                "allowed_read_roots": [str(launch_root)],
            },
        },
    )

    captured = {}

    class _Tool:
        _ctx = ctx

        @enforce_read_guard(path_arg="path")
        def read(self, path):
            return json.dumps({"ok": True})

        def _read_guard_blocked_result(self, err, *a, **kw):
            captured["err"] = err
            return json.dumps({"ok": False, "error": err})

    tool = _Tool()

    res1 = tool.read(path=outside)
    assert json.loads(res1)["ok"] is False
    assert calls["n"] == 1, "First call should prompt"

    res2 = tool.read(path=outside)
    assert json.loads(res2)["ok"] is False
    assert calls["n"] == 1, "Second call must use cache, no re-prompt"

    err = captured.get("err", {})
    assert err.get("type") == "read_guard_cancelled_by_user"
    # Instructive message
    msg = err.get("message", "")
    assert "Do NOT retry" in msg, f"Expected 'Do NOT retry' in: {msg!r}"
    assert "ask the user" in msg, f"Expected 'ask the user' in: {msg!r}"


# ---------------------------------------------------------------------------
# command_moderation — ask + judge unavailable scenario
# ---------------------------------------------------------------------------


def test_command_moderation_decline_caches_and_instructs(
    monkeypatch, tmp_path
):
    """command_moderation ask, judge unavailable: decline кэшируется; сообщение инструктивно."""
    from gc_core.config_modules import env_vars
    from gc_core.security.command_moderation import moderate_command_execution

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    ctx, calls = _tui_ctx("deny")
    _configure(
        ctx,
        {
            "launch_root": str(tmp_path),
            "command_moderation": {
                "enabled": True,
                "mode": "ask",
            },
        },
    )

    captured = {}

    class _Tool:
        _ctx = ctx

        # Judge client that's always unavailable
        def _get_judge(self):
            raise RuntimeError("judge unavailable")

        @moderate_command_execution(
            payload_arg="command",
            tool_name="execute_command",
            judge_client_extractor=lambda self, *a, **kw: None,
        )
        def run(self, command):
            return json.dumps({"ok": True})

        def _command_moderation_blocked_result(self, err, *a, **kw):
            captured["err"] = err
            return json.dumps({"ok": False, "error": err})

    tool = _Tool()
    cmd = "rm -rf /tmp/test"

    res1 = tool.run(command=cmd)
    assert json.loads(res1)["ok"] is False
    # tui_confirm should have been called (judge unavailable → ask user)
    assert calls["n"] >= 1, "Should prompt at least once"
    n_after_first = calls["n"]

    res2 = tool.run(command=cmd)
    assert json.loads(res2)["ok"] is False
    assert calls["n"] == n_after_first, (
        "Second identical command must use cache"
    )

    # Запрет на обход едет отдельным полем guidance (make_block_error), а reason
    # остаётся причиной отказа: раньше одна и та же формулировка лежала в одном
    # словаре дважды — и в reason, и в guidance.
    err = captured.get("err", {})
    assert err.get("reason") == "User declined the command."
    guidance = err.get("guidance", "")
    assert "Do NOT retry" in guidance, (
        f"Expected 'Do NOT retry' in guidance: {guidance!r}"
    )
    assert "Do NOT retry" not in err.get("reason", ""), (
        "дубль в reason вернулся"
    )


# ---------------------------------------------------------------------------
# command_guard (tripwire) — decline caches
# ---------------------------------------------------------------------------


def test_command_guard_decline_caches(monkeypatch, tmp_path):
    """command_guard tripwire ask: decline кэшируется при повторе."""
    from gc_core.config_modules import env_vars
    from gc_core.security.command_guard import enforce_command_guard

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")

    # AGENTS.md is the "instructions" tier → triggers "ask" in scan_command_for_protected
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("instructions")

    ctx, calls = _tui_ctx("deny")
    _configure(
        ctx,
        {
            "launch_root": str(tmp_path),
            "security.protected_paths": {"enabled": True},
        },
    )

    class _Tool:
        _ctx = ctx

        @enforce_command_guard(
            payload_arg="command", tool_name="execute_command"
        )
        def run(self, command):
            return json.dumps({"ok": True})

        def _command_moderation_blocked_result(self, err, *a, **kw):
            return json.dumps({"ok": False, "error": err})

    tool = _Tool()
    # A command that references the AGENTS.md file (instructions → ask decision)
    cmd = f"cat {agents_md!s}"

    res1 = tool.run(command=cmd)
    assert json.loads(res1)["ok"] is False
    n_after_first = calls["n"]
    assert n_after_first >= 1, (
        "Should have prompted for instructions-touching command"
    )

    res2 = tool.run(command=cmd)
    assert json.loads(res2)["ok"] is False
    assert calls["n"] == n_after_first, (
        "Second call must use cache, no re-prompt"
    )
