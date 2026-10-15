"""Память проекта пишут параллельно (два инстанса, два окна): тул не теряет
чужую правку — перечитывает и применяет свою к свежему тексту, а повторный
конфликт отдаёт модели как ошибку тула."""

import json

from gc_core.agent_store import MemoryConflict, ProjectKey
from gc_core.project_memory import compaction
from gc_core.tools.project.project_memory import ProjectMemory
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import fake_ctx

PROJECT = ProjectKey(id="conflict", root="/proj")


class RacingStore(MemoryAgentStore):
    """Перед первыми ``races`` записями память меняет «другой инстанс»."""

    def __init__(self, *, races: int, **kwargs) -> None:
        super().__init__(fallback=None, **kwargs)
        self.races = races

    async def write_memory(self, project, text, *, expected_version):
        if self.races:
            self.races -= 1
            current = await self.read_memory(project)
            foreign = (current.text if current else "") + "\n\nчужое"
            await super().write_memory(
                project,
                foreign.strip(),
                expected_version=current.version if current else None,
            )
        return await super().write_memory(
            project, text, expected_version=expected_version
        )


def _tool(store, **config):
    ctx = fake_ctx(config, project_gc_dir="/proj/.gc", agent_store=store)
    tool = ProjectMemory(ctx=ctx, state=None)
    tool._project = PROJECT
    return tool


async def test_append_reapplies_delta_to_text_changed_concurrently():
    store = RacingStore(races=1, memory={"conflict": "своё"})
    out = json.loads(await _tool(store).acall("append", "новое"))
    assert out["ok"] is True
    memory = await store.read_memory(PROJECT)
    assert memory.text == "своё\n\nчужое\n\nновое"


async def test_second_conflict_is_a_tool_error():
    store = RacingStore(races=2, memory={"conflict": "своё"})
    out = json.loads(await _tool(store).acall("append", "новое"))
    assert out["ok"] is False
    assert out["error"]["type"] == "conflict"
    assert "новое" not in (await store.read_memory(PROJECT)).text


async def test_compaction_over_replaced_text_is_a_conflict(monkeypatch):
    async def compact(text, _ctx, _state=None):
        # Пока модель сжимала, память переписали.
        await store.write_memory(
            PROJECT,
            "переписано",
            expected_version=(await store.read_memory(PROJECT)).version,
        )
        return compaction.CompactionResult(True, "сжато", None, None)

    monkeypatch.setattr(compaction, "acompact_text", compact)
    store = MemoryAgentStore(fallback=None, memory={"conflict": "x" * 30})
    tool = _tool(store)
    tool._max_chars = 40
    out = json.loads(await tool.acall("append", "длинное продолжение"))
    assert out["error"]["type"] == "conflict"
    assert (await store.read_memory(PROJECT)).text == "переписано"


async def test_store_rejects_stale_write():
    store = MemoryAgentStore(fallback=None, memory={"conflict": "раз"})
    stale = (await store.read_memory(PROJECT)).version
    await store.write_memory(PROJECT, "два", expected_version=stale)
    try:
        await store.write_memory(PROJECT, "три", expected_version=stale)
    except MemoryConflict as conflict:
        assert conflict.current.text == "два"
    else:
        raise AssertionError("устаревшая версия прошла")
