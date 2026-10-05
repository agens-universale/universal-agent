"""Живость вложений (спека §6): какие картинки уходят модели пикселями на запрос.

Граница хода — реальная реплика человека; окно ваших картинок — LIVE_USER_TURNS
ходов вместе с текущим, снимки агента — только текущий ход; затем бюджет байт
запроса (base64) — выпадают самые старые.
"""

import os
import tempfile

from gc_core.attachments.model import (
    LIVE_USER_TURNS,
    MAX_REQUEST_IMAGE_BYTES,
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    user_label,
)
from gc_core.config_modules.images import ImagesConfig
from gc_core.message_policy.attachments_outbound import (
    compute_live_ids,
    count_live_images,
)

#: Малые потолки, чтобы счёт был виден на двух-трёх картинках.
IMAGES = ImagesConfig(
    max_per_message=2, live_user_images=3, live_tool_images=1
)
#: Файл 3 000 000 байт весит 4 000 000 в base64: две такие влезают в бюджет запроса, три — нет.
BIG = 3_000_000
_DIR = os.path.join(tempfile.gettempdir(), "gc-images")


def _id(n: int) -> str:
    return f"{n:016x}"


def _att(
    n: int,
    *,
    origin: str = ORIGIN_USER,
    size: int = 1000,
    same_as: int | None = None,
) -> dict:
    """Словарь вложения, как он лежит в записи треда. ``same_as`` — тот же файл
    (тот же id), что у картинки номер ``same_as``."""
    att_id = _id(same_as if same_as is not None else n)
    return Attachment(
        id=att_id,
        kind="image",
        mime="image/png",
        width=10,
        height=10,
        size=size,
        path=os.path.join(_DIR, f"cli_t_{att_id}.png"),
        origin=origin,
        label=user_label(n) if origin == ORIGIN_USER else TOOL_LABEL,
        source="clipboard" if origin == ORIGIN_USER else "shot.png",
        orig_width=10,
        orig_height=10,
    ).to_dict()


def _user(mid: str, text: str, *attachments: dict) -> dict:
    message = {"role": "user", "id": mid, "content": text}
    if attachments:
        message["attachments"] = list(attachments)
    return message


def _technical(mid: str, *attachments: dict) -> dict:
    return {
        "role": "user",
        "id": mid,
        "content": '<technical type="technical">Image from fs_view_image: shot.png · png 10×10</technical>',
        "attachments": list(attachments),
    }


def _assistant(mid: str) -> dict:
    return {"type": "message", "role": "assistant", "id": mid, "content": "ok"}


def test_no_attachments_nothing_is_live():
    assert (
        compute_live_ids(
            [_user("m1", "привет"), _assistant("a1")], images=IMAGES
        )
        == frozenset()
    )


def test_fresh_user_images_are_live():
    history = [
        _user("m1", "[Изображение #1] [Изображение #2]", _att(1), _att(2))
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(1), _id(2)}


def test_user_images_newest_by_count_later_attachments_of_a_message_survive():
    history = [
        _user("m1", "раз", _att(1), _att(2)),
        _assistant("a1"),
        _user("m2", "два", _att(3), _att(4)),
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(2), _id(3), _id(4)}


def test_user_image_older_than_the_turn_window_is_dead():
    """Окно LIVE_USER_TURNS включает текущий ход: картинка на ход раньше окна — неживая."""
    images = ImagesConfig(
        max_per_message=2, live_user_images=10, live_tool_images=1
    )
    history = [
        _user("m0", "за окном", _att(1)),
        _assistant("a0"),
        _user("m1", "первый ход окна", _att(2)),
    ]
    for turn in range(2, LIVE_USER_TURNS + 1):
        history += [_assistant(f"a{turn}"), _user(f"m{turn}", f"ход {turn}")]
    assert compute_live_ids(history, images=images) == {_id(2)}


def test_technical_messages_and_tool_outputs_do_not_open_a_turn():
    history = [
        _user("m1", "сделай скриншот"),
        {
            "type": "function_call",
            "id": "fc1",
            "call_id": "c1",
            "name": "fs_view_image",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "id": "fo1",
            "call_id": "c1",
            "output": '{"ok": true}',
        },
        _technical("t1", _att(7, origin=ORIGIN_TOOL)),
        {
            "role": "user",
            "id": "t2",
            "content": '<technical type="technical">loop detector notice</technical>',
        },
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(7)}


def test_tool_images_live_only_in_the_current_turn():
    history = [
        _user("m1", "раз"),
        _technical("t1", _att(7, origin=ORIGIN_TOOL)),
        _assistant("a1"),
        _user("m2", "два"),
    ]
    assert compute_live_ids(history, images=IMAGES) == frozenset()


def _call(n: int) -> list[dict]:
    """Пара «вызов → ответ» тула: вызов — ответ модели, ответ тула — нет."""
    return [
        {
            "type": "function_call",
            "id": f"fc{n}",
            "call_id": f"c{n}",
            "name": "fs_view_image",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "id": f"fo{n}",
            "call_id": f"c{n}",
            "output": '{"ok": true}',
        },
    ]


def test_mid_run_human_message_closes_the_turn_for_tool_images():
    """Реплика посреди запуска — граница хода (спека §6): снимок до неё, который
    модель уже видела (ответила после реплики), не текущий."""
    images = ImagesConfig(
        max_per_message=2, live_user_images=3, live_tool_images=3
    )
    history = [
        _user("m1", "сделай скриншот"),
        *_call(1),
        _technical("t1", _att(7, origin=ORIGIN_TOOL)),
        _user("m2", "и ещё проверь футер"),
        *_call(2),
        _technical("t2", _att(8, origin=ORIGIN_TOOL)),
    ]
    assert compute_live_ids(history, images=images) == {_id(8)}


def test_tool_image_before_a_mid_run_human_message_stays_live_until_the_model_answers():
    """Тул обещал «you will see it in the next message»: реплика, дописанная к батчу
    «ответы + картинка», не отнимает у модели снимок, который она ещё не видела."""
    history = [
        _user("m1", "сделай скриншот"),
        *_call(1),
        _technical("t1", _att(7, origin=ORIGIN_TOOL)),
        _user("m2", "и ещё проверь футер"),
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(7)}


def test_tool_image_of_the_previous_turn_dies_after_the_model_answers():
    history = [
        _user("m1", "сделай скриншот"),
        *_call(1),
        _technical("t1", _att(7, origin=ORIGIN_TOOL)),
        _user("m2", "и ещё проверь футер"),
        {
            "type": "function_call",
            "id": "fc2",
            "call_id": "c2",
            "name": "fs_read_file",
            "arguments": "{}",
        },
    ]
    assert compute_live_ids(history, images=IMAGES) == frozenset()
    history[-1] = _assistant("a1")
    assert compute_live_ids(history, images=IMAGES) == frozenset()


def test_tool_image_of_the_current_turn_stays_live_after_the_model_answers():
    """В своём ходе снимок живёт и после ответа модели (спека §6, решение 8)."""
    history = [
        _user("m1", "сделай скриншот"),
        *_call(1),
        _technical("t1", _att(7, origin=ORIGIN_TOOL)),
        *_call(2),
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(7)}


def test_tool_images_newest_by_count_later_attachment_of_a_message_survives():
    images = ImagesConfig(
        max_per_message=2, live_user_images=3, live_tool_images=2
    )
    history = [
        _user("m1", "сними три"),
        _technical(
            "t1", _att(7, origin=ORIGIN_TOOL), _att(8, origin=ORIGIN_TOOL)
        ),
        _technical("t2", _att(9, origin=ORIGIN_TOOL)),
    ]
    assert compute_live_ids(history, images=images) == {_id(8), _id(9)}


def test_byte_budget_evicts_the_oldest_live_image():
    assert 2 * 4_000_000 <= MAX_REQUEST_IMAGE_BYTES < 3 * 4_000_000
    history = [
        _user("m1", "раз", _att(1, size=BIG)),
        _assistant("a1"),
        _user("m2", "два", _att(2, size=BIG)),
        _assistant("a2"),
        _user("m3", "три", _att(3, size=BIG)),
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(2), _id(3)}


def test_byte_budget_counts_base64_not_file_size():
    """3 500 000 байт файла — 4 666 668 в base64: по файлам две влезли бы, по проводу — нет."""
    size = 3_500_000
    assert 2 * size <= MAX_REQUEST_IMAGE_BYTES < 2 * 4_666_668
    history = [
        _user("m1", "раз", _att(1, size=size)),
        _assistant("a1"),
        _user("m2", "два", _att(2, size=size)),
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(2)}


def test_byte_budget_cuts_one_message_from_its_earliest_attachment():
    images = ImagesConfig(
        max_per_message=3, live_user_images=3, live_tool_images=1
    )
    history = [
        _user(
            "m1",
            "три больших",
            _att(1, size=BIG),
            _att(2, size=BIG),
            _att(3, size=BIG),
        )
    ]
    assert compute_live_ids(history, images=images) == {_id(2), _id(3)}


def test_byte_budget_evicts_an_old_user_image_before_a_fresh_tool_image():
    history = [
        _user("m1", "раз", _att(1, size=BIG)),
        _assistant("a1"),
        _user("m2", "два", _att(2, size=BIG)),
        _technical("t1", _att(7, origin=ORIGIN_TOOL, size=BIG)),
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(2), _id(7)}


def test_same_image_in_two_messages_counts_bytes_once():
    """Один файл (один id) в двух сообщениях уходит пикселями один раз — в последнем
    вхождении (проекция): и байты бюджета считаются один раз."""
    history = [
        _user("m1", "раз", _att(1, size=BIG)),
        _assistant("a1"),
        _user(
            "m2",
            "снова она и новая",
            _att(2, size=BIG, same_as=1),
            _att(3, size=BIG),
        ),
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(1), _id(3)}


def test_byte_budget_ages_an_image_by_its_latest_place():
    """Снова показанная старая картинка — свежая: вытесняется та, что старше по
    последнему вхождению."""
    history = [
        _user("m1", "раз", _att(1, size=BIG)),
        _assistant("a1"),
        _user("m2", "два", _att(2, size=BIG)),
        _assistant("a2"),
        _user(
            "m3",
            "снова первая и третья",
            _att(4, size=BIG, same_as=1),
            _att(3, size=BIG),
        ),
    ]
    assert compute_live_ids(history, images=IMAGES) == {_id(1), _id(3)}


def test_messages_repeated_by_id_count_once():
    """Слитая последовательность «state + батч»: батч повторяет хвост state по id —
    ход не удваивается, и окно не сдвигается."""
    images = ImagesConfig(
        max_per_message=2, live_user_images=10, live_tool_images=1
    )
    history = [_user("m1", "старая, но в окне", _att(1))]
    for turn in range(2, LIVE_USER_TURNS + 1):
        history += [_assistant(f"a{turn}"), _user(f"m{turn}", f"ход {turn}")]
    batch = [history[-1]]
    assert compute_live_ids([*history, *batch], images=images) == {_id(1)}


# --- Потолок картинок модели (выученный по отказу бэкенда) ---------------------


def test_image_cap_keeps_the_newest_live_images():
    history = [
        _user("m1", "раз", _att(1)),
        _assistant("a1"),
        _user("m2", "два", _att(2), _att(3)),
    ]
    assert compute_live_ids(history, images=IMAGES, max_images=2) == {
        _id(2),
        _id(3),
    }
    assert compute_live_ids(history, images=IMAGES, max_images=1) == {_id(3)}


def test_image_cap_counts_an_image_once_like_the_wire():
    """Картинка в двух сообщениях уходит один раз — и в потолок входит один раз."""
    history = [
        _user("m1", "раз", _att(1)),
        _assistant("a1"),
        _user("m2", "снова она и новая", _att(2, same_as=1), _att(3)),
    ]
    live = compute_live_ids(history, images=IMAGES, max_images=2)
    assert live == {_id(1), _id(3)}
    assert count_live_images(history, live) == 2


def test_image_cap_of_zero_leaves_nothing_live():
    history = [_user("m1", "раз", _att(1))]
    assert (
        compute_live_ids(history, images=IMAGES, max_images=0) == frozenset()
    )


def test_count_live_images_counts_each_image_once():
    history = [
        _user("m1", "раз", _att(1)),
        _assistant("a1"),
        _user("m2", "два", _att(2, same_as=1)),
    ]
    assert count_live_images([*history, history[-1]], frozenset({_id(1)})) == 1
