"""Обслуживание FileThreadStore: ``sweep_orphans`` удаляет устаревшие архивы
сжатия и meta старых форматов, но не живые треды; удалённый архив уходит и
из кеша архивов треда."""

import asyncio
import json
from datetime import UTC, datetime

from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads

FUTURE = datetime(2999, 1, 1, tzinfo=UTC)


def _names(root, pattern):
    return sorted(p.name for p in root.glob(pattern))


def test_old_archives_go_and_leave_the_archive_cache(tmp_path):
    fixture = LEGACY_THREADS["cli_compacted"]
    root = install_legacy_threads(tmp_path)
    store = FileThreadStore(root)
    key = ThreadKey(system=fixture.system, thread=fixture.thread_id)
    assert len(asyncio.run(store.get_thread(key)).archives) == fixture.archives
    live = _names(root, "*_" + fixture.thread_id + ".json*")

    removed_meta, removed_data = store.sweep_orphans(FUTURE)

    assert (removed_meta, removed_data) == (fixture.archives, fixture.archives)
    assert _names(root, "*__pre_summary__*") == []
    assert _names(root, "*_" + fixture.thread_id + ".json*") == live
    assert asyncio.run(store.get_thread(key)).archives == []
    assert asyncio.run(FileThreadStore(root).get_thread(key)).archives == []


def test_dry_run_counts_without_removing(tmp_path):
    (tmp_path / "meta_legacy1.json").write_text(
        json.dumps({"updated_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    (tmp_path / "data_legacy1.jsonl").write_text("{}\n", encoding="utf-8")
    store = FileThreadStore(tmp_path)

    assert store.sweep_orphans(FUTURE, dry_run=True) == (1, 1)
    assert (tmp_path / "meta_legacy1.json").exists()
    assert store.sweep_orphans(FUTURE) == (1, 1)
    assert not (tmp_path / "data_legacy1.jsonl").exists()


def test_fresh_and_broken_meta_are_kept(tmp_path):
    (tmp_path / "meta_fresh.json").write_text(
        json.dumps({"updated_at": "2999-06-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    (tmp_path / "meta_broken.json").write_text("{не json", encoding="utf-8")

    assert FileThreadStore(tmp_path).sweep_orphans(FUTURE) == (0, 0)
    assert _names(tmp_path, "meta_*") == [
        "meta_broken.json",
        "meta_fresh.json",
    ]
