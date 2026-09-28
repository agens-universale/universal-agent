import json
from pathlib import Path

from gc_core.thread_store import (
    edits_journal_path,
    load_thread_messages,
    reset_thread_files,
)


def test_reset_thread_files_deletes_only_target_thread_files(
    tmp_path: Path,
) -> None:
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    target_data = threads_dir / "data_cli_demo-thread.jsonl"
    target_meta = threads_dir / "meta_cli_demo-thread.json"
    target_lock = threads_dir / "locks" / "lock_cli_demo-thread.json"
    other_data = threads_dir / "data_cli_other-thread.jsonl"
    other_meta = threads_dir / "meta_cli_other-thread.json"

    (threads_dir / "locks").mkdir()
    target_data.write_text("x\n", encoding="utf-8")
    target_meta.write_text("{}", encoding="utf-8")
    target_lock.write_text("{}", encoding="utf-8")
    other_data.write_text("y\n", encoding="utf-8")
    other_meta.write_text("{}", encoding="utf-8")

    deleted = reset_thread_files(
        str(threads_dir),
        system_id="cli",
        thread_id="demo-thread",
        launch_root=str(tmp_path),
    )

    assert str(target_data) in deleted
    assert str(target_meta) in deleted
    assert str(target_lock) in deleted
    assert not target_data.exists()
    assert not target_meta.exists()
    assert not target_lock.exists()
    assert other_data.exists()
    assert other_meta.exists()


def test_reset_thread_files_deletes_edits_journal_lock(tmp_path: Path) -> None:
    """`file_lock` (`gc_core.utils.file_lock`) кладёт межпроцессный лок
    журнала правок рядом как `<путь>.lock` и его не удаляет сам — лок живёт,
    пока живёт журнал. Сброс сносит журнал, но обязан снести и лок: иначе
    осиротевший `.lock`-файл остаётся в `threads_dir` навсегда (находка ревью
    про сиротские локи журнала)."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    journal = edits_journal_path(str(threads_dir), "cli", "demo-thread")
    journal.write_text("", encoding="utf-8")
    journal_lock = Path(str(journal) + ".lock")
    journal_lock.write_bytes(b"")

    deleted = reset_thread_files(
        str(threads_dir),
        system_id="cli",
        thread_id="demo-thread",
        launch_root=str(tmp_path),
    )

    assert str(journal) in deleted
    assert str(journal_lock) in deleted
    assert not journal.exists()
    assert not journal_lock.exists()


def test_reset_thread_files_deletes_legacy_thread_files(
    tmp_path: Path,
) -> None:
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    legacy_data = threads_dir / "data_legacy-1.jsonl"
    legacy_meta = threads_dir / "meta_legacy-1.json"
    legacy_data.write_text("x\n", encoding="utf-8")
    legacy_meta.write_text("{}", encoding="utf-8")

    deleted = reset_thread_files(
        str(threads_dir),
        system_id="cli",
        thread_id="legacy-1",
        launch_root=str(tmp_path),
    )

    assert str(legacy_data) in deleted
    assert str(legacy_meta) in deleted
    assert not legacy_data.exists()
    assert not legacy_meta.exists()


def test_load_thread_messages_accepts_double_encoded_jsonl_rows(
    tmp_path: Path,
) -> None:
    """Строка как json.dumps(json.dumps(record)) — нештатно, но загружаем без падения."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    record = {
        "type": "message",
        "thread_id": "t1",
        "system": "cli",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "payload": {"role": "user", "content": "hi", "id": "m1"},
    }
    line = (
        json.dumps(json.dumps(record, ensure_ascii=False), ensure_ascii=False)
        + "\n"
    )
    (threads_dir / "data_cli_t1.jsonl").write_text(line, encoding="utf-8")

    messages, keys, max_seq = load_thread_messages(
        str(threads_dir), "cli", "t1"
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert max_seq == 1


def test_reset_survives_a_lock_held_by_another_process(tmp_path: Path) -> None:
    """Захваченный лок журнала не имеет права сорвать сброс.

    `file_lock` держит дескриптор `<журнал>.lock` весь захват, и на Windows
    `os.remove` по нему даёт `PermissionError`. Лок — первый в списке файл,
    который чужой процесс держит открытым ПО СВОЕЙ ПРИРОДЕ: раньше цикл
    удаления был голый, и сброс падал трейсбеком посреди работы — часть файлов
    снесена, а уборка снимков стоит ПОСЛЕ цикла и не выполнялась вовсе.
    """
    from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
    from gc_core.utils.file_lock import file_lock

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    journal = edits_journal_path(str(threads_dir), "cli", "demo-thread")
    journal.write_text("", encoding="utf-8")
    data = threads_dir / "data_cli_demo-thread.jsonl"
    data.write_text("x\n", encoding="utf-8")

    store = SnapshotStore(snapshots_root(str(tmp_path)), "cli_demo-thread")
    store.capture_session("a.py", b"base")

    with file_lock(journal, timeout=5.0):
        deleted = reset_thread_files(
            str(threads_dir),
            system_id="cli",
            thread_id="demo-thread",
            launch_root=str(tmp_path),
        )

    assert str(data) in deleted
    assert not data.exists()
    assert store.session_baseline("a.py") is None


def test_reset_keeps_going_when_one_file_refuses_to_die(
    tmp_path: Path, monkeypatch
) -> None:
    """Отказ удаления ОДНОГО файла не срывает сброс и не мешает уборке снимков.

    Проверка честная на любой ОС: отказ имитируется на конкретном пути, а не
    ждёт от платформы нужной семантики блокировок.
    """
    import os as _os

    from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    journal = edits_journal_path(str(threads_dir), "cli", "demo-thread")
    journal.write_text("", encoding="utf-8")
    journal_lock = Path(str(journal) + ".lock")
    journal_lock.write_bytes(b"")
    data = threads_dir / "data_cli_demo-thread.jsonl"
    data.write_text("x\n", encoding="utf-8")

    store = SnapshotStore(snapshots_root(str(tmp_path)), "cli_demo-thread")
    store.capture_session("a.py", b"base")

    real_remove = _os.remove

    def stubborn_remove(path, *args, **kwargs):
        if str(path) == str(journal_lock):
            raise PermissionError(22, "файл занят другим процессом")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(_os, "remove", stubborn_remove)

    deleted = reset_thread_files(
        str(threads_dir),
        system_id="cli",
        thread_id="demo-thread",
        launch_root=str(tmp_path),
    )

    assert str(data) in deleted
    assert str(journal) in deleted
    assert str(journal_lock) not in deleted, "неудалённый файл не «удалён»"
    assert journal_lock.exists()
    assert store.session_baseline("a.py") is None
