"""Файл плана сессии для человека: содержимое, запись, удаление вместе с сессией."""

import os

from gc_core.persistence.plan_file import render_plan_file, write_plan_file
from gc_core.plan_store import new_plan_block
from gc_core.thread_store import ThreadSession, plan_file_path
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import SideChange, ThreadKey
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

KEY = ThreadKey(system="cli", thread="t1")
OTHER = ThreadKey(system="cli", thread="t2")


def _plan(status="proposed", mode=None):
    plan = new_plan_block("1. шаг\n2. проверка")
    plan.update(status=status, approved_mode=mode)
    plan["updated_at"] = "2026-10-13T09:12:00+00:00"
    return plan


def test_file_sits_next_to_the_session_files(tmp_path):
    path = plan_file_path(str(tmp_path), "cli", "t1")
    assert path == os.path.join(str(tmp_path), "plans", "plan_cli_t1.md")


def test_file_is_written_for_a_human():
    text = render_plan_file(_plan("approved", "edit"), "t1")
    assert text.startswith("_План сессии t1 · утверждён в edit mode · ")
    assert "утверждён в edit mode" in text
    assert "2026" in text and "октября" in text  # абсолютная дата
    assert text.rstrip().endswith("1. шаг\n2. проверка")
    assert "approved" not in text and "proposed" not in text
    assert render_plan_file(None, "t1") == ""


def test_plan_title_is_the_only_top_level_heading():
    """Служебная строка — без `#`: заголовок первого уровня в файле один,
    и это заголовок самого плана."""
    plan = new_plan_block("# Перенос модуля\n\n1. шаг\n2. проверка")
    plan["updated_at"] = "2026-10-13T09:12:00+00:00"
    text = render_plan_file(plan, "t1")
    headings = [line for line in text.splitlines() if line.startswith("# ")]
    assert headings == ["# Перенос модуля"]
    assert text.startswith("_План сессии t1 · ")


def test_write_happens_only_when_the_content_differs(tmp_path):
    path = str(tmp_path / "plans" / "p.md")
    assert write_plan_file(path, "a") is True
    assert write_plan_file(path, "a") is False  # то же содержимое
    assert write_plan_file(path, "b") is True  # новый план — поверх
    assert open(path, encoding="utf-8").read() == "b"
    assert write_plan_file(path, "") is False


async def test_file_store_exports_and_a_store_without_files_does_not(
    tmp_path,
):
    """Файл плана — возможность файлового хранилища тредов v2; у хранилищ
    без каталога (память, базы) её нет — исполнитель молча пропускает."""
    store = FileThreadStore(str(tmp_path))
    path = await store.save_plan_file(KEY, _plan())
    assert path == plan_file_path(str(tmp_path), "cli", "t1")
    assert "предложен" in open(path, encoding="utf-8").read()
    assert await store.save_plan_file(KEY, None) is None
    assert getattr(MemoryThreadStore(), "save_plan_file", None) is None


async def test_file_goes_away_with_the_session(tmp_path):
    store = FileThreadStore(str(tmp_path))
    for key in (KEY, OTHER):
        await store.append_side(key, SideChange(op_id=f"s-{key.thread}"))
    path = await store.save_plan_file(KEY, _plan())
    other = await store.save_plan_file(OTHER, _plan())
    await store.delete(KEY, op_id="d")
    assert not os.path.exists(path)
    assert os.path.exists(other)  # чужая сессия цела


async def _session(store):
    return await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="t1", store=store
    )


async def test_executor_exports_through_the_session_and_survives_a_failure(
    tmp_path,
):
    import threading
    from types import SimpleNamespace

    from gc_core.agent_executor import AgentExecutor
    from gc_core.execution.state import SharedStateData

    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    ex._thread_session = await _session(FileThreadStore(str(tmp_path)))
    state = SimpleNamespace(shared=SharedStateData(plan=_plan()))
    await ex._aexport_plan_file(state, None)
    assert os.path.exists(plan_file_path(str(tmp_path), "cli", "t1"))

    class _Broken(MemoryThreadStore):
        async def save_plan_file(self, *a):
            raise OSError("диск полон")

    ex._thread_session = await _session(_Broken())
    await ex._aexport_plan_file(state, None)  # не падает
    ex._thread_session = await _session(MemoryThreadStore())
    await ex._aexport_plan_file(state, None)  # хранилище не умеет — молча


async def test_clearing_the_plan_drops_it_and_tells_the_model(tmp_path):
    """``/plan clear`` (фасад ``Threads``): план убран из сессии, следующий
    прогон один раз сообщит модели, файл плана удалён."""
    from gc_core.execution.state import SharedStateData
    from gc_core.persistence.session_state import (
        session_with_shared,
        shared_from_session,
    )
    from gc_core.threads.facade import Threads
    from gc_core.threads.models import SessionState
    from gc_core.threads.session_writer import SessionWriter

    store = FileThreadStore(str(tmp_path))
    threads = Threads(store)
    writer = SessionWriter(store, KEY, owner="w")
    await writer.acommit(
        set_state=session_with_shared(
            SessionState(), SharedStateData(plan=_plan())
        )
    )
    await writer.arelease()
    path = await store.save_plan_file(KEY, _plan())
    assert await threads.clear_plan(KEY) is True
    record = await threads.get(KEY)
    shared = shared_from_session(record.state, record.extra)
    assert shared.plan is None and shared.plan_removed_notice is True
    assert not os.path.exists(path)
    assert await threads.clear_plan(KEY) is False  # плана уже нет
