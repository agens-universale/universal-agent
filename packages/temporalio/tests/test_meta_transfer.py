"""Meta треда в истории Temporal — разницей и частями, хранилищу — целиком.

Без сервера: прокси workflow вызывает activities напрямую, размер каждого
входа и ответа меряется штатным конвертером Temporal. Проверяется, что
хранилище (и с ним локальный режим) видит ту же полную meta, что и раньше,
а в историю не уходит весь журнал намерений.
"""

from copy import deepcopy

import pytest
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
)
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core_temporalio.meta_delta import (
    TEXT_REF_KEY,
    MetaDeltaError,
    TextIndex,
    TextSource,
    apply,
    diff,
    diff_meta,
)
from gc_core_temporalio.sizes import payload_size
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
from gc_core_temporalio.thread_store_activities import ThreadStoreActivities
from gc_core_temporalio.thread_store_contracts import PAGE_BYTES
from gc_core_temporalio.thread_store_proxy import TemporalThreadStore
from temporalio.converter import DataConverter

LIMIT = 2 * 1024 * 1024
#: ~600 КБ в истории Temporal (кириллица — 6 байт на символ).
BIG = "ж" * 100_000


def _size(value):
    payloads = DataConverter.default.payload_converter.to_payloads([value])
    return payloads[0].ByteSize()


def _store(kind, tmp_path):
    if kind == "file":
        return DurableFileThreadStore(str(tmp_path / "threads"))
    return SQLiteThreadStore(str(tmp_path / "threads.sqlite"))


def _record(turn, action, content):
    return {
        "turn": turn,
        "ts": f"2026-10-07T00:00:{turn:02d}Z",
        "action": action,
        "tool_name": None,
        "content": content,
    }


class _Worker:
    """Транспорт прокси без сервера; ``fresh()`` — другой воркер (без кэшей)."""

    def __init__(self, store):
        self.store = store
        self.activities = ThreadStoreActivities({"files": store})
        self.inputs, self.outputs = [], []

    def fresh(self):
        self.activities = ThreadStoreActivities({"files": self.store})

    async def __call__(self, name, request):
        self.inputs.append(_size(request))
        handler = {
            "agent.thread.load": self.activities.load,
            "agent.thread.commit": self.activities.commit,
        }[name]
        try:
            reply = await handler(request)
        except Exception as exc:
            if getattr(exc, "type", None) == "ThreadStoreConflict":
                raise ThreadStoreConflict(str(exc)) from exc
            raise
        self.outputs.append(_size(reply))
        return reply


def _persist(snapshot, messages, meta, *, operation_id="op"):
    return ThreadStoreOperation(
        operation_id,
        "persist",
        {
            "messages": messages,
            "meta": meta,
            "start_seq": snapshot.max_seq,
            "expected_written": len(messages),
        },
        expected_revision=snapshot.revision,
        generation=snapshot.generation,
    )


# --- разница ---------------------------------------------------------------


def test_diff_round_trip_keeps_values_types_and_order():
    old = {
        "a": 1,
        "log": [_record(1, "user_ask", "привет")],
        "nested": {"x": True, "y": [1, 2], "z": "старое"},
        "gone": "x",
    }
    new = {
        "nested": {"y": [1, 2, 3], "x": 1, "z": "новое"},
        "a": 1,
        "log": [
            _record(1, "user_ask", "привет"),
            _record(1, "final_answer", "ок"),
        ],
        "fresh": {"k": "v"},
    }
    for delta in (diff(old, new), diff_meta(old, new)):
        result = apply(deepcopy(old), delta)
        assert result == new
        assert list(result) == list(new)
        assert list(result["nested"]) == list(new["nested"])
        assert result["nested"]["x"] is 1  # noqa: F632 — не True
    assert diff(new, deepcopy(new)) is None


def test_log_tail_is_sent_from_absolute_index():
    log = [
        _record(i, "user_ask", f"запрос {i} " + "я" * 500) for i in range(200)
    ]
    old = {"user_intent_log": log}
    new = {"user_intent_log": [*log, _record(200, "final_answer", "ответ")]}
    delta = diff_meta(old, new)
    assert payload_size(delta) < 2_000 < payload_size(new)
    # Повтор поверх уже записанной meta (хранилище само обновило мелкие
    # ключи верхнего уровня) собирает ту же meta.
    stored = {**new, "updated_at": "later", "schema_version": 9}
    assert apply(stored, delta) == new


def test_long_texts_go_by_reference_to_messages():
    message = {"id": "u1", "role": "user", "content": f"  {BIG}  "}
    texts = TextIndex()
    texts.add([message])
    new = {
        "current_user_intent": [_record(1, "user_ask", BIG)],
        "shared": {"copy": BIG},
    }
    delta = diff_meta({}, new, texts)
    assert payload_size(delta) < 2_000
    assert TEXT_REF_KEY in str(delta)
    from gc_core_temporalio.message_refs import message_hash

    known = {message_hash(message): message}
    assert apply({}, delta, TextSource(known.get)) == new
    with pytest.raises(MetaDeltaError):
        apply({}, delta, TextSource({}.get))
    changed = {message_hash(message): {**message, "content": "другое"}}
    with pytest.raises(MetaDeltaError):
        apply({}, delta, TextSource(changed.get))


def test_text_moving_inside_meta_goes_by_content_reference():
    """Конец хода: запрос переезжает из current_user_intent в журнал;
    сжатие пересобирает журнал из тех же записей. Тексты — ссылкой на
    строку базы; повтор поверх уже записанной meta находит их там же."""
    ask = _record(1, "user_ask", BIG)
    base = {"current_user_intent": [ask], "user_intent_log": []}
    new = {
        "current_user_intent": [],
        "user_intent_log": [ask, _record(1, "final_answer", "ок")],
    }
    texts = TextIndex()
    texts.add_meta(base)
    delta = diff_meta(base, new, texts)
    assert payload_size(delta) < 2_000
    assert apply(base, delta, TextSource(meta=base)) == new
    # Повтор: база — уже записанная meta (хранилище добавило свои ключи).
    stored = {**new, "updated_at": "later"}
    assert apply(stored, delta, TextSource(meta=stored)) == new
    with pytest.raises(MetaDeltaError):
        apply(base, delta, TextSource(meta={}))


# --- прокси и воркер --------------------------------------------------------


@pytest.mark.parametrize("kind", ["file", "sqlite"])
async def test_local_thread_with_large_log_is_read_in_parts_and_kept_whole(
    tmp_path, kind
):
    """Тред вёл локальный режим: журнал намерений ~3.6 МБ кириллицы. Сессия
    Temporal читает meta частями, пишет ход разницей; в хранилище — весь
    журнал плюс новые записи, ничего не урезано."""
    store = _store(kind, tmp_path)
    store.append_messages(
        "s", "t", [{"id": "m0", "role": "user", "content": "старт"}]
    )
    log = [
        _record(i % 60, "user_ask", f"{i}:" + "ю" * 20_000) for i in range(30)
    ]
    store.save_meta(
        "s",
        "t",
        {
            "user_intent_log": log,
            "shared": {"fetch_url_cache": {"u": "кэш" * 1000}, "k": 1},
        },
    )
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    snapshot = await proxy.aread_snapshot("s", "t")
    assert snapshot.meta["user_intent_log"] == log
    assert snapshot.meta["shared"]["fetch_url_cache"] == {}
    assert len(worker.outputs) > 5
    assert max(worker.outputs) < PAGE_BYTES + 100_000

    question = {"id": "m1", "role": "user", "content": BIG}
    meta = deepcopy(snapshot.meta)
    meta["user_intent_log"] += [
        _record(61, "user_ask", BIG),
        _record(61, "final_answer", "готово"),
    ]
    meta["shared"]["k"] = 2
    worker.inputs.clear()
    receipt = await proxy.acommit_operation(
        "s", "t", _persist(snapshot, [question], meta)
    )
    # Вход записи: сообщение и малая разница, не журнал и не второй BIG.
    assert max(worker.inputs) < payload_size(question) + 20_000
    stored = store.read_snapshot("s", "t", include_state=False).meta
    assert stored["user_intent_log"] == meta["user_intent_log"]
    assert stored["shared"]["k"] == 2
    # Кэш fetch_url локального режима не потерян.
    assert stored["shared"]["fetch_url_cache"] == {"u": "кэш" * 1000}
    # Ядро получает полную meta квитанции (без кэша — как при чтении).
    expected = {
        **stored,
        "shared": {**stored["shared"], "fetch_url_cache": {}},
    }
    assert receipt.meta == expected


@pytest.mark.parametrize("kind", ["file", "sqlite"])
async def test_retry_on_fresh_worker_after_write_returns_original_receipt(
    tmp_path, kind
):
    """Воркер упал после записи, до ответа: повтор на другом воркере собирает
    из разницы ту же операцию — хранилище отвечает исходной квитанцией."""
    store = _store(kind, tmp_path)
    store.append_messages(
        "s", "t", [{"id": "m0", "role": "user", "content": "a"}]
    )
    store.save_meta(
        "s", "t", {"user_intent_log": [_record(0, "user_ask", "a")]}
    )
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    snapshot = await proxy.aread_snapshot("s", "t")
    meta = deepcopy(snapshot.meta)
    meta["user_intent_log"].append(_record(1, "user_ask", BIG))
    message = {"id": "m1", "role": "user", "content": BIG}
    operation = _persist(snapshot, [message], meta)

    requests = []
    original_call = worker.__call__

    async def capture(name, request):
        requests.append(deepcopy(request))
        return await original_call(name, request)

    proxy._call = capture
    first = await proxy.acommit_operation("s", "t", operation)
    worker.fresh()
    retried = await worker.activities.commit(requests[-1])
    assert retried.receipt == replace_meta(first, {})
    assert retried.meta_reload  # база другая — workflow перечитает meta
    stored = store.read_snapshot("s", "t", include_state=False).meta
    assert stored["user_intent_log"][-1]["content"] == BIG


def replace_meta(receipt, meta):
    from dataclasses import replace

    return replace(receipt, meta=meta)


async def test_text_from_earlier_message_is_found_in_thread_by_fresh_worker(
    tmp_path,
):
    """Запрос пользователя записан раньше (начало хода), а журнал — в конце:
    ссылка на сообщение треда; другой воркер находит его в хранилище."""
    store = _store("file", tmp_path)
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    snapshot = await proxy.aread_snapshot("s", "t")
    question = {"id": "m1", "role": "user", "content": BIG}
    meta = {"current_user_intent": [_record(1, "user_ask", BIG)]}
    receipt = await proxy.acommit_operation(
        "s", "t", _persist(snapshot, [question], meta, operation_id="a")
    )
    worker.fresh()
    worker.inputs.clear()
    answer = {"id": "m2", "role": "assistant", "content": "ок"}
    meta = {
        "current_user_intent": [],
        "user_intent_log": [
            _record(1, "user_ask", BIG),
            _record(1, "final_answer", "ок"),
        ],
    }
    operation = _persist(receipt, [answer], meta, operation_id="b")
    await proxy.acommit_operation("s", "t", operation)
    assert max(worker.inputs) < 20_000
    stored = store.read_snapshot("s", "t", include_state=False).meta
    assert stored["user_intent_log"][0]["content"] == BIG
    assert stored["current_user_intent"] == []


async def test_metadata_operation_without_known_base_reloads_meta(tmp_path):
    """Флаг runtime на другом воркере, чья база устарела: квитанция без
    разницы, прокси перечитывает meta — ядро получает актуальную."""
    store = _store("file", tmp_path)
    store.save_meta(
        "s", "t", {"user_intent_log": [_record(0, "user_ask", BIG)]}
    )
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    await proxy.aread_snapshot("s", "t", include_state=False)
    worker.fresh()
    # Хранилище сменило версию мимо этого воркера (как после повтора).
    store.save_meta(
        "s", "t", {"user_intent_log": [_record(0, "user_ask", BIG)], "x": 1}
    )
    session_meta = {}
    await proxy.apatch_runtime_flag(
        "s", "t", "summarizing", active=True, session_meta=session_meta
    )
    assert session_meta["runtime"]["summarizing"] is True
    assert max(worker.outputs) < LIMIT
