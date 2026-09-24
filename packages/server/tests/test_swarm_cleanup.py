"""Тесты для `gc_server.swarm.cleanup` (M6/3.1).

До разбиения на пакеты код жил в `scripts/cleanup_swarm_threads.py` и в
колёса не ехал — запустить его из установки было нечем. Переезд внутрь
пакета сделал команду доступной пользователю, и вместе с ней приехала
опечатка в `main()`, которую никто не мог встретить раньше.

Покрывает TTL-логику удаления swarm_threads + sandboxes + orphan
sandboxes; --dry-run; обработку отсутствующих/невалидных meta.
"""

import json
import os
import time
from datetime import UTC, datetime, timedelta

# Обычный импорт: чистка стала модулем пакета. Раньше файл лежал в scripts/ и
# грузился через spec_from_file_location по пути от корня репозитория — а
# scripts/ не попадал ни в одно колесо, поэтому в установленном профиле такой
# чистки не существовало вовсе.
from gc_server.swarm import cleanup as cleanup_mod


def _make_meta(updated_at: datetime) -> dict:
    return {
        "system": "g",
        "task_id": "t",
        "updated_at": updated_at.isoformat(),
        "swarm_meta": {
            "id": "g_t",
            "status": "completed",
            "finished_at": updated_at.isoformat(),
        },
    }


def _setup_pair(tmp_path, sid: str, age_hours: float):
    """Создать пару threads/<sid> + sandboxes/<sid> с meta.json возрастом age_hours."""
    threads = tmp_path / "threads"
    sandbox = tmp_path / "sandboxes"
    threads.mkdir(exist_ok=True)
    sandbox.mkdir(exist_ok=True)

    t_dir = threads / sid
    t_dir.mkdir()
    s_dir = sandbox / sid
    s_dir.mkdir()

    updated_at = datetime.now(UTC) - timedelta(hours=age_hours)
    meta = _make_meta(updated_at)
    (t_dir / "meta.json").write_text(json.dumps(meta))
    (s_dir / "scratch.txt").write_text("data")
    # sandbox чистится по СВОЕМУ mtime (расцеплено с threads) — старим его тоже.
    old_time = time.time() - age_hours * 3600
    os.utime(s_dir, (old_time, old_time))
    return str(threads), str(sandbox)


def test_removes_old_threads_and_matching_sandboxes(tmp_path):
    threads, sandbox = _setup_pair(tmp_path, "old_one", age_hours=48)
    stats = cleanup_mod.cleanup_threads(threads, sandbox, ttl_hours=24)
    assert stats["threads_removed"] == 1
    assert stats["sandboxes_removed"] == 1
    assert stats["kept"] == 0
    # Реально удалены.
    assert not os.path.exists(os.path.join(threads, "old_one"))
    assert not os.path.exists(os.path.join(sandbox, "old_one"))


def test_keeps_fresh_threads(tmp_path):
    threads, sandbox = _setup_pair(tmp_path, "fresh", age_hours=1)
    stats = cleanup_mod.cleanup_threads(threads, sandbox, ttl_hours=24)
    assert stats["threads_removed"] == 0
    assert stats["kept"] == 1
    assert os.path.exists(os.path.join(threads, "fresh"))


def test_dry_run_does_not_delete(tmp_path):
    threads, sandbox = _setup_pair(tmp_path, "drytest", age_hours=48)
    stats = cleanup_mod.cleanup_threads(
        threads,
        sandbox,
        ttl_hours=24,
        dry_run=True,
    )
    assert stats["threads_removed"] == 1  # counted as removed
    # Но физически НЕ удалено.
    assert os.path.exists(os.path.join(threads, "drytest"))
    assert os.path.exists(os.path.join(sandbox, "drytest"))


def test_orphan_sandbox_removed(tmp_path):
    """Sandbox без соответствующих threads/<id>/ → удаляется."""
    threads = tmp_path / "threads"
    threads.mkdir()
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir()
    # Только sandbox, без threads.
    orphan = sandbox / "orphan_one"
    orphan.mkdir()
    (orphan / "stuff").write_text("x")
    # Сделать его «старым» — установить mtime.
    old_time = time.time() - 48 * 3600
    os.utime(orphan, (old_time, old_time))

    stats = cleanup_mod.cleanup_threads(
        str(threads), str(sandbox), ttl_hours=24
    )
    assert stats["sandboxes_removed"] == 1
    assert not os.path.exists(orphan)


def test_orphan_sandbox_kept_when_fresh(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir()
    fresh_orphan = sandbox / "fresh_orphan"
    fresh_orphan.mkdir()

    stats = cleanup_mod.cleanup_threads(
        str(threads), str(sandbox), ttl_hours=24
    )
    assert stats["sandboxes_removed"] == 0
    assert os.path.exists(fresh_orphan)


def test_missing_meta_uses_mtime_fallback(tmp_path):
    """Если meta.json нет — используется mtime каталога."""
    threads = tmp_path / "threads"
    threads.mkdir()
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir()
    no_meta = threads / "no_meta"
    no_meta.mkdir()
    old_time = time.time() - 48 * 3600
    os.utime(no_meta, (old_time, old_time))

    stats = cleanup_mod.cleanup_threads(
        str(threads), str(sandbox), ttl_hours=24
    )
    assert stats["threads_removed"] == 1


def test_corrupted_meta_uses_mtime_fallback(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir()
    corrupted = threads / "corrupt"
    corrupted.mkdir()
    (corrupted / "meta.json").write_text("{not valid")
    old_time = time.time() - 48 * 3600
    os.utime(corrupted, (old_time, old_time))

    stats = cleanup_mod.cleanup_threads(
        str(threads), str(sandbox), ttl_hours=24
    )
    assert stats["threads_removed"] == 1


def test_missing_threads_dir_returns_zero_stats(tmp_path):
    stats = cleanup_mod.cleanup_threads(
        str(tmp_path / "nope"),
        str(tmp_path / "nope_sb"),
        ttl_hours=24,
    )
    assert stats["threads_removed"] == 0
    assert stats["kept"] == 0


def test_parse_iso_datetime_invalid_returns_none():
    assert cleanup_mod._parse_iso_datetime("not a date") is None
    assert cleanup_mod._parse_iso_datetime("") is None
    assert cleanup_mod._parse_iso_datetime(None) is None


def test_meta_timestamp_uses_latest_field(tmp_path):
    """Берём самый поздний из updated_at/finished_at/created_at."""
    t = tmp_path / "t"
    t.mkdir()
    meta = {
        "updated_at": "2026-06-04T10:00:00Z",
        "swarm_meta": {
            "finished_at": "2026-06-04T11:00:00Z",  # позже
            "created_at": "2026-06-04T09:00:00Z",
        },
    }
    (t / "meta.json").write_text(json.dumps(meta))
    ts = cleanup_mod._meta_timestamp(str(t / "meta.json"))
    assert ts.isoformat().startswith("2026-06-04T11:00:00")
