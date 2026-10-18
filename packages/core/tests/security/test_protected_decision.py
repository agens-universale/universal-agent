import json

from gc_core.execution.state import SharedStateData
from gc_core.security.path_policy import enforce_write_guard
from gc_core.security.permission_store import PermissionStore
from gc_core.security.protected_paths import (
    protected_decision,
    protection_enabled,
)
from gc_core.security.read_guard import enforce_read_guard
from gc_testkit.helpers.tool_ctx import local_ctx
from gc_testkit.settings import fake_ctx, make_settings


def test_decisions(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"

    assert (
        protected_decision(str(proj / ".gc" / "config.yml"), cwd=str(proj))
        == "deny"
    )
    assert protected_decision(str(proj / "AGENTS.md"), cwd=str(proj)) == "ask"
    assert (
        protected_decision(
            str(proj / ".gc" / "skills" / "foo" / "SKILL.md"), cwd=str(proj)
        )
        == "ask"
    )
    assert (
        protected_decision(str(proj / "src" / "main.py"), cwd=str(proj))
        == "allow"
    )


def test_protection_enabled_default_true():
    assert protection_enabled(make_settings()) is True
    assert protection_enabled(None) is True


def test_protection_follows_the_config_key():
    on = local_ctx({"security.protected_paths": {"enabled": True}})
    off = local_ctx({"security.protected_paths": {"enabled": False}})
    assert protection_enabled(on) is True
    assert protection_enabled(off) is False


def test_any_git_directory_is_protected_like_instructions(
    tmp_path, monkeypatch
):
    """`.git` хранит конфиг и хуки, которые git исполняет сам (`core.fsmonitor`
    при `git status`, хуки при `git commit`): молча писать туда нельзя ни в
    корне проекта, ни во вложенном репозитории. Как у эталона: `.git` —
    защищённый путь, запись спрашивается."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    for path in (
        proj / ".git" / "config",
        proj / "sub" / ".git" / "hooks" / "pre-commit",
        proj / ".git",
    ):
        assert protected_decision(str(path), cwd=str(proj)) == "ask", path
    assert (
        protected_decision(str(proj / ".gitignore"), cwd=str(proj)) == "allow"
    )
    assert (
        protected_decision(str(proj / "my.git" / "x"), cwd=str(proj))
        == "allow"
    )


def test_operational_roots_stay_denied_even_with_a_git_component(
    tmp_path, monkeypatch
):
    """`.git` под служебным каталогом или под `extra` — по-прежнему запрет:
    класс vcs проверяется после служебных, а не вместо них."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    assert (
        protected_decision(
            str(proj / ".gc" / ".git" / "config"), cwd=str(proj)
        )
        == "deny"
    )
    assert (
        protected_decision(
            str(proj / ".git" / "config"),
            cwd=str(proj),
            extra=[str(proj / ".git")],
        )
        == "deny"
    )


def test_git_directory_name_is_matched_regardless_of_case(
    tmp_path, monkeypatch
):
    """На Маке файловая система по умолчанию не различает регистр: `.GIT` — тот
    же каталог `.git`."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    assert (
        protected_decision(
            str(proj / ".GIT" / "hooks" / "pre-commit"), cwd=str(proj)
        )
        == "ask"
    )


@enforce_write_guard(path_arg="path")
def _write(path: str, ctx=None) -> str:
    return "written"


@enforce_read_guard(path_arg="path")
def _read(path: str, ctx=None) -> str:
    return "content"


def _guard_ctx(root, *, enabled):
    """edit, человек есть и на любой вопрос отвечает «нет»; ключ защиты —
    `enabled`."""
    shared = SharedStateData(permission_mode="edit")
    asked = []

    def tui_confirm(*, title, question, fields, **kwargs):
        asked.append(title)
        return "deny_once"

    ctx = fake_ctx(
        config={
            "launch_root": str(root),
            "security.protected_paths": {"enabled": enabled},
        },
        permission_store=PermissionStore(shared.confirm_rules, shared=shared),
        logger=None,
        is_tui=True,
        tui_confirm=tui_confirm,
    )
    ctx.asked = asked
    return ctx


def test_disabled_protection_lifts_the_write_floor(tmp_path, monkeypatch):
    """`.git/config` — вопрос пола; при `enabled: false` пола нет: запись в
    проекте в edit идёт без вопроса."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    root = tmp_path / "proj"
    root.mkdir()
    target = str(root / ".git" / "config")
    on = _guard_ctx(root, enabled=True)
    out = json.loads(_write(path=target, ctx=on))
    assert out["error"]["type"] == "protected_path_denied"
    assert on.asked == ["Защищённый путь"]
    off = _guard_ctx(root, enabled=False)
    assert _write(path=target, ctx=off) == "written"
    assert off.asked == []


def test_disabled_protection_lifts_the_read_floor(tmp_path, monkeypatch):
    """Служебный `.gc` проекта на чтение — отказ пола; при `enabled: false`
    чтение идёт, как любое чтение в проекте."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    root = tmp_path / "proj"
    (root / ".gc").mkdir(parents=True)
    target = root / ".gc" / "notes.txt"
    target.write_text("x", encoding="utf-8")
    on_ctx = _guard_ctx(root, enabled=True)
    on = json.loads(_read(path=str(target), ctx=on_ctx))
    assert on["ok"] is False
    assert on["error"]["type"] == "protected_path_denied"
    off = _guard_ctx(root, enabled=False)
    assert _read(path=str(target), ctx=off) == "content"
    assert off.asked == []
