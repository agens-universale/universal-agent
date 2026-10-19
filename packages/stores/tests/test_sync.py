"""``gc-stores sync``: ресурсы агента из каталога и умолчания пакета в
хранилище агента."""

from gc_core.agent_store import FileAgentStore, Prompt
from gc_stores import sqlite
from gc_stores.defaults import bundled_source
from gc_stores.sync import copy_defaults


async def test_copy_defaults_puts_package_prompts_and_system_skills(tmp_path):
    store = sqlite.SQLiteAgentStore(tmp_path / "a.db", agent_id="a")
    report = await copy_defaults(store)
    bundled = await FileAgentStore().read("prompt", Prompt.SUMMARY)
    assert await store.read("prompt", Prompt.SUMMARY) == bundled
    assert "prompt:summary" in report.written
    assert any(name.startswith("skill:system/") for name in report.written)
    stored = await store.list_resources()
    assert {r.source for r in stored} == {bundled_source()}
    again = await copy_defaults(store)
    assert again.written == [] and len(again.unchanged) == len(report.written)


async def test_copy_defaults_keeps_a_users_prompt(tmp_path):
    store = sqlite.SQLiteAgentStore(tmp_path / "a.db", agent_id="a")
    await store.put_resource("prompt", Prompt.SUMMARY, b"own")
    report = await copy_defaults(store)
    assert await store.read("prompt", Prompt.SUMMARY) == b"own"
    assert "prompt:summary" not in report.written


async def test_copy_defaults_dry_run_writes_nothing(tmp_path):
    store = sqlite.SQLiteAgentStore(tmp_path / "a.db", agent_id="a")
    report = await copy_defaults(store, dry_run=True)
    assert report.written and await store.list_resources() == []


import pytest
from gc_core.agent_store import ProjectKey
from gc_stores.sync import sync_directory


def _layout(root, files):
    for rel, text in files.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def _store(tmp_path):
    return sqlite.SQLiteAgentStore(tmp_path / "a.db", agent_id="a")


async def test_directory_lands_in_the_store_and_second_sync_writes_nothing(
    tmp_path,
):
    root = _layout(
        tmp_path / "agent",
        {
            "prompts/system.md": "S",
            "instructions/L1.md": "Глобально",
            "subagents/helper.md": "H",
            "skills/user/s/SKILL.md": "---\nname: s\n---\n",
            "skills/user/s/scripts/run.py": "print(1)",
            ".DS_Store": "мусор",
        },
    )
    store = _store(tmp_path)
    first = await sync_directory(store, root)
    assert sorted(first.written) == [
        "instructions:L1",
        "prompt:system",
        "skill:user/s/SKILL.md",
        "skill:user/s/scripts/run.py",
        "subagent:helper.md",
    ]
    assert await store.read("subagent", "helper.md") == b"H"
    assert [e.name for e in await store.list("skill", "user/s")] == [
        "SKILL.md",
        "scripts",
    ]
    second = await sync_directory(store, root)
    assert second.written == [] and len(second.unchanged) == 5


async def test_project_resources_go_to_the_project(tmp_path):
    root = _layout(
        tmp_path / "agent",
        {"instructions/L3.md": "Проект", "skills/project/p/SKILL.md": "P"},
    )
    store = _store(tmp_path)
    await sync_directory(store, root, project="repo")
    project = ProjectKey("repo")
    assert (
        await store.read("instructions", "L3", project=project)
        == "Проект".encode()
    )
    assert (
        await store.read("skill", "project/p/SKILL.md", project=project)
        == b"P"
    )


@pytest.mark.parametrize(
    "rel",
    ["instructions/L2.md", "instructions/L3.md", "skills/project/p/SKILL.md"],
)
async def test_project_layer_without_project_is_refused(tmp_path, rel):
    root = _layout(tmp_path / "agent", {rel: "x"})
    with pytest.raises(ValueError, match="--project"):
        await sync_directory(_store(tmp_path), root)


@pytest.mark.parametrize(
    "rel",
    [
        "prompts/unknown.md",
        "instructions/L9.md",
        "skills/other/x/SKILL.md",
        "notes.txt",
    ],
)
async def test_unknown_names_are_refused(tmp_path, rel):
    root = _layout(tmp_path / "agent", {rel: "x"})
    with pytest.raises(ValueError):
        await sync_directory(_store(tmp_path), root)


async def test_prune_keeps_other_scope_and_defaults(tmp_path):
    store = _store(tmp_path)
    await copy_defaults(store)
    await store.put_resource("prompt", "special_instructions", b"old")
    await store.put_resource("instructions", "L3", b"P", project="repo")
    root = _layout(tmp_path / "agent", {"prompts/system.md": "S"})
    report = await sync_directory(store, root, prune=True)
    assert report.deleted == ["prompt:special_instructions"]
    assert await store.read("prompt", "summary") is not None  # умолчание
    assert (
        await store.read("instructions", "L3", project=ProjectKey("repo"))
        == b"P"
    )


async def test_with_defaults_then_directory_override(tmp_path):
    store = _store(tmp_path)
    root = _layout(tmp_path / "agent", {"prompts/summary.md": "своё"})
    await sync_directory(store, root, with_defaults=True)
    assert await store.read("prompt", "summary") == "своё".encode()
    assert await store.read("prompt", "finish_gate") is not None


async def test_dry_run_writes_nothing(tmp_path):
    store = _store(tmp_path)
    root = _layout(tmp_path / "agent", {"prompts/system.md": "S"})
    report = await sync_directory(
        store, root, dry_run=True, with_defaults=True
    )
    assert "prompt:system" in report.written
    assert await store.list_resources() == []


def test_cli_sync_prints_the_report(tmp_path, capsys):
    from gc_stores.cli import main

    root = _layout(tmp_path / "agent", {"prompts/system.md": "S"})
    args = [
        "sync",
        str(root),
        "--store",
        "sqlite",
        "--path",
        str(tmp_path / "a.db"),
        "--agent-id",
        "a",
    ]
    assert main(args) == 0
    assert "записано: 1" in capsys.readouterr().out
    assert main([*args, "--dry-run"]) == 0
    assert "без изменений: 1" in capsys.readouterr().out


async def test_symlinks_are_refused(tmp_path):
    """Находка проверки безопасности: ссылка в каталоге агента на файл вне
    его (ключи, секреты) не уходит в хранилище — отказ, а не чтение."""
    from gc_testkit.helpers.crossplatform import require_symlinks

    require_symlinks(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("ключ", encoding="utf-8")
    root = _layout(tmp_path / "agent", {"prompts/system.md": "S"})
    (root / "prompts" / "summary.md").symlink_to(secret)
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="ссылк"):
        await sync_directory(store, root)
    assert await store.list_resources() == []


async def test_symlinked_directory_is_refused(tmp_path):
    from gc_testkit.helpers.crossplatform import require_symlinks

    require_symlinks(tmp_path)
    outside = _layout(tmp_path / "outside", {"s/SKILL.md": "чужое"})
    root = _layout(tmp_path / "agent", {"prompts/system.md": "S"})
    (root / "skills").mkdir()
    (root / "skills" / "user").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="ссылк"):
        await sync_directory(_store(tmp_path), root)
