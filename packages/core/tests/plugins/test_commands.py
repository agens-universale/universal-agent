"""Команды плагинов: подстановка в ядре, общее пространство имён со skills
(§4.2)."""

import asyncio
import json

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules.root import AppConfig
from gc_core.message_policy.user_message_policy import (
    is_technical_user_message_text,
)
from gc_core.plugins import catalog, installed, sources
from gc_core.plugins.catalog import build_plugin_snapshot, with_plugin_snapshot
from gc_core.plugins.commands import (
    Resolution,
    command_messages,
    expand_command,
    resolve_slash_name,
)
from gc_core.plugins.manifest import PluginCommand
from gc_core.plugins.snapshot import PluginSnapshot
from gc_core.settings import LoadedSettings
from gc_core.tools.skills.registry import get_skills_registry
from gc_testkit.settings import make_settings
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    catalog.reset_plugin_cache()


def make_cmd(name, body="Тело $ARGUMENTS"):
    return PluginCommand(
        name=name,
        description="",
        argument_hint="",
        allowed_tools=(),
        body=body,
        source=f"/p/commands/{name}.md",
    )


def _plugin(tmp_path, name="plug", *, commands=(), skills=()):
    root = tmp_path / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    for cmd, body in commands:
        path = root / "commands" / f"{cmd}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    for skill in skills:
        path = root / "skills" / skill / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {skill}\ndescription: d\n---\nbody\n",
            encoding="utf-8",
        )
    return root


def test_expand_arguments_and_positionals():
    cmd = make_cmd("review", "Ревью $1: $ARGUMENTS")
    assert (
        expand_command(cmd, "main.py безопасность")
        == "Ревью main.py: main.py безопасность"
    )


def test_missing_positional_is_empty():
    assert expand_command(make_cmd("r", "[$2]"), "one") == "[]"


def test_short_name_ambiguous_across_commands_and_skills():
    r = resolve_slash_name("review", [("a", make_cmd("review"))], ["b:review"])
    assert r.kind == "ambiguous" and r.candidates == ("a:review", "b:review")


def test_full_and_unique_short_names_resolve():
    commands = [("a", make_cmd("review"))]
    assert resolve_slash_name("a:review", commands, ["b:review"]) == (
        Resolution("command", "a:review", ("a:review",))
    )
    assert resolve_slash_name("b:review", commands, ["b:review"]) == (
        Resolution("skill", "b:review", ("b:review",))
    )
    assert resolve_slash_name("review", commands, []) == (
        Resolution("command", "a:review", ("a:review",))
    )
    assert resolve_slash_name("x:review", commands, []) == (
        Resolution("none", "", ())
    )


def test_flat_skill_counts_for_short_name():
    r = resolve_slash_name("review", [("a", make_cmd("review"))], ["review"])
    assert r.candidates == ("a:review", "review")


def test_command_messages_expand_into_technical_message():
    snap = PluginSnapshot(commands=(("a", make_cmd("review", "R $1")),))

    (msg,) = command_messages(snap, [], "/a:review main.py")

    assert msg["role"] == "user"
    assert is_technical_user_message_text(msg["content"])
    assert "R main.py" in msg["content"]
    assert command_messages(snap, [], "просто текст") == []
    assert command_messages(snap, [], "/nope x") == []
    assert command_messages(snap, ["b:review"], "/b:review x") == []


def test_ambiguous_command_gets_notice_and_no_expansion():
    snap = PluginSnapshot(commands=(("a", make_cmd("review", "R-BODY")),))

    (msg,) = command_messages(snap, ["b:review"], "/review x")

    assert "R-BODY" not in msg["content"]
    assert "a:review" in msg["content"] and "b:review" in msg["content"]


def test_command_with_skill_id_dropped_with_notice(tmp_path):
    root = _plugin(
        tmp_path,
        commands=[("review", "CMD"), ("other", "OTHER")],
        skills=["review"],
    )
    loaded = LoadedSettings(
        settings=AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    )

    snap = build_plugin_snapshot(loaded, clone=False)

    assert [(p, c.name) for p, c in snap.commands] == [("plug", "other")]
    assert any("plug:review" in n for n in snap.notices)


def test_command_not_in_skills_list(tmp_path):
    loaded = make_settings().with_runtime(
        plugins=PluginSnapshot(commands=(("demo", make_cmd("hi")),))
    )
    reg = get_skills_registry(loaded, project_root=str(tmp_path / "p"))

    ids = {s["id"] for s in asyncio.run(reg.list_skills(include_all=True))}

    assert "demo:hi" not in ids


@pytest.fixture
def _stub_turn_finish(monkeypatch):
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    real = pr.aapply_tool

    async def _apply(**kw):
        if kw.get("tool_name") == "turn_finish":
            return await stub_turn_finish_apply(**kw)
        return await real(**kw)

    monkeypatch.setattr(pr, "aapply_tool", _apply)


@pytest.mark.usefixtures("_stub_turn_finish")
async def test_intent_log_gets_expanded_command(tmp_path, monkeypatch):
    from gc_core import run_context as rc

    root = _plugin(tmp_path, commands=[("hi", "SAY-HI $ARGUMENTS\n")])
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{"plugins.paths": [str(root)], "plugins.use_installed": False},
    )
    finish = assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "direct_answer",
            "task_report": "ok",
            "handoff": "{}",
        },
        call_id="c1",
    )
    backend = install(monkeypatch, ScriptedBackend([finish, judge_accept()]))
    ctx = await rc.acreate_run_context(with_plugin_snapshot(cfg, clone=False))

    await AgentExecutor(ctx).arun(["/plug:hi there"], thread_id="t1")

    asks = [
        r.content
        for r in ctx.active_state.user_intent_log
        if r.action == "user_ask"
    ]
    assert asks == ["SAY-HI there"]
    assert "SAY-HI there" in json.dumps(backend.calls[0], ensure_ascii=False)


def test_dropped_command_uses_skill_id_from_frontmatter(tmp_path):
    root = _plugin(
        tmp_path, commands=[("review", "C"), ("audit", "A"), ("noid", "N")]
    )
    skill = root / "skills" / "folder"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nid: review\n---\nb\n")
    empty = root / "skills" / "noid"
    empty.mkdir()
    (empty / "SKILL.md").write_text('---\nid: ""\n---\nb\n')
    loaded = LoadedSettings(
        settings=AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    )

    snap = build_plugin_snapshot(loaded, clone=False)

    assert [c.name for _, c in snap.commands] == ["audit"]


def test_missing_skills_folder_does_not_break_snapshot(tmp_path, monkeypatch):
    root = _plugin(tmp_path, commands=[("hi", "H")], skills=["s"])
    real = catalog.skill_roots

    def gone(plugins):
        return tuple(
            r.__class__(r.plugin, str(tmp_path / "gone"), r.contain)
            for r in real(plugins)
        )

    monkeypatch.setattr(catalog, "skill_roots", gone)
    loaded = LoadedSettings(
        settings=AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    )

    snap = build_plugin_snapshot(loaded, clone=False)

    assert [c.name for _, c in snap.commands] == ["hi"]


@pytest.mark.usefixtures("_stub_turn_finish")
async def test_command_with_technical_tail_is_expanded(tmp_path, monkeypatch):
    """Строка TUI с ``@файлом`` — пара «запрос + технический хвост»."""
    from gc_core import run_context as rc
    from gc_core.message_policy.user_message_policy import (
        wrap_technical_user_content,
    )

    root = _plugin(tmp_path, commands=[("hi", "SAY-HI $ARGUMENTS\n")])
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{"plugins.paths": [str(root)], "plugins.use_installed": False},
    )
    finish = assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "direct_answer",
            "task_report": "ok",
            "handoff": "{}",
        },
        call_id="c1",
    )
    backend = install(monkeypatch, ScriptedBackend([finish, judge_accept()]))
    ctx = await rc.acreate_run_context(with_plugin_snapshot(cfg, clone=False))

    await AgentExecutor(ctx).arun(
        ["/plug:hi @main.py", wrap_technical_user_content("file main.py")],
        thread_id="t1",
    )

    request = json.dumps(backend.calls[0], ensure_ascii=False)
    assert "SAY-HI @main.py" in request
