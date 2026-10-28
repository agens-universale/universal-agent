"""Конформанс-набор контракта ``AgentStore`` v2.

Своё хранилище встраивающего проверяется так же, как наши:

    from gc_testkit.agent_store_conformance import Seed, assert_conforms

    async def test_my_store_conforms():
        await assert_conforms(lambda seed: MyStore.seeded(seed))

``make(seed)`` возвращает хранилище (или корутину, которая его вернёт), в
котором есть ресурсы ``seed``: ресурсы агента (промпт ``system``, ``L1``,
субагенты, скиллы ``system`` и ``user``) и ресурсы проекта ``project``
(``L2``, ``L3``, скиллы ``project``). У проекта ``other`` ресурсов нет.
Память обоих проектов в начале пуста. Хранилище, которое ресурсы проекта
держит в каталоге проекта (файловое), передаёт свои ``ProjectKey`` с
``root``.

Сидировать хранилище с записью ресурсов (база, S3) помогает
:func:`seed_store`.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from gc_core.agent_store import (
    AgentStore,
    MemoryConflict,
    ProjectKey,
    Prompt,
    memory_version,
)

PROJECT = ProjectKey(id="conformance-project")
OTHER_PROJECT = ProjectKey(id="conformance-other")
_FACT = "факт проекта\n"


@dataclass(frozen=True)
class Seed:
    """Ресурсы, которые хранилище отдаёт в наборе."""

    system_prompt: str = "Системный промпт набора.\nВторая строка."
    subagents: dict[str, str] = field(
        default_factory=lambda: {"b_helper.md": "B", "a_helper.md": "A"}
    )
    instructions: dict[str, str] = field(
        default_factory=lambda: {"L1": "Глобальные инструкции."}
    )
    skills: dict[str, str] = field(
        default_factory=lambda: {
            "system/alpha/SKILL.md": "---\nname: alpha\n---\nA",
            "user/beta/SKILL.md": "---\nname: beta\n---\nB",
            "user/beta/references/r.md": "R",
        }
    )
    project_instructions: dict[str, str] = field(
        default_factory=lambda: {
            "L2": "Инструкции .gc проекта.",
            "L3": "AGENTS.md проекта.",
        }
    )
    project_skills: dict[str, str] = field(
        default_factory=lambda: {
            "project/gamma/SKILL.md": "---\nname: gamma\n---\nG"
        }
    )


class _Writer(Protocol):
    async def put_resource(
        self,
        kind: Any,
        name: str,
        data: bytes,
        *,
        project: str | None = None,
        source: str | None = None,
    ) -> None: ...


async def seed_store(
    writer: _Writer, seed: Seed, *, project: ProjectKey = PROJECT
) -> None:
    """Записать ``seed`` в хранилище с записью ресурсов (``ResourceWriter``
    хранилищ ``gc-stores``)."""
    resources = [
        ("prompt", Prompt.SYSTEM.value, seed.system_prompt, None),
        *(("subagent", n, t, None) for n, t in seed.subagents.items()),
        *(("instructions", n, t, None) for n, t in seed.instructions.items()),
        *(("skill", n, t, None) for n, t in seed.skills.items()),
        *(
            ("instructions", n, t, project.id)
            for n, t in seed.project_instructions.items()
        ),
        *(("skill", n, t, project.id) for n, t in seed.project_skills.items()),
    ]
    for kind, name, text, owner in resources:
        await writer.put_resource(
            kind, name, text.encode("utf-8"), project=owner
        )


async def assert_conforms(
    make: Callable[[Seed], AgentStore | Awaitable[AgentStore]],
    *,
    project: ProjectKey = PROJECT,
    other: ProjectKey = OTHER_PROJECT,
) -> None:
    seed = Seed()
    for check in (
        _check_read,
        _check_list,
        _check_instructions,
        _check_project_required,
        _check_skill_tree,
        _check_memory_round_trip,
        _check_memory_conflict,
        _check_blank_memory_has_no_version,
        _check_written_version_is_read_version,
        _check_memory_per_project,
    ):
        store = make(seed)
        if inspect.isawaitable(store):
            store = await store
        try:
            await check(store, seed, project, other)
        finally:
            close = getattr(store, "close", None)
            if inspect.iscoroutinefunction(close):
                await close()


async def _check_read(store: AgentStore, seed: Seed, *_projects) -> None:
    data = await store.read("prompt", Prompt.SYSTEM)
    assert data == seed.system_prompt.encode("utf-8"), data
    assert await store.read("prompt", Prompt.SPECIAL_INSTRUCTIONS) is None
    assert isinstance(store.label("prompt", Prompt.SYSTEM), str)


async def _check_list(store: AgentStore, seed: Seed, *_projects) -> None:
    listed = await store.list("subagent")
    assert [e.name for e in listed] == sorted(seed.subagents), listed
    assert {e.type for e in listed} == {"file"}
    for name, text in seed.subagents.items():
        assert await store.read("subagent", name) == text.encode("utf-8")


async def _check_memory_round_trip(
    store: AgentStore, _seed: Seed, project: ProjectKey, _other
) -> None:
    assert await store.read_memory(project) is None
    written = await store.write_memory(project, _FACT, expected_version=None)
    assert written.version == memory_version(_FACT)
    read = await store.read_memory(project)
    assert read is not None
    assert (read.text, read.version) == (_FACT, written.version)


async def _check_memory_conflict(
    store: AgentStore, _seed: Seed, project: ProjectKey, _other
) -> None:
    first = await store.write_memory(project, "раз", expected_version=None)
    # Второй пишущий читал до первой записи — его правка не затирает её.
    try:
        await store.write_memory(project, "два", expected_version=None)
    except MemoryConflict as conflict:
        assert conflict.current is not None
        assert conflict.current.version == first.version
    else:
        raise AssertionError("запись по устаревшей версии прошла")
    second = await store.write_memory(
        project, "два", expected_version=first.version
    )
    assert (await store.read_memory(project)).version == second.version


async def _check_blank_memory_has_no_version(
    store: AgentStore, _seed: Seed, project: ProjectKey, _other
) -> None:
    blank = await store.write_memory(project, "  \n", expected_version=None)
    assert blank.version is None
    await store.write_memory(project, "снова", expected_version=None)


async def _check_written_version_is_read_version(
    store: AgentStore, _seed: Seed, project: ProjectKey, _other
) -> None:
    """Версия из ``write_memory`` — та же, что у следующего чтения, даже если
    хранилище нормализует текст (переводы строк): по ней пишут дальше."""
    written = await store.write_memory(
        project, "строка\r\nещё\rконец", expected_version=None
    )
    read = await store.read_memory(project)
    assert read is not None and read.version == written.version, (
        written,
        read,
    )
    await store.write_memory(
        project, "дальше", expected_version=written.version
    )


async def _check_instructions(
    store: AgentStore, seed: Seed, project: ProjectKey, other: ProjectKey
) -> None:
    assert await store.read("instructions", "L1") == (
        seed.instructions["L1"].encode("utf-8")
    )
    for name, text in seed.project_instructions.items():
        got = await store.read("instructions", name, project=project)
        assert got == text.encode("utf-8"), (name, got)
        assert await store.read("instructions", name, project=other) is None
    label = store.label("instructions", "L3", project=project)
    assert isinstance(label, str) and label


async def _check_project_required(store: AgentStore, *_rest) -> None:
    """Ресурс проекта без проекта — ошибка вызывающего, а не «нет ресурса»."""
    for kind, name in (
        ("instructions", "L2"),
        ("instructions", "L3"),
        ("skill", "project/gamma/SKILL.md"),
    ):
        try:
            await store.read(kind, name)
        except ValueError:
            continue
        raise AssertionError(f"{kind}:{name} без проекта прочитан")
    try:
        await store.list("skill", "project")
    except ValueError:
        return
    raise AssertionError("слой скиллов project без проекта перечислен")


async def _check_skill_tree(
    store: AgentStore, _seed: Seed, project: ProjectKey, other: ProjectKey
) -> None:
    assert [e.name for e in await store.list("skill", "user")] == ["beta"]
    beta = await store.list("skill", "user/beta")
    assert [(e.name, e.type) for e in beta] == [
        ("SKILL.md", "file"),
        ("references", "dir"),
    ], beta
    assert await store.read("skill", "user/beta/references/r.md") == b"R"
    listed = await store.list("skill", "project", project=project)
    assert [e.name for e in listed] == ["gamma"], listed
    try:
        foreign = await store.list("skill", "project", project=other)
    except (FileNotFoundError, NotADirectoryError):
        foreign = []
    assert foreign == [], "чужой проект видит скиллы проекта"


async def _check_memory_per_project(
    store: AgentStore, _seed: Seed, project: ProjectKey, other: ProjectKey
) -> None:
    await store.write_memory(project, "раз", expected_version=None)
    assert await store.read_memory(other) is None
