"""Роль запуска в хранилище агента (``agent.role``).

Один агент на сервер, у поверхностей — свои роли: вариант L0 и
специнструкций внутри агента (``L0@<роль>``, ``special_instructions@<роль>``).
Хранилище в базе читает вариант роли без отката на общий; файловое роль не
использует — у него роль задаёт путь ``paths.agent_md_path``.
"""

from gc_core.agent_store import FileAgentStore, Prompt
from gc_core.agent_store.roles import RoleAgentStore, with_role
from gc_core.instructions.loader import abuild_instructions
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import make_settings


def _store():
    # Без отката на файловое: как хранилище в базе, чего нет — того нет.
    return MemoryAgentStore(
        fallback=None,
        prompts={
            "special_instructions": "общие",
            "special_instructions@chat": "роль chat",
            "special_instructions@chat.completions": "роль completions",
            "summary": "сжатие",
        },
        instructions={"L0": "общая роль", "L0@chat": "роль чата"},
    )


async def test_role_reads_its_own_variants():
    view = RoleAgentStore(
        _store(), role="chat", special_role="chat.completions"
    )
    assert await view.read("instructions", "L0") == "роль чата".encode()
    assert (
        await view.read("prompt", Prompt.SPECIAL_INSTRUCTIONS)
        == "роль completions".encode()
    )
    # Остальное — общее у агента.
    assert await view.read("prompt", Prompt.SUMMARY) == "сжатие".encode()


async def test_missing_variant_does_not_fall_back_to_the_common_one():
    view = RoleAgentStore(_store(), role="a2a")
    assert await view.read("instructions", "L0") is None
    assert await view.read("prompt", Prompt.SPECIAL_INSTRUCTIONS) is None
    assert "L0@a2a" in view.label("instructions", "L0")


async def test_settings_put_the_role_on_a_store_of_its_own():
    store = _store()
    settings = make_settings(
        {
            "agent.role": "chat",
            "agent.special_instructions_role": "chat.completions",
        }
    )
    view = with_role(store, settings)
    assert (
        await view.read("prompt", Prompt.SPECIAL_INSTRUCTIONS)
        == "роль completions".encode()
    )
    assert with_role(store, make_settings({})) is store
    files = FileAgentStore(settings)
    assert with_role(files, settings) is files


async def test_run_context_reads_the_role_of_its_settings():
    from gc_core.run_context import acreate_run_context

    cfg = make_settings(
        {
            "agent.model": "M",
            "agent.role": "chat",
            "logging.save_logs": False,
        }
    )
    ctx = await acreate_run_context(cfg, agent_store=_store())
    loaded = await abuild_instructions(is_tui=False, store=ctx.agent_store)
    assert "роль чата" in loaded.text
    assert "общая роль" not in loaded.text


def test_role_name_cannot_leave_its_key():
    import pytest
    from pydantic import ValidationError

    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(ValidationError):
            make_settings({"agent.role": bad})
