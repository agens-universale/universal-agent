import json

from gc_core.security.path_policy import enforce_write_guard


class _Tool:
    def __init__(self, ctx):
        self._ctx = ctx

    @enforce_write_guard(path_arg="path")
    def write(self, path, content):
        return json.dumps({"ok": True})


def _ctx(cwd, confirm=False):
    return fake_ctx(
        config={
            "launch_root": str(cwd),
            "security.write_guard": {"enabled": True, "mode": "deny"},
        },
        is_tui=False,
        tui_confirm=None,
        logger=None,
    )


def test_operational_gc_write_denied(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = _ctx(tmp_path)
    res = _Tool(ctx).write(
        path=str(tmp_path / ".gc" / "config.yml"), content="x"
    )
    assert json.loads(res)["ok"] is False  # заблокировано floor-ом


def test_operational_gc_write_denied_even_when_guard_disabled(
    tmp_path, monkeypatch
):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = fake_ctx(
        config={
            "launch_root": str(tmp_path),
            "security.write_guard": {"enabled": False},
        },
        is_tui=False,
        tui_confirm=None,
        logger=None,
    )
    res = _Tool(ctx).write(
        path=str(tmp_path / ".gc" / "config.yml"), content="x"
    )
    assert (
        json.loads(res)["ok"] is False
    )  # floor blocks even when write_guard disabled


def test_skills_write_is_not_silent(tmp_path, monkeypatch):
    """Скилл — защищённый путь с вопросом: без человека запись не проходит."""
    import io

    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    # Без человека: при `pytest -s` вопрос не должен уйти в консоль.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    ctx = _ctx(tmp_path)
    (tmp_path / ".gc" / "skills" / "foo").mkdir(parents=True)
    res = _Tool(ctx).write(
        path=str(tmp_path / ".gc" / "skills" / "foo" / "SKILL.md"), content="x"
    )
    assert json.loads(res)["ok"] is False
    assert json.loads(res)["error"]["type"] == "protected_path_denied"


# ---------------------------------------------------------------------------
# TUI repro — guard ask должен идти через confirm-порт, не stdin
# ---------------------------------------------------------------------------
from gc_testkit.settings import fake_ctx


def _ctx_ask_tui(launch_root, tui_confirm):
    """ctx для mode=ask в TUI-режиме."""
    return fake_ctx(
        config={
            "launch_root": str(launch_root),
            "security.write_guard": {"enabled": True, "mode": "ask"},
        },
        is_tui=True,
        tui_confirm=tui_confirm,
        logger=None,
    )


def test_instruction_file_confirm_cached_by_decision_key(
    tmp_path, monkeypatch
):
    """Повторная запись инструкц-файла (AGENTS.md) в одном прогоне НЕ переспрашивает:
    первый ответ кэшируется по decision_key, остальные правки авто-проходят."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    from gc_core.security.permission_store import PermissionStore

    calls = []

    def tui_confirm(*, title, question, fields):
        calls.append(question)
        return "once"

    ctx = fake_ctx(
        config={
            "launch_root": str(tmp_path),
            "security.write_guard": {"enabled": True, "mode": "deny"},
        },
        is_tui=True,
        tui_confirm=tui_confirm,
        logger=None,
        permission_store=PermissionStore(),
    )
    agents_md = str(tmp_path / "AGENTS.md")

    r1 = _Tool(ctx).write(path=agents_md, content="a")
    assert json.loads(r1)["ok"] is True
    assert len(calls) == 1, "первая запись — один confirm"

    r2 = _Tool(ctx).write(path=agents_md, content="b")
    assert json.loads(r2)["ok"] is True
    assert len(calls) == 1, (
        f"повторная запись того же файла не должна переспрашивать, calls={calls}"
    )


def test_write_guard_ask_tui_confirmed(tmp_path, monkeypatch):
    """TUI + mode=ask + banner подтверждает → запись проходит."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    # launch_root — подкаталог; файл снаружи него → WriteGuardViolation
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    recorded = []
    tui_confirm = lambda **kw: (
        recorded.append(kw) or "once"
    )  # 3-значный контракт: once=разрешить
    ctx = _ctx_ask_tui(launch_root, tui_confirm)

    out_path = str(tmp_path / "outside.txt")

    import json

    res = _Tool(ctx).write(path=out_path, content="x")
    assert json.loads(res)["ok"] is True, (
        "Banner confirmed → write should pass"
    )
    assert recorded, "tui_confirm должен быть вызван (баннер показан)"


def test_write_guard_ask_tui_declined(tmp_path, monkeypatch):
    """TUI + mode=ask + banner отклоняет → запись заблокирована."""
    from gc_core.config_modules import env_vars

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
