import json
import types

from security.path_policy import enforce_write_guard


class _Tool:
    def __init__(self, ctx):
        self._ctx = ctx

    @enforce_write_guard(path_arg="path")
    def write(self, path, content):
        return json.dumps({"ok": True})


def _ctx(cwd, confirm=False):
    return types.SimpleNamespace(
        config_dict={"launch_root": str(cwd), "write_guard": {"enabled": True, "mode": "deny"}},
        is_tui=False, tui_confirm=None, logger=None,
    )


def test_operational_gc_write_denied(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = _ctx(tmp_path)
    res = _Tool(ctx).write(path=str(tmp_path / ".gc" / "config.yml"), content="x")
    assert json.loads(res)["ok"] is False  # заблокировано floor-ом


def test_operational_gc_write_denied_even_when_guard_disabled(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = types.SimpleNamespace(
        config_dict={"launch_root": str(tmp_path), "write_guard": {"enabled": False}},
        is_tui=False, tui_confirm=None, logger=None,
    )
    res = _Tool(ctx).write(path=str(tmp_path / ".gc" / "config.yml"), content="x")
    assert json.loads(res)["ok"] is False  # floor blocks even when write_guard disabled


def test_skills_write_allowed(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = _ctx(tmp_path)
    (tmp_path / ".gc" / "skills" / "foo").mkdir(parents=True)
    res = _Tool(ctx).write(path=str(tmp_path / ".gc" / "skills" / "foo" / "SKILL.md"), content="x")
    assert json.loads(res)["ok"] is True


# ---------------------------------------------------------------------------
# TUI repro — guard ask должен идти через confirm-порт, не stdin
# ---------------------------------------------------------------------------
import types as _types
import security.path_policy as _pp
import security.confirm_port as _cp


def _ctx_ask_tui(launch_root, tui_confirm):
    """ctx для mode=ask в TUI-режиме."""
    return _types.SimpleNamespace(
        config_dict={"launch_root": str(launch_root), "write_guard": {"enabled": True, "mode": "ask"}},
        is_tui=True,
        tui_confirm=tui_confirm,
        logger=None,
    )


def test_write_guard_ask_tui_confirmed(tmp_path, monkeypatch):
    """TUI + mode=ask + banner подтверждает → запись проходит."""
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    # launch_root — подкаталог; файл снаружи него → WriteGuardViolation
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    recorded = []
    tui_confirm = lambda **kw: recorded.append(kw) or "once"  # 3-значный контракт: once=разрешить
    ctx = _ctx_ask_tui(launch_root, tui_confirm)

    out_path = str(tmp_path / "outside.txt")

    import json
    res = _Tool(ctx).write(path=out_path, content="x")
    assert json.loads(res)["ok"] is True, "Banner confirmed → write should pass"
    assert recorded, "tui_confirm должен быть вызван (баннер показан)"


def test_write_guard_ask_tui_declined(tmp_path, monkeypatch):
    """TUI + mode=ask + banner отклоняет → запись заблокирована."""
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    # launch_root — подкаталог; файл снаружи него → WriteGuardViolation
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    tui_confirm = lambda **kw: "deny"  # 3-значный контракт: deny=отклонить
    ctx = _ctx_ask_tui(launch_root, tui_confirm)

    out_path = str(tmp_path / "outside.txt")

    import json
    res = _Tool(ctx).write(path=out_path, content="x")
    parsed = json.loads(res)
    assert parsed["ok"] is False
    assert parsed["error"]["type"] == "write_guard_cancelled_by_user"
