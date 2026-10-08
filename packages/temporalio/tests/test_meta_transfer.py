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
from gc_core_temporalio.thread_store_contracts import (
    PAGE_BYTES,
    ThreadStoreCall,
    ThreadStoreReply,
)
from gc_core_temporalio.thread_store_transport import _STORE_ERRORS
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


def _through_history(value, kind):
    """Значение так, как его увидит другая сторона: через JSON-конвертер
    Temporal (ключи сортируются, кортежи — списками)."""
    converter = DataConverter.default.payload_converter
    return converter.from_payloads(converter.to_payloads([value]), [kind])[0]


class _Worker:
    """Транспорт прокси без сервера, но через настоящий конвертер Temporal;
    ``fresh()`` — другой воркер (без кэшей)."""

    def __init__(self, store):
        self.store = store
        self.activities = ThreadStoreActivities({"files": store})
        self.inputs, self.outputs, self.calls = [], [], []

    def fresh(self):
        self.activities = ThreadStoreActivities({"files": self.store})

    async def __call__(self, name, request):
        self.inputs.append(_size(request))
        self.calls.append(name)
        handler = {
            "agent.thread.load": self.activities.load,
            "agent.thread.commit": self.activities.commit,
        }[name]
        try:
            reply = await handler(_through_history(request, ThreadStoreCall))
        except Exception as exc:
            # Как ThreadStoreTransport: ошибка хранилища — тем же классом.
            error = _STORE_ERRORS.get(getattr(exc, "type", None))
            if error is not None:
                raise error(str(exc)) from exc
            raise
        self.outputs.append(_size(reply))
        return _through_history(reply, ThreadStoreReply)


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


def test_diff_round_trip_keeps_values_and_types():
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
        assert set(result) == set(new)
        assert result["nested"]["x"] is 1  # noqa: F632 — не True
    assert diff(new, deepcopy(new)) is None


def test_key_order_does_not_count_as_a_change():
    """База из истории Temporal или SQLite — с ключами по алфавиту, ядро
    пишет записи в своём порядке: разница — только новая запись, а не весь
    журнал (дефект r7: 625 КБ на 200 записях)."""
    log = [
        _record(i, "user_ask", f"запрос {i} " + "я" * 500) for i in range(200)
    ]
    base = _through_history({"user_intent_log": log}, dict)
    assert list(base["user_intent_log"][0]) != list(log[0])
    new = {"user_intent_log": [*log, _record(200, "final_answer", "ответ")]}
    delta = diff_meta(base, new)
    assert payload_size(delta) < 1_000


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


async def test_retry_after_foreign_meta_edit_falls_back_to_full_meta(tmp_path):
    """Запись прошла, воркер упал до ответа, затем TUI поправил meta
    напрямую. Повтор из разницы собрал бы другую meta (Mismatch) — хэш это
    видит, прокси повторяет полной meta, хранилище отдаёт исходную квитанцию."""
    import uuid

    store = _store("file", tmp_path)
    store.append_messages(
        "s", "t", [{"id": "m0", "role": "user", "content": "a"}]
    )
    store.save_meta(
        "s",
        "t",
        {"user_intent_log": [_record(0, "user_ask", "a")], "tui": {"x": 1}},
    )
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    snapshot = await proxy.aread_snapshot("s", "t")
    meta = deepcopy(snapshot.meta)
    meta["user_intent_log"].append(_record(1, "final_answer", "готово"))
    operation = _persist(
        snapshot,
        [{"id": "m1", "role": "assistant", "content": "готово"}],
        meta,
    )
    first = await proxy.acommit_operation("s", "t", operation)
    # «Ответ потерян»: прокси не видел квитанции, а хранилище поменяли.
    current = store.read_snapshot("s", "t", include_state=False)
    store.commit_operation(
        "s",
        "t",
        ThreadStoreOperation(
            uuid.uuid4().hex,
            "update_tui",
            {"patch": {"title": "чужая правка"}},
            generation=current.generation,
        ),
    )
    proxy._meta[("s", "t")] = (
        [snapshot.revision, snapshot.generation],
        deepcopy(snapshot.meta),
    )
    worker.fresh()
    worker.calls.clear()
    retried = await proxy.acommit_operation("s", "t", operation)
    # Разница не сошлась по хэшу → вторая попытка полной meta.
    assert worker.calls[:2] == ["agent.thread.commit", "agent.thread.commit"]
    assert retried.revision == first.revision
    assert retried.max_seq == first.max_seq
    stored = store.read_snapshot("s", "t", include_state=False).meta
    assert stored["user_intent_log"][-1]["content"] == "готово"
    assert len(store.load_messages("s", "t")) == 2


async def test_delta_over_changed_base_is_a_conflict_not_a_failure(tmp_path):
    """Чужая запись между чтением и записью: ядро должно получить конфликт
    ревизии (оно перечитает и повторит), а не отказ сессии."""
    store = _store("file", tmp_path)
    store.save_meta(
        "s", "t", {"current_user_intent": [_record(0, "user_ask", BIG)]}
    )
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    snapshot = await proxy.aread_snapshot("s", "t")
    meta = deepcopy(snapshot.meta)
    meta["user_intent_log"] = [meta["current_user_intent"][0]]
    meta["current_user_intent"] = []
    store.save_meta("s", "t", {"other": "чужая meta"})
    worker.fresh()
    with pytest.raises(ThreadStoreConflict):
        await proxy.acommit_operation(
            "s",
            "t",
            _persist(
                snapshot, [{"id": "x", "role": "user", "content": "y"}], meta
            ),
        )


async def test_subagent_trace_without_base_does_not_reread_parent_meta(
    tmp_path,
):
    """Прокси ребёнка без базы meta: запись трассы не тянет всю meta
    родителя (раньше — перечитывание частями)."""
    store = _store("file", tmp_path)
    log = [_record(i, "user_ask", f"{i}:" + "ю" * 20_000) for i in range(30)]
    store.save_meta("s", "t", {"user_intent_log": log})
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    await proxy.aappend_subagent_trace(
        "s",
        "t",
        parent_call_id="c1",
        child_run_id="r1",
        kind="step",
        payload={},
    )
    assert worker.calls == ["agent.thread.commit"]
    assert max(worker.outputs) < 10_000


async def test_same_meta_version_is_not_sent_twice(tmp_path):
    """Начало хода: meta без состояния, затем снимок с состоянием — большая
    meta второй раз не идёт (та же версия у workflow уже есть)."""
    store = _store("sqlite", tmp_path)
    store.append_messages(
        "s", "t", [{"id": "m0", "role": "user", "content": "a"}]
    )
    log = [_record(i, "user_ask", f"{i}:" + "ю" * 20_000) for i in range(30)]
    store.save_meta("s", "t", {"user_intent_log": log})
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    first = await proxy.aread_snapshot("s", "t", include_state=False)
    calls = len(worker.calls)
    worker.outputs.clear()
    second = await proxy.aread_snapshot("s", "t")
    assert len(worker.calls) - calls == 1
    assert max(worker.outputs) < 10_000
    assert second.meta == first.meta
    assert second.meta["user_intent_log"] == log


def _short_log(count):
    """Журнал из коротких записей (~1 КБ): строки короче порога ссылок на
    тексты сообщений, а вместе — мегабайты."""
    return [
        _record(i % 60, "user_ask", f"{i}: " + "к" * 150) for i in range(count)
    ]


async def test_full_meta_fallback_fits_the_record_with_a_3mb_log(tmp_path):
    """Ревью r8: meta 3 МБ, чужой update_tui посреди хода (ревизию не
    поднимает), запись на другом воркере. Запасной путь полной meta раньше
    слал журнал целиком (HistoryLimitExceeded 3 МБ); теперь — ссылками на
    записи по содержимому, запись проходит, кэш fetch_url не стёрт."""
    from gc_core import thread_store as raw

    store = _store("file", tmp_path)
    store.append_messages(
        "s", "t", [{"id": "m0", "role": "user", "content": "a"}]
    )
    log = _short_log(3100)
    store.save_meta(
        "s",
        "t",
        {
            "user_intent_log": log,
            "tui": {"title": "старый"},
            "shared": {"fetch_url_cache": {"u": "кэш"}, "k": 1},
        },
    )
    assert payload_size(store.load_meta("s", "t")) > 3_000_000
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    snapshot = await proxy.aread_snapshot("s", "t")
    raw.update_tui_meta(
        str(tmp_path / "threads"), "s", "t", {"title": "чужой"}
    )
    worker.fresh()
    worker.inputs.clear()
    meta = deepcopy(snapshot.meta)
    meta["user_intent_log"].append(_record(61, "final_answer", "готово"))
    await proxy.acommit_operation(
        "s",
        "t",
        _persist(
            snapshot,
            [{"id": "m1", "role": "assistant", "content": "готово"}],
            meta,
        ),
    )
    assert max(worker.inputs) < 300_000
    stored = store.load_meta("s", "t")
    assert stored["user_intent_log"] == [
        *log,
        _record(61, "final_answer", "готово"),
    ]
    assert stored["shared"]["fetch_url_cache"] == {"u": "кэш"}


async def test_meta_known_sees_a_tui_edit_without_revision_bump(tmp_path):
    """update_tui в файловом хранилище ревизию не поднимает: версия meta —
    ещё и хэш содержимого, второе чтение отдаёт свежий заголовок."""
    from gc_core import thread_store as raw

    store = _store("file", tmp_path)
    store.append_messages(
        "s", "t", [{"id": "m0", "role": "user", "content": "a"}]
    )
    store.save_meta("s", "t", {"tui": {"title": "старый"}})
    worker = _Worker(store)
    proxy = TemporalThreadStore("files", worker)
    first = await proxy.aread_snapshot("s", "t", include_state=False)
    raw.update_tui_meta(
        str(tmp_path / "threads"), "s", "t", {"title": "новый"}
    )
    second = await proxy.aread_snapshot("s", "t")
    assert second.revision == first.revision
    assert second.meta["tui"]["title"] == "новый"


def test_sqlite_thread_files_live_and_die_with_the_thread(tmp_path):
    """Файлы треда в SQLite: запись идемпотентна, ревизию треда не двигает,
    reset удаляет их вместе с историей."""
    store = _store("sqlite", tmp_path)
    store.append_messages(
        "s", "t", [{"id": "m0", "role": "user", "content": "a"}]
    )
    revision = store.read_snapshot("s", "t", include_state=False).revision
    store.write_thread_file("s", "t", "images/abc.png", b"PNG")
    store.write_thread_file("s", "t", "images/abc.png", b"PNG")
    assert store.read_thread_file("s", "t", "images/abc.png") == b"PNG"
    assert store.read_thread_file("s", "other", "images/abc.png") is None
    assert (
        store.read_snapshot("s", "t", include_state=False).revision == revision
    )
    store.reset("s", "t")
    assert store.read_thread_file("s", "t", "images/abc.png") is None
