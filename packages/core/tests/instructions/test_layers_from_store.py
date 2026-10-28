"""L2 и L3 — ресурсы проекта в хранилище агента: TUI берёт их у хранилища
по проекту рана (``agent.project_id`` или каталог), а не файлом с диска."""

from gc_core.agent_store import ProjectKey
from gc_core.instructions.loader import abuild_instructions
from gc_core.state_preparation import (
    StatePreparationRequest,
    aload_state_metadata,
)
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import fake_ctx


def _store() -> MemoryAgentStore:
    return MemoryAgentStore(
        instructions={"L1": "глобальные"},
        projects={
            "p": {"instructions": {"L2": "из .gc", "L3": "корневые"}},
        },
        fallback=None,
    )


async def test_tui_reads_l2_l3_from_the_store():
    store = _store()
    loaded = await abuild_instructions(
        is_tui=True, store=store, project=ProjectKey("p")
    )
    assert loaded.loaded_ids == ["L1", "L2", "L3"]
    assert loaded.text.index("из .gc") < loaded.text.index("корневые")
    assert ("instructions", "L3") in store.reads


async def test_other_project_has_no_project_layers():
    loaded = await abuild_instructions(
        is_tui=True, store=_store(), project=ProjectKey("чужой")
    )
    assert loaded.loaded_ids == ["L1"]


async def test_override_reads_only_l3_of_the_project():
    loaded = await abuild_instructions(
        is_tui=True,
        store=_store(),
        project=ProjectKey("p"),
        agentsmd_override=True,
    )
    assert loaded.loaded_ids == ["L3"]


async def test_state_metadata_uses_the_project_of_the_run(tmp_path):
    """Проект рана — из контекста (``agent.project_id``), а не из cwd."""
    store = _store()
    ctx = fake_ctx(
        {"agent.project_id": "p"},
        project_gc_dir=tmp_path / ".gc",
        agent_store=store,
    )
    _status, loaded = await aload_state_metadata(
        StatePreparationRequest(is_tui=True),
        metadata_config=ctx,
        store=store,
    )
    assert loaded.loaded_ids == ["L1", "L2", "L3"]
