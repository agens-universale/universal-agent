"""Ответ на turn_ask_human с изображениями (спека §20.3).

Порт отвечает строкой (прежнее поведение) или HumanAnswer. Картинки ответа ядро
копирует в тред и отдаёт модели после батча; в ответ тула идёт текст с плашками.
"""

import io
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from gc_core.attachments.model import ORIGIN_USER, chip_text, user_label
from gc_core.attachments.normalize import normalize_image
from gc_core.attachments.store import ImageStore
from gc_core.execution.progress import QueueSubagentProgressSink
from gc_core.tools.turn.ask_human import (
    AskHuman,
    HumanAnswer,
    _aread_human_response,
)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, format="PNG")
    return buf.getvalue()


def _staged(store: ImageStore, n: int = 3):
    return store.stage(
        normalize_image(_png()),
        origin=ORIGIN_USER,
        label=user_label(n),
        source="clipboard",
    )


def _ctx(store, port, *, thread=True, **extra):
    ctx = SimpleNamespace(
        config_dict={}, logger=None, image_store=store, ask_human=port, **extra
    )
    if thread:
        ctx.system_id, ctx.thread_id = "cli", "t1"
    return ctx


async def _ask(ctx, state, question="Что не так на экране?"):
    return json.loads(
        await AskHuman(ctx=ctx, state=state).acall(question=question)
    )


async def test_port_may_answer_with_images():
    answer = HumanAnswer(text="вот так", attachments=())
    assert (
        await _aread_human_response(
            SimpleNamespace(ask_human=lambda q: answer), "?"
        )
        is answer
    )


async def test_answer_images_go_to_the_thread_and_after_the_batch(tmp_path):
    store = ImageStore(str(tmp_path / "images"))
    att = _staged(store)
    text = f"{chip_text(3)} кнопка съехала"
    state = SimpleNamespace(pending_tool_attachments=[])

    out = await _ask(
        _ctx(store, lambda q: HumanAnswer(text=text, attachments=(att,))),
        state,
    )

    assert out["ok"] is True
    assert out["response"] == text
    [queued] = state.pending_tool_attachments
    assert queued["id"] == att.id
    assert queued["origin"] == ORIGIN_USER
    assert queued["label"] == user_label(3)
    assert Path(queued["path"]) == tmp_path / "images" / f"cli_t1_{att.id}.png"
    assert Path(queued["path"]).is_file()


async def test_run_without_thread_keeps_answer_images_in_staging(tmp_path):
    store = ImageStore(str(tmp_path / "images"))
    att = _staged(store)
    state = SimpleNamespace(pending_tool_attachments=[])

    await _ask(
        _ctx(
            store,
            lambda q: HumanAnswer(text=chip_text(3), attachments=(att,)),
            thread=False,
        ),
        state,
    )

    assert [a["path"] for a in state.pending_tool_attachments] == [att.path]


async def test_response_names_every_answer_image(tmp_path):
    store = ImageStore(str(tmp_path / "images"))
    att = _staged(store)

    only_image = await _ask(
        _ctx(store, lambda q: HumanAnswer(text="", attachments=(att,))),
        SimpleNamespace(pending_tool_attachments=[]),
    )
    bare_text = await _ask(
        _ctx(
            store,
            lambda q: HumanAnswer(text="  см. экран  ", attachments=(att,)),
        ),
        SimpleNamespace(pending_tool_attachments=[]),
    )

    assert only_image["response"] == chip_text(3)
    assert bare_text["response"] == f"{chip_text(3)} см. экран"


async def test_string_answer_is_unchanged_and_carries_no_images(tmp_path):
    state = SimpleNamespace(pending_tool_attachments=[])
    out = await _ask(
        _ctx(
            ImageStore(str(tmp_path / "images")),
            lambda q: f"  {chip_text(1)} текст  ",
        ),
        state,
    )

    assert out["response"] == f"{chip_text(1)} текст"
    assert state.pending_tool_attachments == []


async def test_lost_staging_file_keeps_the_reference_and_the_answer(tmp_path):
    """Файл кармана пропал: ссылка остаётся как есть — проекция пошлёт «файл недоступен»."""
    store = ImageStore(str(tmp_path / "images"))
    att = _staged(store)
    Path(att.path).unlink()
    state = SimpleNamespace(pending_tool_attachments=[])

    out = await _ask(
        _ctx(
            store,
            lambda q: HumanAnswer(
                text=f"{chip_text(3)} вот", attachments=(att,)
            ),
        ),
        state,
    )

    assert out["ok"] is True
    assert out["response"] == f"{chip_text(3)} вот"
    assert state.pending_tool_attachments == [att.to_dict()]


async def test_child_ask_passes_images_to_the_child_run(tmp_path):
    """Вопрос субагента идёт тем же портом родителя: картинки видит модель ребёнка."""
    sink = QueueSubagentProgressSink()
    store = ImageStore(str(tmp_path / "images"))
    att = _staged(store)
    ctx = _ctx(
        store,
        lambda q: HumanAnswer(text=chip_text(3), attachments=(att,)),
        run_id="child_1",
        parent_call_id="parent_fc",
        subagent_progress_sink=sink,
        subagent_tool_name="subagent_worker",
    )
    state = SimpleNamespace(steps=1, max_steps=10, pending_tool_attachments=[])

    out = await _ask(ctx, state, question="Какой экран?")

    assert out["response"] == chip_text(3)
    assert [a["id"] for a in state.pending_tool_attachments] == [att.id]
    assert [e.kind for e in sink.drain()] == ["ask_human", "ask_human_done"]
