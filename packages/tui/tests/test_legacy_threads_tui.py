"""TUI видит треды формата develop (спека хранилищ v2, 9.4).

На этапе 3 FileTaskStore уступает место фасаду Threads; ожидания —
из manifest.json фикстур.
"""

from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads
from gc_tui.services.stores.file_store import FileTaskStore


def test_tui_lists_legacy_session_with_title(tmp_path):
    fixture = LEGACY_THREADS["tui_session"]
    threads = install_legacy_threads(tmp_path)
    tasks = FileTaskStore(str(threads), "tui").list_tasks()
    match = [t for t in tasks if t.thread_id == fixture.thread_id]
    assert match, "старая сессия TUI не видна в списке"
    assert (match[0].tui_meta or {}).get("title") == fixture.title


def test_tui_shows_full_history_of_compacted_thread(tmp_path):
    fixture = LEGACY_THREADS["cli_compacted"]
    threads = install_legacy_threads(tmp_path)
    segments = FileTaskStore(str(threads), "cli").read_segments(
        fixture.thread_id, "cli"
    )
    assert len(segments) == fixture.archives + 1
    assert any(s["compacted_by"] for s in segments)
