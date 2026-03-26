from pathlib import Path

from thread_store import reset_thread_files


def test_reset_thread_files_deletes_only_target_thread_files(tmp_path: Path) -> None:
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    target_data = threads_dir / "data_cli_demo-thread.jsonl"
    target_meta = threads_dir / "meta_cli_demo-thread.json"
    other_data = threads_dir / "data_cli_other-thread.jsonl"
    other_meta = threads_dir / "meta_cli_other-thread.json"

    target_data.write_text("x\n", encoding="utf-8")
    target_meta.write_text("{}", encoding="utf-8")
    other_data.write_text("y\n", encoding="utf-8")
    other_meta.write_text("{}", encoding="utf-8")

    deleted = reset_thread_files(str(threads_dir), system_id="cli", thread_id="demo-thread")

    assert str(target_data) in deleted
    assert str(target_meta) in deleted
    assert not target_data.exists()
    assert not target_meta.exists()
    assert other_data.exists()
    assert other_meta.exists()


def test_reset_thread_files_deletes_legacy_thread_files(tmp_path: Path) -> None:
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    legacy_data = threads_dir / "data_legacy-1.jsonl"
    legacy_meta = threads_dir / "meta_legacy-1.json"
    legacy_data.write_text("x\n", encoding="utf-8")
    legacy_meta.write_text("{}", encoding="utf-8")

    deleted = reset_thread_files(str(threads_dir), system_id="cli", thread_id="legacy-1")

    assert str(legacy_data) in deleted
    assert str(legacy_meta) in deleted
    assert not legacy_data.exists()
    assert not legacy_meta.exists()
