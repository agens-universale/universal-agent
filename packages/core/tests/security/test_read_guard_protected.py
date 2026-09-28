import json
import types

from gc_core.security.read_guard import enforce_read_guard


class _Tool:
    def __init__(self, ctx):
        self._ctx = ctx

    @enforce_read_guard(path_arg="path")
    def read(self, path):
        return json.dumps({"ok": True, "data": "secret"})


def _ctx(cwd):
    return types.SimpleNamespace(
        config_dict={
            "launch_root": str(cwd),
            "read_guard": {"enabled": False},
        },
        ctx=None,
        logger=None,
    )


def test_operational_gc_read_denied_even_when_readguard_off(
    tmp_path, monkeypatch
):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = types.SimpleNamespace(
        config_dict={
            "launch_root": str(tmp_path),
            "read_guard": {"enabled": False},
        },
        logger=None,
    )
    res = _Tool(ctx).read(path=str(tmp_path / ".gc" / "config.yml"))
    assert json.loads(res)["ok"] is False


def test_skills_read_allowed(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    (tmp_path / ".gc" / "skills" / "foo").mkdir(parents=True)
    (tmp_path / ".gc" / "skills" / "foo" / "SKILL.md").write_text(
        "x", encoding="utf-8"
    )
    ctx = types.SimpleNamespace(
        config_dict={
            "launch_root": str(tmp_path),
            "read_guard": {"enabled": False},
        },
        logger=None,
    )
    res = _Tool(ctx).read(
        path=str(tmp_path / ".gc" / "skills" / "foo" / "SKILL.md")
    )
    assert json.loads(res)["ok"] is True
