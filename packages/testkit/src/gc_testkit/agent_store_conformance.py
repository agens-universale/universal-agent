"""Конформанс-набор контракта ``AgentStore`` v2.

Своё хранилище встраивающего проверяется так же, как наши:

    from gc_testkit.agent_store_conformance import Seed, assert_conforms

    async def test_my_store_conforms():
        await assert_conforms(lambda seed: MyStore.seeded(seed))

``make(seed)`` возвращает хранилище, в котором есть ресурсы ``seed``: промпт
``system``, определения субагентов. Память проекта ``PROJECT`` в начале пуста.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from gc_core.agent_store import (
    AgentStore,
    MemoryConflict,
    ProjectKey,
    Prompt,
    memory_version,
)

PROJECT = ProjectKey(id="conformance-project")
_FACT = "факт проекта\n"


@dataclass(frozen=True)
class Seed:
    """Ресурсы, которые хранилище отдаёт в наборе."""

    system_prompt: str = "Системный промпт набора.\nВторая строка."
    subagents: dict[str, str] = field(
        default_factory=lambda: {"b_helper.md": "B", "a_helper.md": "A"}
    )


async def assert_conforms(make: Callable[[Seed], AgentStore]) -> None:
    seed = Seed()
    for check in (
        _check_read,
        _check_list,
        _check_memory_round_trip,
        _check_memory_conflict,
        _check_blank_memory_has_no_version,
    ):
        await check(make(seed), seed)


async def _check_read(store: AgentStore, seed: Seed) -> None:
    data = await store.read("prompt", Prompt.SYSTEM)
    assert data == seed.system_prompt.encode("utf-8"), data
    assert await store.read("prompt", Prompt.SPECIAL_INSTRUCTIONS) is None
    assert isinstance(store.label("prompt", Prompt.SYSTEM), str)


async def _check_list(store: AgentStore, seed: Seed) -> None:
    listed = await store.list("subagent")
    assert [e.name for e in listed] == sorted(seed.subagents), listed
    assert {e.type for e in listed} == {"file"}
    for name, text in seed.subagents.items():
        assert await store.read("subagent", name) == text.encode("utf-8")


async def _check_memory_round_trip(store: AgentStore, _seed: Seed) -> None:
    assert await store.read_memory(PROJECT) is None
    written = await store.write_memory(PROJECT, _FACT, expected_version=None)
    assert written.version == memory_version(_FACT)
    read = await store.read_memory(PROJECT)
    assert read is not None
    assert (read.text, read.version) == (_FACT, written.version)


async def _check_memory_conflict(store: AgentStore, _seed: Seed) -> None:
    first = await store.write_memory(PROJECT, "раз", expected_version=None)
    # Второй пишущий читал до первой записи — его правка не затирает её.
    try:
        await store.write_memory(PROJECT, "два", expected_version=None)
    except MemoryConflict as conflict:
        assert conflict.current is not None
        assert conflict.current.version == first.version
    else:
        raise AssertionError("запись по устаревшей версии прошла")
    second = await store.write_memory(
        PROJECT, "два", expected_version=first.version
    )
    assert (await store.read_memory(PROJECT)).version == second.version


async def _check_blank_memory_has_no_version(
    store: AgentStore, _seed: Seed
) -> None:
    blank = await store.write_memory(PROJECT, "  \n", expected_version=None)
    assert blank.version is None
    await store.write_memory(PROJECT, "снова", expected_version=None)
