"""Старые записи треда без ``payload.id`` не дописываются второй раз.

В память такая запись попадает с синтезированным ``id``; история, отправленная
в ход целиком (Responses без ``previous_response_id``), должна узнаваться по
ключу и до, и после синтеза id.
"""

import json

from gc_core.thread_store import ThreadSession
from gc_core.threads import file_layout
from gc_core.threads.file_store import FileThreadStore


async def test_legacy_records_without_id_are_not_duplicated(tmp_path):
    store = FileThreadStore(tmp_path)
    data_path, _ = file_layout.thread_paths(str(tmp_path), "sys", "t1")
    with open(data_path, "w", encoding="utf-8") as f:
        for seq, message in enumerate(
            [
                {"role": "user", "content": "привет"},
                {"role": "assistant", "content": "здравствуйте"},
            ],
            1,
        ):
            record = {
                "type": "message",
                "thread_id": "t1",
                "system": "sys",
                "seq": seq,
                "payload": message,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    session = await ThreadSession.afrom_config(
        settings=None, system_id="sys", thread_id="t1", store=store
    )
    assert all(m["id"].startswith("m_") for m in session.history_messages)
    batch = [
        *session.history_messages,
        {"role": "user", "content": "новое", "id": "m_new"},
    ]
    await session.apersist_batch(batch, previous_response_id=None)
    with open(data_path, encoding="utf-8") as f:
        payloads = [json.loads(line)["payload"] for line in f]
    assert [p["content"] for p in payloads] == [
        "привет",
        "здравствуйте",
        "новое",
    ]
    assert len(session.history_messages) == 3
