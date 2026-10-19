"""Ход агента, у которого всё в базе: треды и ресурсы — в SQLite, ресурсы
залиты из пакета. Модель видит то же, что с файлами: системный промпт и
набор тулов совпадают байт в байт."""

import re

from gc_core.agent_store import FileAgentStore, Prompt
from gc_core.aio import AsyncAgent
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_stores import sqlite
from gc_stores.sync import copy_defaults
from scripted_model import scripted_settings


async def _first_request(settings, requests_seen, **stores):
    requests_seen.clear()
    async with await AsyncAgent.create(settings, **stores) as agent:
        await agent.run("Сделай демо", thread_id="demo", system="cli")
    return requests_seen[0]


def _workspace(tmp_path, monkeypatch) -> None:
    """Пустой рабочий каталог (его листинг — в системном промпте); данные
    хранилищ — вне его."""
    workspace = tmp_path / "workspace"
    if workspace.exists():
        for child in workspace.iterdir():
            child.unlink()
    workspace.mkdir(exist_ok=True)
    monkeypatch.chdir(workspace)


def _without_locations(text: str) -> str:
    """``<location>`` скилла — где он лежит у хранилища: путь на диске у
    файлового, путь в базе у своего; остальное совпадает байт в байт."""
    return re.sub(r"<location>[^<]*</location>", "<location/>", text)


async def test_agent_sees_the_same_prompt_and_tools(
    tmp_path, monkeypatch, requests_seen
):
    settings = scripted_settings()
    _workspace(tmp_path, monkeypatch)
    # Эталон — файлы пакета (то, что заливает --with-defaults).
    on_files = await _first_request(
        settings,
        requests_seen,
        thread_store=FileThreadStore(str(tmp_path / "threads")),
        agent_store=FileAgentStore(),
    )
    _workspace(tmp_path, monkeypatch)
    stores = sqlite.open_stores(tmp_path / "agent.db", agent_id="a")
    await copy_defaults(stores.agent)
    try:
        in_database = await _first_request(
            settings, requests_seen, stores=stores
        )
    finally:
        await stores.aclose()
    assert _without_locations(
        in_database["messages"][0]["content"]
    ) == _without_locations(on_files["messages"][0]["content"])
    assert in_database["tools"] == on_files["tools"]


async def test_system_prompt_comes_from_the_database(
    tmp_path, monkeypatch, requests_seen
):
    monkeypatch.chdir(tmp_path)
    settings = scripted_settings()
    stores = sqlite.open_stores(tmp_path / "agent.db", agent_id="a")
    await copy_defaults(stores.agent)
    await stores.agent.put_resource(
        "prompt", Prompt.SYSTEM, "Свой системный промпт из базы.".encode()
    )
    try:
        request = await _first_request(settings, requests_seen, stores=stores)
    finally:
        await stores.aclose()
    assert (
        "Свой системный промпт из базы." in request["messages"][0]["content"]
    )
    threads = sqlite.SQLiteThreadStore(tmp_path / "agent.db")
    record = await threads.get_thread(ThreadKey(system="cli", thread="demo"))
    assert record is not None and record.info.messages_count, "тред — в базе"
