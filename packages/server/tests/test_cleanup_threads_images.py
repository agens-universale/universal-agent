"""Серверный уборщик удаляет тред целиком — тем же путём, что удаление сессии в TUI.

Раньше он удалял только meta и data своим перечнем файлов: картинки треда
(`images/`), сайдкар субагентов и архивы компакции оставались навсегда.
"""

import io
import json
import os

from PIL import Image

from gc_core.attachments.model import ORIGIN_USER, user_label
from gc_core.attachments.normalize import normalize_image
from gc_core.attachments.store import ImageStore
from gc_server.cleanup_threads import cleanup_threads

OLD = "2020-01-01T00:00:00+00:00"


def _png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def _thread(threads, tid: str, updated_at: str) -> None:
    (threads / f"meta_srv_{tid}.json").write_text(
        json.dumps(
            {"thread_id": tid, "system": "srv", "updated_at": updated_at}
        ),
        encoding="utf-8",
    )
    (threads / f"data_srv_{tid}.jsonl").write_text("{}\n", encoding="utf-8")


def _snap(threads, tid: str, color: str):
    return ImageStore.for_threads_dir(str(threads)).snapshot(
        normalize_image(_png(color)),
        "srv",
        tid,
        origin=ORIGIN_USER,
        label=user_label(1),
        source="shot.png",
    )


def test_old_thread_goes_with_its_images_sidecar_and_archive(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    _thread(threads, "t1", OLD)
    (threads / "data_subagents_srv_t1.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    (threads / "data_srv_t1__pre_summary__20200101_000000.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    own = _snap(threads, "t1", "red")
    other = _snap(threads, "t2", "blue")

    stats = cleanup_threads(str(threads), ttl_days=7)

    assert not (threads / "meta_srv_t1.json").exists()
    assert not (threads / "data_srv_t1.jsonl").exists()
    assert not (threads / "data_subagents_srv_t1.jsonl").exists()
    assert not (
        threads / "data_srv_t1__pre_summary__20200101_000000.jsonl"
    ).exists()
    assert not os.path.exists(own.path)
    assert os.path.exists(other.path)
    assert stats["removed_meta"] == 1
    assert stats["removed_data"] == 1


def test_fresh_thread_keeps_its_images(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    _thread(threads, "t1", "2999-01-01T00:00:00+00:00")
    own = _snap(threads, "t1", "red")

    cleanup_threads(str(threads), ttl_days=7)

    assert os.path.exists(own.path)
    assert (threads / "meta_srv_t1.json").exists()


def test_dry_run_deletes_nothing(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    _thread(threads, "t1", OLD)
    own = _snap(threads, "t1", "red")

    stats = cleanup_threads(str(threads), ttl_days=7, dry_run=True)

    assert os.path.exists(own.path)
    assert (threads / "meta_srv_t1.json").exists()
    assert stats["removed_meta"] == 1


FRESH = "2999-01-01T00:00:00+00:00"
ARCHIVE = "__pre_summary__20200101_000000"


def _archive(threads, tid: str) -> None:
    """Архив компакции: копия meta на момент сжатия несёт те же system/thread_id."""
    (threads / f"meta_srv_{tid}{ARCHIVE}.json").write_text(
        json.dumps({"thread_id": tid, "system": "srv", "updated_at": OLD}),
        encoding="utf-8",
    )
    (threads / f"data_srv_{tid}{ARCHIVE}.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )


def test_old_archive_of_live_thread_does_not_delete_the_thread(tmp_path):
    """Старый архив компакции живого треда удаляет только себя, не живой тред."""
    threads = tmp_path / "threads"
    threads.mkdir()
    _thread(threads, "t1", FRESH)
    _archive(threads, "t1")
    own = _snap(threads, "t1", "red")

    stats = cleanup_threads(str(threads), ttl_days=7)

    assert (threads / "meta_srv_t1.json").exists()
    assert (threads / "data_srv_t1.jsonl").exists()
    assert os.path.exists(own.path)
    assert not (threads / f"meta_srv_t1{ARCHIVE}.json").exists()
    assert not (threads / f"data_srv_t1{ARCHIVE}.jsonl").exists()
    assert stats["removed_meta"] == 1
    assert stats["removed_data"] == 1
    assert stats["errors"] == 0


def test_old_legacy_meta_does_not_delete_the_live_thread(tmp_path):
    """Legacy `meta_<tid>.json` с system/thread_id внутри удаляется по имени файла."""
    threads = tmp_path / "threads"
    threads.mkdir()
    _thread(threads, "t1", FRESH)
    (threads / "meta_t1.json").write_text(
        json.dumps({"thread_id": "t1", "system": "srv", "updated_at": OLD}),
        encoding="utf-8",
    )
    (threads / "data_t1.jsonl").write_text("{}\n", encoding="utf-8")
    own = _snap(threads, "t1", "red")

    cleanup_threads(str(threads), ttl_days=7)

    assert (threads / "meta_srv_t1.json").exists()
    assert (threads / "data_srv_t1.jsonl").exists()
    assert os.path.exists(own.path)
    assert not (threads / "meta_t1.json").exists()
    assert not (threads / "data_t1.jsonl").exists()


def test_old_thread_with_meta_archive_counts_no_errors(tmp_path):
    """Архив, удалённый вместе со своим тредом раньше в том же проходе, — не ошибка."""
    threads = tmp_path / "threads"
    threads.mkdir()
    _thread(threads, "t1", OLD)
    _archive(threads, "t1")

    stats = cleanup_threads(str(threads), ttl_days=7)

    # `.store` — служебный каталог хранилища v2: там остаётся надгробие
    # удалённого треда (его поколение), чтобы курсор читателя заметил
    # пересоздание треда с тем же id. Файлов треда не остаётся.
    assert sorted(p.name for p in threads.iterdir()) == [".store"]
    assert not list((threads / ".store").rglob("data_*"))
    assert stats["errors"] == 0


def test_meta_without_thread_identity_is_removed_by_name(tmp_path):
    """Старая meta без `system` — прежний путь: meta и data по имени файла."""
    threads = tmp_path / "threads"
    threads.mkdir()
    (threads / "meta_legacy1.json").write_text(
        json.dumps({"updated_at": OLD}), encoding="utf-8"
    )
    (threads / "data_legacy1.jsonl").write_text("{}\n", encoding="utf-8")

    stats = cleanup_threads(str(threads), ttl_days=7)

    assert not (threads / "meta_legacy1.json").exists()
    assert not (threads / "data_legacy1.jsonl").exists()
    assert stats["removed_meta"] == 1
    assert stats["removed_data"] == 1
