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


def test_skills_write_allowed(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = _ctx(tmp_path)
    (tmp_path / ".gc" / "skills" / "foo").mkdir(parents=True)
    res = _Tool(ctx).write(path=str(tmp_path / ".gc" / "skills" / "foo" / "SKILL.md"), content="x")
    assert json.loads(res)["ok"] is True
