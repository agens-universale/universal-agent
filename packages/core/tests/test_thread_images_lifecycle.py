"""Картинки треда живут столько же, сколько тред (спека §3).

Удаление треда (им же идёт сброс `-n`) сносит `images/<sys>_<tid>_*` — и
только их. Компакция картинки не трогает: архив истории ссылается на те же
файлы.
"""

import asyncio
import io
import os

from PIL import Image

from gc_core.attachments.model import ORIGIN_USER, user_label
from gc_core.attachments.normalize import normalize_image
from gc_core.attachments.store import IMAGES_DIRNAME, ImageStore
from gc_core.thread_store import ThreadSession
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey


def delete_thread(threads_dir: str, system: str, thread: str) -> None:
    """Удаление треда хранилищем — так удаляют сессии TUI и уборщик сервера."""
    key = ThreadKey(system=system, thread=thread)
    asyncio.run(FileThreadStore(threads_dir).delete(key, op_id="delete"))


def _png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def _snap(threads_dir: str, thread_id: str, color: str):
    store = ImageStore.for_threads_dir(threads_dir)
    return store.snapshot(
        normalize_image(_png(color)),
        "cli",
        thread_id,
        origin=ORIGIN_USER,
        label=user_label(1),
        source="shot.png",
    )


def test_delete_thread_removes_its_images_only(tmp_path):
    d = str(tmp_path)
    own = _snap(d, "t1", "red")
    other = _snap(d, "t2", "blue")

    delete_thread(d, "cli", "t1")

    assert not os.path.exists(own.path)
    assert os.path.exists(other.path)


def test_system_id_is_normalized_like_the_thread_files(tmp_path):
    """`CLI` и `cli` — один тред: файлы истории санитизируются, картинки — так же."""
    d = str(tmp_path)
    own = _snap(d, "t1", "red")

    delete_thread(d, "CLI", "t1")

    assert not os.path.exists(own.path)


def test_deleting_a_thread_without_images_creates_no_images_dir(tmp_path):
    d = str(tmp_path)

    delete_thread(d, "cli", "t1")

    assert not os.path.exists(os.path.join(d, IMAGES_DIRNAME))


def test_thread_cleanup_continues_after_one_image_cannot_be_deleted(
    tmp_path, monkeypatch, caplog
):
    d = str(tmp_path)
    blocked = _snap(d, "t1", "red")
    removable = _snap(d, "t1", "blue")
    other = _snap(d, "t2", "green")
    history = tmp_path / "data_cli_t1.jsonl"
    history.write_text('{"role":"user","content":"что тут?"}\n')
    remove = os.remove

    def remove_unless_blocked(path):
        if path == blocked.path:
            raise PermissionError("изображение занято другим процессом")
        remove(path)

    monkeypatch.setattr(os, "remove", remove_unless_blocked)

    delete_thread(d, "cli", "t1")

    assert not history.exists()
    assert os.path.exists(blocked.path)
    assert not os.path.exists(removable.path)
    assert os.path.exists(other.path)
    assert blocked.path in caplog.text


def test_thread_cleanup_logs_an_unreadable_images_directory(
    tmp_path, monkeypatch, caplog
):
    d = str(tmp_path)
    own = _snap(d, "t1", "red")
    images_dir = os.path.join(d, IMAGES_DIRNAME)
    listdir = os.listdir

    def list_unless_images(path):
        if path == images_dir:
            raise PermissionError("каталог изображений недоступен")
        return listdir(path)

    monkeypatch.setattr(os, "listdir", list_unless_images)

    delete_thread(d, "cli", "t1")

    assert os.path.exists(own.path)
    assert images_dir in caplog.text


async def test_compaction_keeps_the_images(tmp_path):
    d = str(tmp_path)
    own = _snap(d, "t1", "red")
    ts = await ThreadSession.afrom_config(
        settings=None,
        system_id="cli",
        thread_id="t1",
        store=FileThreadStore(d),
    )
    await ts.apersist_batch(
        [
            {
                "role": "user",
                "content": "[Изображение #1] что тут?",
                "id": "m1",
                "attachments": [own.to_dict()],
            }
        ],
        previous_response_id=None,
    )

    await ts.aarchive_and_compact_summary_payload(
        {"summary_message_content": "кратко", "dialog_steps_offset": 0}
    )

    assert os.path.exists(own.path)
