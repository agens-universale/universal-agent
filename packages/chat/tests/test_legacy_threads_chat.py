"""gc-chat видит диалоги формата develop (спека хранилищ v2, 9.4).

Ответы develop лежат файлами ``responses-index`` и читаются, пока ответа нет
в хранилище тредов; новые ответы — поток ``chat.responses`` и алиасы.
Ожидания — из manifest.json фикстур.
"""

import uuid

from gc_chat.dialogs import dialog_list, dialog_turns
from gc_chat.responses.index_store import ResponseIndex, ResponseRecord, chain
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads


async def test_chat_lists_legacy_dialog(tmp_path):
    fixture = LEGACY_THREADS["chat_dialog"]
    threads = install_legacy_threads(tmp_path)
    dialogs = await dialog_list(ResponseIndex.for_threads_dir(str(threads)))
    assert [d["thread_id"] for d in dialogs] == [fixture.thread_id]


async def test_develop_dialog_continues_in_v2(tmp_path):
    fixture = LEGACY_THREADS["chat_dialog"]
    index = ResponseIndex.for_threads_dir(
        str(install_legacy_threads(tmp_path))
    )
    old_chain = chain(index.legacy_dialogs()[fixture.thread_id])
    old = old_chain[-1]
    assert await index.load(old.response_id) == old
    new = ResponseRecord(
        "resp_" + str(uuid.uuid4()),
        fixture.thread_id,
        old.cwd,
        old.response_id,
        old.created_at + 10,
        "completed",
        {"output": []},
    )
    await index.save(new)

    [dialog] = await dialog_list(index)
    assert dialog["latest_response_id"] == new.response_id
    assert dialog["created_at"] == old_chain[0].created_at
    assert dialog["updated_at"] == new.created_at
    turns = await dialog_turns(index, fixture.thread_id)
    assert [turn["response_id"] for turn in turns] == [
        *(record.response_id for record in old_chain),
        new.response_id,
    ]


async def test_v2_answers_are_also_written_for_develop(tmp_path):
    """Откат на develop: его ``ResponseIndex.load`` читает только
    ``responses-index/*.json`` — ответ ветки лежит и там, в его формате."""
    import json

    threads = tmp_path / "threads"
    index = ResponseIndex.for_threads_dir(str(threads))
    record = ResponseRecord(
        "resp_" + str(uuid.uuid4()),
        "dialog-1",
        str(tmp_path),
        None,
        100,
        "completed",
        {"output": []},
    )
    await index.save(record)
    path = tmp_path / "responses-index" / f"{record.response_id}.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "thread_id": "dialog-1",
        "cwd": str(tmp_path),
        "previous_response_id": None,
        "created_at": 100,
        "status": "completed",
        "response": {"output": []},
    }
