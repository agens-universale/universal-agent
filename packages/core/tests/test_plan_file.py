"""Файл плана сессии для человека: содержимое, запись, удаление вместе с сессией."""

import os

from gc_core.persistence.plan_file import render_plan_file, write_plan_file
from gc_core.plan_store import new_plan_block
from gc_core.thread_store import (
    delete_thread,
    plan_file_path,
    reset_thread_files,
    save_thread_meta,
)
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_file_durable import DurableFileThreadStore


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


def test_plain_file_store_exports_and_the_journal_store_does_not(tmp_path):
    store = FileThreadStore(str(tmp_path))
    path = store.save_plan_file("cli", "t1", _plan())
    assert path == plan_file_path(str(tmp_path), "cli", "t1")
    assert "предложен" in open(path, encoding="utf-8").read()
    assert store.save_plan_file("cli", "t1", None) is None
    assert DurableFileThreadStore.save_plan_file is None


def test_file_goes_away_with_the_session(tmp_path):
    threads = str(tmp_path)
    store = FileThreadStore(threads)
    for remove in (delete_thread, reset_thread_files):
        save_thread_meta(threads, "cli", "t1", {})
        path = store.save_plan_file("cli", "t1", _plan())
        other = store.save_plan_file("cli", "t2", _plan())
        remove(threads, "cli", "t1")
        assert not os.path.exists(path)
        assert os.path.exists(other)  # чужая сессия цела


def test_executor_exports_through_the_store_and_survives_a_failure(tmp_path):
    import threading
    from types import SimpleNamespace

    from gc_core.agent_executor import AgentExecutor
    from gc_core.execution.state import SharedStateData

    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    store = FileThreadStore(str(tmp_path))
    ex._thread_session = SimpleNamespace(
        store=store, system_id="cli", thread_id="t1"
    )
    state = SimpleNamespace(shared=SharedStateData(plan=_plan()))
    ex._export_plan_file(state, None)
    assert os.path.exists(plan_file_path(str(tmp_path), "cli", "t1"))

    class _Broken:
        def save_plan_file(self, *a):
            raise OSError("диск полон")

    ex._thread_session = SimpleNamespace(
        store=_Broken(), system_id="cli", thread_id="t1"
    )
    ex._export_plan_file(state, None)  # не падает
    ex._thread_session = SimpleNamespace(
        store=object(), system_id="cli", thread_id="t1"
    )
    ex._export_plan_file(state, None)  # хранилище не умеет — молча
