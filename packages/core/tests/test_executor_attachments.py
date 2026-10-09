"""Вложения сообщения: копию из кармана в тред делает ядро, когда сообщение получает тред.

Единственный владелец копии — ядро (спека §3): TUI, CLI и SDK отдают вложения из
кармана процесса как есть. Копия делается при сборке стартовых сообщений хода —
после сброса треда для `start_new_thread` — и при приёме сообщения из очереди.

Каркас — как в `test_executor_queue_before_loop.py`: настоящие
`acreate_run_context` и `AgentExecutor.arun`, подменён только цикл агента.
"""

import errno
import importlib
import io
import os
import types

from gc_core.agent_executor import AgentExecutor
from gc_core.aio import AsyncAgent
from gc_core.attachments import store as store_module
from gc_core.attachments.model import (
    ORIGIN_USER,
    Attachment,
    attachments_of,
    chip_text,
    user_label,
)
from gc_core.attachments.store import ImageStore, amake_attachment
from gc_testkit.settings import make_settings
from PIL import Image


def _cfg(tmp_path):
    return make_settings(
        {
            "paths.threads_data_path": str(tmp_path),
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://test",
            "agent.model": "x",
            "agent.max_steps": 1,
            "agent.system_prompt_id": "v1",
            "logging.save_logs": False,
        }
    )


def _png(color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


async def _staged(tmp_path, color: str = "red", n: int = 1) -> Attachment:
    """Картинка в кармане процесса — в таком виде её отдают ядру TUI, CLI и SDK."""
    store = ImageStore.for_threads_dir(str(tmp_path))
    return await amake_attachment(
        store,
        _png(color),
        origin=ORIGIN_USER,
        label=user_label(n),
        source="clipboard",
    )


def _att(att_id: str, path: str) -> Attachment:
    return Attachment(
        id=att_id,
        kind="image",
        mime="image/png",
        width=64,
        height=48,
        size=100,
        path=path,
        origin=ORIGIN_USER,
        label=user_label(1),
        source="clipboard",
        orig_width=64,
        orig_height=48,
    )


async def _executor(monkeypatch, tmp_path, on_call):
    """Настоящий executor; цикл агента подменён на `on_call(messages, kw)`."""
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))

    async def _stub(messages, state, ctx, **kw):
        await on_call(messages, kw)
        return {"finished": True}

    monkeypatch.setattr(ae, "arun_agent", _stub)
    ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    return ae.AgentExecutor(ctx)


def test_build_user_messages_puts_committed_attachments_on_its_message():
    staged = _att("aaaaaaaaaaaaaaaa", "/pocket/aaaaaaaaaaaaaaaa.png")
    committed = _att(
        "aaaaaaaaaaaaaaaa", "/threads/images/cli_t_aaaaaaaaaaaaaaaa.png"
    )
    msgs = AgentExecutor._build_user_messages(
        ["[Изображение #1] что тут?", "без картинки"],
        message_ids=None,
        display_contents=None,
        skills_loaded=None,
        gen_id=lambda p: "g",
        attachments=[[staged], None],
        commit=lambda att: committed,
    )
    assert msgs[0]["attachments"] == [committed.to_dict()]
    assert msgs[0]["content"] == "[Изображение #1] что тут?"
    assert "attachments" not in msgs[1]


def test_build_user_messages_without_a_thread_keeps_the_pocket_attachment():
    staged = _att("aaaaaaaaaaaaaaaa", "/pocket/aaaaaaaaaaaaaaaa.png")
    msgs = AgentExecutor._build_user_messages(
        ["[Изображение #1]"],
        message_ids=None,
        display_contents=None,
        skills_loaded=None,
        gen_id=lambda p: "g",
        attachments=[[staged]],
        commit=None,
    )
    assert msgs[0]["attachments"] == [staged.to_dict()]


async def test_attachment_is_copied_into_the_thread(monkeypatch, tmp_path):
    seen: list = []

    async def _on_call(messages, kw):
        seen.append(messages)

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    staged = await _staged(tmp_path)

    await ex.arun(
        [f"{chip_text(1)} что тут?"],
        thread_id="t",
        start_new_thread=True,
        system="cli",
        attachments=[[staged]],
    )

    [committed] = attachments_of(seen[0][0])
    assert committed.id == staged.id
    assert committed.label == user_label(1)
    assert committed.path != staged.path
    assert (
        os.path.dirname(committed.path)
        == ImageStore.for_threads_dir(str(tmp_path)).root
    )
    assert os.path.basename(committed.path).startswith("cli_t_")
    assert os.path.isfile(committed.path)
    assert seen[0][0]["content"] == f"{chip_text(1)} что тут?"


async def test_without_a_thread_the_attachment_stays_in_the_pocket(
    monkeypatch, tmp_path
):
    seen: list = []

    async def _on_call(messages, kw):
        seen.append(messages)

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    staged = await _staged(tmp_path)

    await ex.arun([f"{chip_text(1)} что тут?"], attachments=[[staged]])

    assert attachments_of(seen[0][0]) == [staged]


async def test_new_thread_keeps_the_fresh_copy(monkeypatch, tmp_path):
    """Сторож порядка: копия в тред делается ПОСЛЕ сброса `start_new_thread`.

    Нагрузку тест несёт с Task 24 — там сброс начинает удалять картинки треда;
    копия, сделанная до сброса, пропала бы вместе с тредом.
    """
    seen: list = []

    async def _on_call(messages, kw):
        seen.append(messages)

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    staged = await _staged(tmp_path)

    for _ in range(2):
        await ex.arun(
            [f"{chip_text(1)} что тут?"],
            thread_id="t",
            start_new_thread=True,
            system="cli",
            attachments=[[staged]],
        )

    [committed] = attachments_of(seen[1][0])
    assert os.path.isfile(committed.path)


async def test_message_taken_mid_run_is_copied_into_the_thread(
    monkeypatch, tmp_path
):
    holder: dict = {}

    async def _on_call(messages, kw):
        holder["ex"].add_message(
            {
                "role": "user",
                "content": f"{chip_text(2)} и вот это",
                "id": "m_mid",
                "attachments": [holder["staged"].to_dict()],
            }
        )
        holder["taken"] = kw["get_pending_messages"]()

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    holder["ex"] = ex
    holder["staged"] = await _staged(tmp_path, "blue", n=2)

    await ex.arun(
        ["первое"], thread_id="t", start_new_thread=True, system="cli"
    )

    [committed] = attachments_of(holder["taken"][0])
    assert committed.path != holder["staged"].path
    assert os.path.isfile(committed.path)
    assert holder["taken"][0]["id"] == "m_mid"


async def test_message_taken_mid_run_survives_a_full_disk(
    monkeypatch, tmp_path
):
    """Очередь к моменту копии уже разобрана: ошибка записи потеряла бы сообщение.
    Копия не удалась — сообщение уходит с вложением из кармана."""
    holder: dict = {}

    def _no_space(path, data):
        raise OSError(errno.ENOSPC, "No space left on device", path)

    async def _on_call(messages, kw):
        holder["ex"].add_message(
            {
                "role": "user",
                "content": f"{chip_text(2)} и вот это",
                "id": "m_mid",
                "attachments": [holder["staged"].to_dict()],
            }
        )
        holder["taken"] = kw["get_pending_messages"]()

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    holder["ex"] = ex
    holder["staged"] = await _staged(tmp_path, "blue", n=2)
    monkeypatch.setattr(store_module, "_write_atomic", _no_space)

    await ex.arun(
        ["первое"], thread_id="t", start_new_thread=True, system="cli"
    )

    assert [m["id"] for m in holder["taken"]] == ["m_mid"]
    assert attachments_of(holder["taken"][0]) == [holder["staged"]]


async def test_message_queued_before_the_run_is_copied_into_the_thread(
    monkeypatch, tmp_path
):
    seen: list = []

    async def _on_call(messages, kw):
        seen.append(messages)

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    staged = await _staged(tmp_path)
    ex.add_message(
        {
            "role": "user",
            "content": f"{chip_text(1)} из брони",
            "id": "m_q",
            "attachments": [staged.to_dict()],
        }
    )

    await ex.arun(
        ["стартовое"], thread_id="t", start_new_thread=True, system="cli"
    )

    [committed] = attachments_of(seen[0][1])
    assert committed.path != staged.path
    assert os.path.isfile(committed.path)


async def test_missing_pocket_file_does_not_break_the_turn(
    monkeypatch, tmp_path
):
    """Файл кармана пропал до старта хода — ход идёт, вложение остаётся как было.

    На месте такой картинки проекция отправит записку «file unavailable» (Part E).
    """
    seen: list = []

    async def _on_call(messages, kw):
        seen.append(messages)

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    staged = await _staged(tmp_path)
    os.remove(staged.path)

    await ex.arun(
        [f"{chip_text(1)} что тут?"],
        thread_id="t",
        start_new_thread=True,
        system="cli",
        attachments=[[staged]],
    )

    assert attachments_of(seen[0][0]) == [staged]


async def test_sdk_run_passes_attachments_and_the_tail_carries_its_own(
    monkeypatch, tmp_path
):
    """`AsyncAgent.run(attachments=)` доходит до хода; хвост их не наследует."""
    ae = importlib.import_module("gc_core.agent_executor")
    # `AsyncAgent` держит класс исполнителя из первого импорта, а тесты с заглушкой
    # `gc_core.tools` (`test_backend_mode_switch.py` и соседи) перезагружают
    # `gc_core.agent_executor`: без этой строки подмены ниже ушли бы в свежую копию
    # модуля, мимо которой `AsyncAgent` ходит (настоящий запрос к бэкенду).
    monkeypatch.setattr(
        importlib.import_module("gc_core.aio.agent"),
        "AgentExecutor",
        ae.AgentExecutor,
    )
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))
    batches: list = []
    teardown_steps: list = []

    async def _loop(messages, state, ctx, **kw):
        batches.append([[a.id for a in attachments_of(m)] for m in messages])
        return {"answer": "ok"}

    original_teardown = ae._aclose_mcp_and_release

    async def _teardown(owner_reset):
        if teardown_steps:
            teardown_steps.pop(0)()
        await original_teardown(owner_reset)

    monkeypatch.setattr(ae, "arun_agent", _loop)
    monkeypatch.setattr(ae, "_aclose_mcp_and_release", _teardown)
    staged = await _staged(tmp_path)

    async with await AsyncAgent.create(_cfg(tmp_path)) as agent:
        teardown_steps.append(
            lambda: agent.send(
                {"role": "user", "content": "вдогонку", "id": "m_tail"}
            )
        )
        await agent.run(
            f"{chip_text(1)} что тут?",
            thread_id="t",
            start_new_thread=True,
            attachments=[[staged]],
        )

    assert batches == [[[staged.id]], [[]]]


async def test_executor_persist_raises_image_seq_of_the_thread(
    monkeypatch, tmp_path
):
    from gc_core.attachments.numbering import read_image_seq
    from gc_core.thread_store import load_thread_meta

    async def _on_call(messages, kw):
        await kw["persist_messages_callback"](messages)

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    staged = await _staged(tmp_path, n=7)

    await ex.arun(
        [f"{chip_text(7)} что тут?"],
        thread_id="t",
        start_new_thread=True,
        system="cli",
        attachments=[[staged]],
    )

    assert read_image_seq(load_thread_meta(str(tmp_path), "cli", "t")) == 7
