"""Сквозной номер картинок треда — `meta.runtime.image_seq` (спека §4).

Фронт выдаёт следующий номер от счётчика, ядро поднимает его при записи батча.
Счётчик только растёт: компакция уносит записи в архив, но не meta.
"""

from gc_core.attachments.model import (
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    user_label,
)
from gc_core.attachments.numbering import IMAGE_SEQ_KEY, read_image_seq
from gc_core.thread_store import ThreadSession
from gc_testkit.develop_files import load_thread_meta
from gc_core.threads.file_store import FileThreadStore


async def _session(tmp_path):
    return await ThreadSession.afrom_config(
        settings=None,
        system_id="cli",
        thread_id="t1",
        store=FileThreadStore(tmp_path),
    )


def _att(n: int, *, origin: str = ORIGIN_USER) -> dict:
    label = user_label(n) if origin == ORIGIN_USER else TOOL_LABEL
    return Attachment(
        id=f"{n:016x}",
        kind="image",
        mime="image/png",
        width=64,
        height=48,
        size=100,
        path=f"/threads/images/cli_t1_{n:016x}.png",
        origin=origin,
        label=label,
        source="clipboard",
        orig_width=64,
        orig_height=48,
    ).to_dict()


def _user(msg_id: str, *numbers: int) -> dict:
    chips = " ".join(f"[Изображение #{n}]" for n in numbers)
    return {
        "role": "user",
        "content": f"{chips} вопрос",
        "id": msg_id,
        "attachments": [_att(n) for n in numbers],
    }


def _runtime(tmp_path) -> dict:
    return load_thread_meta(str(tmp_path), "cli", "t1").get("runtime") or {}


async def test_persist_raises_image_seq_to_the_largest_number_in_the_batch(
    tmp_path,
):
    ts = await _session(tmp_path)
    await ts.apersist_batch([_user("m1", 3, 5)], previous_response_id=None)

    assert read_image_seq(load_thread_meta(str(tmp_path), "cli", "t1")) == 5


async def test_image_seq_never_goes_down(tmp_path):
    ts = await _session(tmp_path)
    await ts.apersist_batch([_user("m1", 5)], previous_response_id=None)
    await ts.apersist_batch([_user("m2", 2)], previous_response_id=None)

    assert read_image_seq(load_thread_meta(str(tmp_path), "cli", "t1")) == 5


async def test_tool_images_do_not_move_the_counter(tmp_path):
    ts = await _session(tmp_path)
    await ts.apersist_batch(
        [
            {
                "role": "user",
                "content": '<technical type="technical">x</technical>',
                "id": "m1",
                "attachments": [_att(9, origin=ORIGIN_TOOL)],
            }
        ],
        previous_response_id=None,
    )

    assert IMAGE_SEQ_KEY not in _runtime(tmp_path)


async def test_thread_without_images_gets_no_counter(tmp_path):
    ts = await _session(tmp_path)
    await ts.apersist_batch(
        [{"role": "user", "content": "текст", "id": "m1"}],
        previous_response_id=None,
    )

    assert IMAGE_SEQ_KEY not in _runtime(tmp_path)


async def test_compaction_does_not_lower_image_seq(tmp_path):
    ts = await _session(tmp_path)
    await ts.apersist_batch([_user("m1", 4)], previous_response_id=None)

    await ts.aarchive_and_compact_summary_payload(
        {"summary_message_content": "кратко", "dialog_steps_offset": 0}
    )

    assert read_image_seq(load_thread_meta(str(tmp_path), "cli", "t1")) == 4
