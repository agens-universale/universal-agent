"""Конформанс хранилища тредов v2: проверка чужой реализации одной функцией.

Реализацию пишет встраивающий — у себя, поверх своей базы. Проверить её нашей
меркой, не переписывая наши тесты::

    from gc_testkit.thread_store_conformance_v2 import assert_conforms

    def test_my_postgres_store():
        assert_conforms(lambda clock: PostgresThreadStore(dsn, clock=clock))

Фабрика получает :class:`FakeClock` — часы, по которым хранилище считает
истечение аренды и возраст квитанций. Хранилище с собственными часами (база)
проверку аренды пройдёт, только если умеет брать время у переданных часов:
иначе проверки истечения ждали бы минуту живого времени.

Каждая проверка получает свежие часы и свежее хранилище. Отказ —
:class:`ConformanceError` с объяснением, что именно нарушено. Хранилище с
соединениями (пул базы привязан к loop) набор закрывает после каждой
проверки в том же loop: зовёт ``await store.close()``, если такой метод есть.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import UnionType
from typing import Any, Union, get_args, get_origin

from gc_core.threads.errors import (
    OperationMismatch,
    OwnershipError,
    ThreadConflict,
)
from gc_core.threads.models import (
    BlobRef,
    Compaction,
    Lease,
    LeaseRequest,
    RuntimeStatus,
    SessionState,
    SideChange,
    ThreadChange,
    ThreadInfo,
    ThreadKey,
    ThreadQuery,
    Usage,
)
from gc_core.threads.store import ThreadStore
from pydantic import BaseModel

KEY = ThreadKey(system="cli", thread="conformance")
MINUTE = timedelta(seconds=60)
_WAIT = 2.0


class ConformanceError(AssertionError):
    """Реализация не удовлетворяет контракту. Текст называет, чем именно."""


class FakeClock:
    """Часы хранилища в проверках: время идёт только по ``advance``."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _expect(condition: bool, what: str) -> None:
    if not condition:
        raise ConformanceError(what)


async def _raises(exc: type[BaseException], awaitable, what: str) -> None:
    try:
        await awaitable
    except exc:
        return
    raise ConformanceError(f"{what}: ожидался {exc.__name__}")


def _msgs(*ids: str) -> list[dict[str, Any]]:
    return [{"id": i, "role": "user", "content": f"текст {i}"} for i in ids]


def _change(op: str, revision: int, lease: Lease, **fields) -> ThreadChange:
    return ThreadChange(
        op_id=op, expected_revision=revision, lease=lease, **fields
    )


async def _claim(store: ThreadStore, owner="a", ttl=MINUTE, key=KEY) -> Lease:
    record = await store.claim(key, LeaseRequest(owner=owner, ttl=ttl))
    _expect(record.lease is not None, "claim обязан вернуть запись с арендой")
    return record.lease


async def _ids(store: ThreadStore, stream="messages", key=KEY) -> list[str]:
    return [e.data.get("id") for e in await store.read_stream(key, stream)]


# --- чтение и создание --------------------------------------------------------


async def check_missing_thread(store: ThreadStore, _clock: FakeClock) -> None:
    _expect(await store.get_thread(KEY) is None, "нет треда — None")
    _expect(await store.read_stream(KEY, "messages") == [], "нет треда — []")
    _expect(await store.resolve_alias("nope") is None, "нет алиаса — None")


async def check_claim_creates_thread(store: ThreadStore, clock: FakeClock):
    record = await store.claim(KEY, LeaseRequest(owner="a", ttl=MINUTE))
    lease = record.lease
    _expect(record.revision == 0, "новый тред — revision 0")
    _expect(record.generation == 0, "новый тред — generation 0")
    _expect(lease is not None and lease.epoch == 1, "первая аренда — epoch 1")
    _expect(lease.expires_at == clock() + MINUTE, "срок по часам хранилища")
    _expect(bool(record.info.created_at), "created_at ставит хранилище")
    _expect(await store.get_thread(KEY) is not None, "тред создан")


async def check_commit_requires_claim(store: ThreadStore, _clock: FakeClock):
    lease = Lease(owner="a", epoch=1)
    await _raises(
        OwnershipError,
        store.commit(KEY, _change("op", 0, lease, append_messages=_msgs("m"))),
        "commit без claim",
    )


async def check_append_and_read_messages(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    first = await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("m1", "m2"))
    )
    _expect(first.revision == 1, "commit увеличивает revision")
    _expect(first.max_seq.get("messages") == 2, "квитанция знает max_seq")
    await store.commit(
        KEY, _change("op2", 1, lease, append_messages=_msgs("m3", "m4"))
    )
    entries = await store.read_stream(KEY, "messages")
    _expect([e.seq for e in entries] == [1, 2, 3, 4], "seq 1..4 по порядку")
    _expect(
        await _ids(store) == ["m1", "m2", "m3", "m4"],
        "данные в порядке записи",
    )
    tail = await store.read_stream(KEY, "messages", after_seq=2)
    _expect([e.seq for e in tail] == [3, 4], "after_seq отсекает прочитанное")
    head = await store.read_stream(KEY, "messages", limit=1)
    _expect([e.seq for e in head] == [1], "limit ограничивает выдачу")
    record = await store.get_thread(KEY)
    _expect(record.info.messages_count == 4, "messages_count = длина потока")
    _expect(record.max_seq.get("messages") == 4, "запись знает max_seq")


# --- ревизия и op_id ----------------------------------------------------------


async def check_revision_conflict(store: ThreadStore, _clock: FakeClock):
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    await _raises(
        ThreadConflict,
        store.commit(
            KEY, _change("op2", 0, lease, append_messages=_msgs("b"))
        ),
        "устаревшая expected_revision",
    )
    _expect(await _ids(store) == ["a"], "отвергнутый commit ничего не пишет")


async def check_op_id_idempotent(store: ThreadStore, _clock: FakeClock):
    lease = await _claim(store)
    change = _change("op1", 0, lease, append_messages=_msgs("a"))
    first = await store.commit(KEY, change)
    again = await store.commit(KEY, change)
    _expect(again == first, "повтор op_id возвращает ту же квитанцию")
    _expect(await _ids(store) == ["a"], "повтор op_id не задваивает историю")


async def check_op_id_mismatch(store: ThreadStore, _clock: FakeClock):
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    await _raises(
        OperationMismatch,
        store.commit(
            KEY, _change("op1", 0, lease, append_messages=_msgs("b"))
        ),
        "тот же op_id с другим содержимым",
    )


async def check_receipt_beats_lease(store: ThreadStore, clock: FakeClock):
    lease = await _claim(store, owner="a")
    change = _change("op1", 0, lease, append_messages=_msgs("a"))
    first = await store.commit(KEY, change)
    clock.advance(MINUTE + timedelta(seconds=1))
    taken = await _claim(store, owner="b")
    _expect(taken.epoch == 2, "смена владельца — epoch + 1")
    again = await store.commit(KEY, change)
    _expect(again == first, "квитанция проверяется раньше аренды")


# --- аренда ------------------------------------------------------------------


async def check_lease_expiry_and_fencing(store, clock: FakeClock) -> None:
    old = await _claim(store, owner="a")
    clock.advance(MINUTE + timedelta(seconds=1))
    new = await _claim(store, owner="b")
    _expect(new.epoch == old.epoch + 1, "истёкшую аренду забирают с epoch + 1")
    await _raises(
        OwnershipError,
        store.commit(KEY, _change("a1", 0, old, append_messages=_msgs("a"))),
        "фенсинг: запись по устаревшей эпохе",
    )
    await store.commit(KEY, _change("b1", 0, new, append_messages=_msgs("b")))
    _expect(await _ids(store) == ["b"], "пишет только новый владелец")


async def check_claim_held_by_other(store: ThreadStore, _clock: FakeClock):
    await _claim(store, owner="a")
    await _raises(
        OwnershipError,
        store.claim(KEY, LeaseRequest(owner="b", ttl=MINUTE)),
        "живая чужая аренда",
    )


async def check_claim_extends_own(store: ThreadStore, clock: FakeClock):
    first = await _claim(store, owner="a")
    clock.advance(timedelta(seconds=30))
    again = await _claim(store, owner="a")
    _expect(again.epoch == first.epoch, "продление своей аренды — та же эпоха")
    _expect(again.expires_at == clock() + MINUTE, "продление сдвигает срок")


async def check_claim_after_own_expiry_bumps_epoch(store, clock: FakeClock):
    first = await _claim(store, owner="a")
    clock.advance(MINUTE + timedelta(seconds=1))
    again = await _claim(store, owner="a")
    _expect(again.epoch == first.epoch + 1, "своя истёкшая аренда — epoch + 1")


async def check_release(store: ThreadStore, _clock: FakeClock) -> None:
    lease = await _claim(store, owner="a")
    await store.release(KEY, Lease(owner="b", epoch=lease.epoch))
    _expect(
        (await store.get_thread(KEY)).lease == lease, "чужой release — no-op"
    )
    await store.release(KEY, lease)
    _expect((await store.get_thread(KEY)).lease is None, "release снимает")
    again = await _claim(store, owner="a")
    _expect(again.epoch == lease.epoch + 1, "эпоха переживает release")


async def check_expired_lease_rejected_without_takeover(store, clock):
    lease = await _claim(store, owner="a")
    clock.advance(MINUTE + timedelta(seconds=1))
    await _raises(
        OwnershipError,
        store.commit(KEY, _change("op", 0, lease, append_messages=_msgs("a"))),
        "истёкшая аренда, даже если её никто не забрал",
    )


async def check_commit_with_pre_extension_lease(store, clock) -> None:
    first = await _claim(store, owner="a")
    clock.advance(timedelta(seconds=30))
    await _claim(store, owner="a")
    clock.advance(timedelta(seconds=40))
    # Срок считает хранилище по своей аренде, а не по копии у вызывающего.
    await store.commit(
        KEY, _change("op", 0, first, append_messages=_msgs("a"))
    )


async def check_lease_checked_before_revision(store, clock) -> None:
    old = await _claim(store, owner="a")
    await store.commit(KEY, _change("op1", 0, old, append_messages=_msgs("a")))
    clock.advance(MINUTE + timedelta(seconds=1))
    await _claim(store, owner="b")
    await _raises(
        OwnershipError,
        store.commit(KEY, _change("op2", 0, old, append_messages=_msgs("x"))),
        "чужая аренда важнее устаревшей ревизии",
    )


async def check_epoch_survives_delete(store, _clock: FakeClock) -> None:
    old = await _claim(store, owner="a")
    await store.delete(KEY, op_id="d")
    again = await _claim(store, owner="a")
    _expect(again.epoch > old.epoch, "эпоха переживает delete")
    await _raises(
        OwnershipError,
        store.commit(
            KEY, _change("ghost", 0, old, append_messages=_msgs("g"))
        ),
        "запись по аренде удалённого треда",
    )
    _expect(await _ids(store) == [], "призрачная запись не прошла")


async def check_untimed_lease(store: ThreadStore, clock: FakeClock) -> None:
    lease = await _claim(store, owner="wf", ttl=None)
    _expect(lease.expires_at is None, "ttl=None — бессрочная аренда")
    clock.advance(timedelta(days=365))
    await store.commit(
        KEY, _change("op", 0, lease, append_messages=_msgs("a"))
    )


# --- состояние, карточка, статус -------------------------------------------------


async def check_state_usage_info_roundtrip(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    state = SessionState(
        working_memory={"goal": "g"},
        todo=[{"content": "шаг", "status": "pending"}],
        user_turn_seq=2,
    )
    usage = Usage(by_category={"main": {"input": 1}}, currency="RUB")
    info = ThreadInfo(title="T", workspace="/w", api_mode="chat_completions")
    await store.commit(
        KEY,
        _change(
            "op", 0, lease, set_state=state, set_usage=usage, set_info=info
        ),
    )
    runtime = RuntimeStatus(
        turn="running",
        flags=frozenset({"summarizing"}),
        image_seq=3,
        probe="x",
    )
    await store.append_side(KEY, SideChange(op_id="s", set_runtime=runtime))
    record = await store.get_thread(KEY)
    _expect(record.state == state, "SessionState сохраняется как есть")
    _expect(record.usage == usage, "Usage с лишним ключом сохраняется")
    _expect(record.info.title == "T", "карточка сохраняется")
    _expect(record.info.workspace == "/w", "workspace сохраняется")
    _expect(record.runtime == runtime, "RuntimeStatus с лишним ключом")


def _sample(annotation: Any, field: str) -> Any:
    """Значение поля, отличное от умолчания, — по аннотации типа."""
    args = [a for a in get_args(annotation) if a is not type(None)]
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return _sample(args[0], field)
    if annotation is bool:
        return True
    if annotation is int:
        return 7
    if annotation is str:
        return f"{field}-значение"
    if annotation is Any:
        return {"field": field}
    if origin is dict:
        return {"k": _sample(args[1], field)}
    if origin is list:
        return [_sample(args[0], field)]
    raise ConformanceError(
        f"нет образца для поля {field} ({annotation}): допишите _sample"
    )


def _every_field_set(model: type[BaseModel]) -> BaseModel:
    """Модель, где каждое поле задано и отлично от умолчания: новое поле
    попадает в проверку само."""
    values = {
        name: _sample(info.annotation, name)
        for name, info in model.model_fields.items()
    }
    built = model(**values)
    for name, info in model.model_fields.items():
        if getattr(built, name) == info.get_default(call_default_factory=True):
            raise ConformanceError(f"образец поля {name} равен умолчанию")
    return built


async def check_every_state_field_roundtrip(store, _clock: FakeClock) -> None:
    """Каждое поле ``SessionState`` переживает запись и чтение: хранилище
    с раскладкой по своим ключам (файловое — ключи meta develop) не теряет
    поле, которого нет в его раскладке."""
    lease = await _claim(store)
    state = _every_field_set(SessionState)
    await store.commit(KEY, _change("op", 0, lease, set_state=state))
    stored = (await store.get_thread(KEY)).state
    lost = sorted(
        name
        for name in SessionState.model_fields
        if name not in stored.model_fields_set
        or getattr(stored, name) != getattr(state, name)
    )
    _expect(not lost, f"поля SessionState не пережили запись: {lost}")


async def check_commit_sets_runtime(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    runtime = RuntimeStatus(
        live_msg_count=2, live_history_chars=40, image_seq=1
    )
    await store.commit(
        KEY,
        _change(
            "op", 0, lease, append_messages=_msgs("a"), set_runtime=runtime
        ),
    )
    _expect(
        (await store.get_thread(KEY)).runtime == runtime,
        "commit пишет runtime вместе с шагом",
    )


async def check_explicit_fields_survive(store, _clock: FakeClock) -> None:
    """По ``model_fields_set`` фасад строит meta (ключ есть ⇔ поле задано):
    «не задано» и «задано значением по умолчанию» после чтения разные."""
    lease = await _claim(store)
    await store.commit(
        KEY,
        _change(
            "op",
            0,
            lease,
            set_state=SessionState(notes=""),
            set_runtime=RuntimeStatus(turn="idle"),
            set_info=ThreadInfo(title="t"),
            set_usage=Usage(),
        ),
    )
    record = await store.get_thread(KEY)
    _expect(
        record.state.model_fields_set == {"notes"},
        f"заданные поля state: {sorted(record.state.model_fields_set)}",
    )
    _expect(
        record.runtime.model_fields_set == {"turn"},
        f"заданные поля runtime: {sorted(record.runtime.model_fields_set)}",
    )
    info_set = record.info.model_fields_set
    _expect(
        "title" in info_set and "workspace" not in info_set,
        f"заданные поля info: {sorted(info_set)}",
    )
    _expect(
        not record.usage.model_fields_set,
        f"заданные поля usage: {sorted(record.usage.model_fields_set)}",
    )


async def check_non_finite_numbers_survive(store, _clock: FakeClock) -> None:
    """NaN и Infinity в данных (вывод тулов, расход) пишутся и читаются, как
    у файлового хранилища: ``json`` их принимает, отказ ронял бы ход."""
    lease = await _claim(store)
    nan, inf = float("nan"), float("inf")
    await store.commit(
        KEY,
        _change(
            "op",
            0,
            lease,
            append_messages=[{"id": "m", "v": nan, "w": -inf}],
            set_extra={"score": inf},
        ),
    )
    [entry] = await store.read_stream(KEY, "messages")
    value = entry.data["v"]
    _expect(value != value, "NaN в истории пережил хранилище")
    _expect(entry.data["w"] == -inf, "-Infinity в истории")
    record = await store.get_thread(KEY)
    _expect(record.extra.get("score") == inf, "Infinity в extra")


async def check_extra_roundtrip(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    extra = {"custom_section": {"owner": "сервер", "n": 1}}
    await store.commit(KEY, _change("op", 0, lease, set_extra=extra))
    record = await store.get_thread(KEY)
    _expect(
        record.extra.get("custom_section") == extra["custom_section"],
        "незнакомая секция meta сохраняется как есть",
    )


async def check_store_owns_timestamps_and_count(store, _clock: FakeClock):
    lease = await _claim(store)
    fake = ThreadInfo(created_at="x", updated_at="y", messages_count=99)
    await store.commit(
        KEY,
        _change(
            "op", 0, lease, set_info=fake, append_messages=_msgs("a", "b")
        ),
    )
    info = (await store.get_thread(KEY)).info
    _expect(info.created_at != "x", "created_at ставит хранилище")
    _expect(info.updated_at != "y", "updated_at ставит хранилище")
    _expect(info.messages_count == 2, "messages_count считает хранилище")


# --- замена истории -----------------------------------------------------------


async def check_replace_history_renumbers(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("m1", "m2", "m3"))
    )
    before = (await store.get_thread(KEY)).history_version
    summary = Compaction(messages=_msgs("s1"), archive=True)
    receipt = await store.commit(
        KEY, _change("op2", 1, lease, replace_history=summary)
    )
    _expect(receipt.archive_id is not None, "архив получает идентификатор")
    entries = await store.read_stream(KEY, "messages")
    _expect([e.seq for e in entries] == [1], "новая история нумеруется с 1")
    record = await store.get_thread(KEY)
    _expect(record.history_version == before + 1, "history_version + 1")
    _expect(
        [a.id for a in record.archives] == [receipt.archive_id],
        "архив в записи",
    )
    _expect(record.archives[0].messages_count == 3, "архив знает размер")
    archived = await store.read_archive(KEY, receipt.archive_id)
    _expect(
        [e.data["id"] for e in archived] == ["m1", "m2", "m3"],
        "архив — старая",
    )
    await store.commit(
        KEY, _change("op3", 2, lease, append_messages=_msgs("m4"))
    )
    tail = await store.read_stream(KEY, "messages")
    _expect([e.seq for e in tail] == [1, 2], "после замены seq растёт дальше")


async def check_discard_message(store: ThreadStore, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("m1", "m2", "m3"))
    )
    await store.commit(
        KEY, _change("op2", 1, lease, discard_message_ids=["m2"])
    )
    entries = await store.read_stream(KEY, "messages")
    _expect([e.data["id"] for e in entries] == ["m1", "m3"], "m2 удалено")
    _expect([e.seq for e in entries] == [1, 3], "seq остальных не меняются")


async def check_reset_history(store: ThreadStore, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a", "b"))
    )
    await store.append_side(
        KEY, SideChange(op_id="s", append=[("edits", [{"e": 1}])])
    )
    before = await store.get_thread(KEY)
    await store.commit(KEY, _change("op2", 1, lease, reset_history=True))
    after = await store.get_thread(KEY)
    _expect(await store.read_stream(KEY, "messages") == [], "история пуста")
    _expect(after.generation == before.generation + 1, "generation + 1")
    _expect(
        after.history_version == before.history_version + 1,
        "history_version + 1",
    )
    _expect(after.lease is None, "сброс снимает аренду")
    _expect(
        len(await store.read_stream(KEY, "edits")) == 1,
        "сброс истории не трогает другие потоки",
    )
    again = await _claim(store)
    await store.commit(
        KEY, _change("op3", 2, again, append_messages=_msgs("c"))
    )
    entries = await store.read_stream(KEY, "messages")
    _expect([e.seq for e in entries] == [1], "после сброса нумерация с 1")


async def check_commit_applies_in_order(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    await store.commit(
        KEY,
        _change(
            "op2", 1, lease, reset_history=True, append_messages=_msgs("z")
        ),
    )
    entries = await store.read_stream(KEY, "messages")
    _expect(
        [(e.seq, e.data["id"]) for e in entries] == [(1, "z")],
        "порядок в commit: сброс, замена, удаление, дописывание",
    )


async def check_replace_without_archive(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a", "b"))
    )
    replace = Compaction(messages=_msgs("s"), archive=False)
    receipt = await store.commit(
        KEY, _change("op2", 1, lease, replace_history=replace)
    )
    record = await store.get_thread(KEY)
    _expect(receipt.archive_id is None, "archive=False — без архива")
    _expect(record.archives == [], "архивов не прибавилось")
    _expect(await _ids(store) == ["s"], "история заменена")


async def check_seq_never_reused(store: ThreadStore, _clock: FakeClock):
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("m1", "m2", "m3"))
    )
    await store.commit(
        KEY, _change("op2", 1, lease, discard_message_ids=["m3"])
    )
    await store.commit(
        KEY, _change("op3", 2, lease, append_messages=_msgs("m4"))
    )
    tail = await store.read_stream(KEY, "messages", after_seq=3)
    _expect(
        [(e.seq, e.data["id"]) for e in tail] == [(4, "m4")],
        "seq удалённого хвоста не переиспользуется: читатель с курсора 3 "
        "иначе потерял бы m4",
    )


# --- запись сбоку ---------------------------------------------------------------


async def check_side_does_not_bump_revision(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    side = SideChange(
        op_id="s1",
        set_runtime=RuntimeStatus(turn="running"),
        set_frontend=("tui", {"status": "idle"}),
        append=[("edits", [{"e": 1}])],
    )
    await store.append_side(KEY, side)
    await store.append_side(KEY, side)
    record = await store.get_thread(KEY)
    _expect(record.revision == 1, "append_side не меняет revision")
    _expect(record.frontend.get("tui") == {"status": "idle"}, "frontend_state")
    _expect(len(await store.read_stream(KEY, "edits")) == 1, "повтор op_id")
    await store.commit(
        KEY, _change("op2", 1, lease, append_messages=_msgs("b"))
    )


async def check_turns_stream(store, _clock: FakeClock) -> None:
    """Итог хода — поток ядра ``turns``: пишется сбоку по аренде хода одной
    записью с коротким итогом в ``runtime.last_turn``; сброс и замена
    истории поток не трогают."""
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    runtime = RuntimeStatus(last_turn={"status": "done", "reason": None})
    entry = {"after": "a", "outcome": {"v": 1, "status": "done"}}
    await store.append_side(
        KEY,
        SideChange(
            op_id="t1",
            lease=lease,
            append=[("turns", [entry])],
            set_runtime=runtime,
        ),
    )
    record = await store.get_thread(KEY)
    _expect(record.runtime == runtime, "короткий итог хода в runtime")
    _expect(record.max_seq.get("turns") == 1, "max_seq потока итогов")
    _expect(
        [e.data for e in await store.read_stream(KEY, "turns")] == [entry],
        "запись итога хода читается как есть",
    )
    await store.commit(
        KEY,
        _change(
            "op2", 1, lease, replace_history=Compaction(messages=_msgs("s"))
        ),
    )
    await store.commit(KEY, _change("op3", 2, lease, reset_history=True))
    _expect(
        len(await store.read_stream(KEY, "turns")) == 1,
        "сжатие и сброс истории поток итогов не трогают",
    )


async def check_model_choices_stream(store, _clock: FakeClock) -> None:
    """Смены модели — поток ядра ``model_choices``: пишется сбоку по аренде
    хода; сброс истории поток не трогает."""
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    entry = {"after": "a", "model": {"v": 1, "choice": "max"}}
    await store.append_side(
        KEY,
        SideChange(
            op_id="m1", lease=lease, append=[("model_choices", [entry])]
        ),
    )
    await store.commit(KEY, _change("op2", 1, lease, reset_history=True))
    _expect(
        [e.data for e in await store.read_stream(KEY, "model_choices")]
        == [entry],
        "запись смены модели читается как есть и переживает сброс",
    )


async def check_patch_frontend_merges(store, _clock: FakeClock) -> None:
    await store.append_side(
        KEY,
        SideChange(op_id="s1", set_frontend=("tui", {"status": "idle"})),
    )
    await store.append_side(
        KEY,
        SideChange(op_id="s2", patch_frontend=("tui", {"branch": "main"})),
    )
    await store.append_side(
        KEY,
        SideChange(
            op_id="s3",
            patch_frontend=("tui", {"status": None, "skills": ["a"]}),
        ),
    )
    frontend = (await store.get_thread(KEY)).frontend
    _expect(
        frontend.get("tui")
        == {"status": None, "branch": "main", "skills": ["a"]},
        "patch_frontend сливает ключи (dict.update, null хранится)",
    )


async def check_side_creates_thread(store: ThreadStore, _clock: FakeClock):
    await store.append_side(
        KEY, SideChange(op_id="s", append=[("inbox", [{"text": "привет"}])])
    )
    record = await store.get_thread(KEY)
    _expect(record is not None, "append_side нового ключа создаёт тред")
    _expect(record.lease is None, "созданный сбоку тред — без аренды")
    _expect(len(await store.read_stream(KEY, "inbox")) == 1, "inbox записан")


async def check_side_cannot_touch_messages(store, _clock: FakeClock) -> None:
    # Модель SideChange такое не пропустит; хранилище обязано отказать и
    # само — его зовут и мимо модели (прокси, model_construct).
    bypass = SideChange.model_construct(
        op_id="s",
        set_runtime=None,
        set_frontend=None,
        append=[("messages", [{"id": "x"}])],
        add_aliases=[],
    )
    await _raises(
        ValueError, store.append_side(KEY, bypass), "история через append_side"
    )
    _expect(await _ids(store) == [], "отвергнутая запись ничего не пишет")


async def check_frontend_streams(
    store: ThreadStore, _clock: FakeClock
) -> None:
    await store.append_side(
        KEY,
        SideChange(op_id="s", append=[("chat.responses", [{"id": "resp_1"}])]),
    )
    _expect(
        await _ids(store, "chat.responses") == ["resp_1"], "поток фронтенда"
    )
    await _raises(
        ValueError,
        store.read_stream(KEY, "Bad Name"),
        "неверное имя потока на чтении",
    )


async def check_aliases(store: ThreadStore, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(KEY, _change("op", 0, lease, add_aliases=["resp_1"]))
    await store.append_side(KEY, SideChange(op_id="s", add_aliases=["resp_2"]))
    _expect(await store.resolve_alias("resp_1") == KEY, "алиас из commit")
    _expect(await store.resolve_alias("resp_2") == KEY, "алиас из append_side")
    await store.delete(KEY, op_id="d")
    _expect(await store.resolve_alias("resp_1") is None, "delete сносит алиас")


async def check_alias_collision(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(KEY, _change("op", 0, lease, add_aliases=["resp_1"]))
    other = ThreadKey(system="cli", thread="other")
    other_lease = await _claim(store, key=other)
    await _raises(
        ValueError,
        store.commit(
            other,
            _change(
                "op",
                0,
                other_lease,
                add_aliases=["resp_1"],
                append_messages=_msgs("x"),
            ),
        ),
        "алиас чужого треда",
    )
    _expect(await store.resolve_alias("resp_1") == KEY, "алиас не угнан")
    record = await store.get_thread(other)
    _expect(record.revision == 0, "отвергнутый commit ничего не пишет")


async def check_op_id_shared_by_commit_and_side(store, _clock) -> None:
    lease = await _claim(store)
    await store.commit(KEY, _change("x", 0, lease, append_messages=_msgs("a")))
    await _raises(
        OperationMismatch,
        store.append_side(KEY, SideChange(op_id="x")),
        "op_id треда общий у commit и append_side",
    )


async def check_commit_receipt_carries_lease(store, _clock) -> None:
    lease = await _claim(store)
    receipt = await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    got = receipt.lease
    _expect(
        got is not None
        and (got.owner, got.epoch) == (lease.owner, lease.epoch),
        "квитанция commit несёт аренду после записи",
    )
    reset = await store.commit(
        KEY, _change("op2", 1, lease, reset_history=True)
    )
    _expect(reset.lease is None, "после reset_history аренды в квитанции нет")


async def check_side_writes_to_durable_thread(store, _clock) -> None:
    """Тред durable-исполнителя (бессрочная аренда): статус хода, потоки и
    алиасы сбоку пишет только он (``SideChange.lease``), состояние
    фронтенда — любой процесс."""
    lease = await _claim(store, owner="wf", ttl=None)
    await store.append_side(
        KEY, SideChange(op_id="tui", patch_frontend=("tui", {"x": 1}))
    )
    for op_id, fields in (
        ("rt", {"set_runtime": RuntimeStatus(turn="running")}),
        ("tr", {"append": [("subagent_events", [{"k": 1}])]}),
        ("al", {"add_aliases": ["resp-1"]}),
    ):
        await _raises(
            OwnershipError,
            store.append_side(KEY, SideChange(op_id=op_id, **fields)),
            f"запись хода чужим в тред durable-исполнителя ({op_id})",
        )
    await store.append_side(
        KEY,
        SideChange(
            op_id="own", lease=lease, set_runtime=RuntimeStatus(turn="idle")
        ),
    )
    _expect(
        (await store.get_thread(KEY)).runtime.turn == "idle",
        "запись хода самим исполнителем проходит",
    )


async def check_side_receipt_has_no_lease(store, _clock: FakeClock) -> None:
    await _claim(store, owner="a")
    receipt = await store.append_side(KEY, SideChange(op_id="s"))
    _expect(
        receipt.lease is None, "квитанция append_side не раскрывает аренду"
    )


async def check_set_info_replaces(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, set_info=ThreadInfo(workspace="/w"))
    )
    await store.commit(
        KEY, _change("op2", 1, lease, set_info=ThreadInfo(title="t"))
    )
    info = (await store.get_thread(KEY)).info
    _expect(info.title == "t", "set_info записан")
    _expect(info.workspace is None, "set_* заменяет целиком, а не сливает")


async def check_set_info_keeps_title(store, _clock: FakeClock) -> None:
    """Заголовок ставит фронтенд; запись хода без ``title`` (поле не задано
    явно) его не трогает — иначе шаг стирал бы переименование треда."""
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, set_info=ThreadInfo(title="Мой тред"))
    )
    await store.commit(
        KEY,
        _change(
            "op2", 1, lease, set_info=ThreadInfo(api_mode="chat_completions")
        ),
    )
    info = (await store.get_thread(KEY)).info
    _expect(info.title == "Мой тред", "set_info без title не стирает его")
    _expect(info.api_mode == "chat_completions", "остальное — заменено")


async def check_updated_at_only_on_commit(store, clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    stamp = (await store.get_thread(KEY)).info.updated_at
    clock.advance(timedelta(seconds=5))
    await store.append_side(
        KEY,
        SideChange(op_id="s", set_runtime=RuntimeStatus(turn="running")),
    )
    await _claim(store)
    _expect(
        (await store.get_thread(KEY)).info.updated_at == stamp,
        "append_side и claim не двигают updated_at",
    )
    await store.commit(
        KEY, _change("op2", 1, lease, append_messages=_msgs("b"))
    )
    _expect(
        (await store.get_thread(KEY)).info.updated_at != stamp,
        "commit двигает updated_at",
    )


async def check_inbox_moves_updated_at(store, clock: FakeClock) -> None:
    """Сообщение во входящий поток — активность треда: задачу, простоявшую
    дольше окна восстановления и чистки, новое сообщение снова делает
    свежей (иначе обход «обновлённых за окно» её не увидит, а чистка по
    сроку удалит вместе с недоставленным)."""
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
    )
    stamp = (await store.get_thread(KEY)).info.updated_at
    clock.advance(timedelta(seconds=5))
    await store.append_side(
        KEY,
        SideChange(op_id="in", append=[("inbox", [{"text": "новое"}])]),
    )
    _expect(
        (await store.get_thread(KEY)).info.updated_at != stamp,
        "запись во inbox двигает updated_at",
    )


async def check_delete_spares_a_durable_thread(store, _clock: FakeClock):
    """Тред, который ведёт durable-исполнитель (бессрочная аренда), не
    удаляет никто другой — ни уборщик по сроку, ни фронтенд."""
    lease = await _claim(store, ttl=None)
    await store.commit(
        KEY, _change("op", 0, lease, append_messages=_msgs("a"))
    )
    await _raises(
        OwnershipError,
        store.delete(KEY, op_id="d"),
        "delete треда под бессрочной арендой",
    )
    _expect(await store.get_thread(KEY) is not None, "тред на месте")
    await store.release(KEY, lease)
    await store.delete(KEY, op_id="d2")
    _expect(await store.get_thread(KEY) is None, "после release — удалён")


async def check_stored_data_is_isolated(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    sent = _msgs("a")
    await store.commit(KEY, _change("op", 0, lease, append_messages=sent))
    sent[0]["content"] = "испорчено снаружи"
    read = await store.read_stream(KEY, "messages")
    read[0].data["content"] = "испорчено читателем"
    again = await store.read_stream(KEY, "messages")
    _expect(again[0].data["content"] == "текст a", "данные изолированы")


async def check_blobs(store: ThreadStore, _clock: FakeClock) -> None:
    await _claim(store)
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(16))
    ref = await store.put_blob(KEY, data, mime="image/png")
    _expect(ref.key == KEY and bool(ref.location), "ссылка на вложение")
    _expect(await store.get_blob(ref) == data, "те же байты")
    missing = BlobRef(key=KEY, name="nope.png", mime="image/png", location="x")
    await _raises(FileNotFoundError, store.get_blob(missing), "нет вложения")


# --- список и удаление ----------------------------------------------------------


async def _thread(store, system, thread, workspace=None) -> ThreadKey:
    key = ThreadKey(system=system, thread=thread)
    lease = await _claim(store, key=key)
    info = ThreadInfo(workspace=workspace)
    await store.commit(key, _change(f"op-{thread}", 0, lease, set_info=info))
    return key


async def check_list_threads_filters(store, clock: FakeClock) -> None:
    a = await _thread(store, "cli", "a", "/w1")
    clock.advance(timedelta(seconds=1))
    b = await _thread(store, "cli", "b", "/w2")
    clock.advance(timedelta(seconds=1))
    c = await _thread(store, "tui", "c")
    page = await store.list_threads(ThreadQuery(system="cli"))
    _expect([r.key for r in page.items] == [b, a], "system и порядок")
    page = await store.list_threads(ThreadQuery(workspace="/w1"))
    _expect([r.key for r in page.items] == [a], "фильтр workspace")
    pivot = (await store.get_thread(b)).info.updated_at
    page = await store.list_threads(ThreadQuery(updated_after=pivot))
    _expect([r.key for r in page.items] == [c], "updated_after строго")
    page = await store.list_threads(ThreadQuery(updated_before=pivot))
    _expect([r.key for r in page.items] == [a], "updated_before строго")


async def check_list_pagination_is_stable(store, _clock: FakeClock) -> None:
    keys = {await _thread(store, "cli", f"t{n}") for n in range(5)}
    seen, cursor, pages = [], None, 0
    while True:
        page = await store.list_threads(ThreadQuery(limit=2, cursor=cursor))
        seen.extend(r.key for r in page.items)
        pages += 1
        cursor = page.next_cursor
        if cursor is None or pages > 10:
            break
    _expect(pages == 3, "5 тредов по 2 — три страницы")
    _expect(len(seen) == 5 and set(seen) == keys, "без потерь и повторов")


async def check_list_rejects_broken_cursor(store, _clock: FakeClock):
    """Чужой или испорченный курсор списка — ``InvalidCursor`` (у API —
    400), а не падение разбора и не тихая первая страница."""
    from gc_core.threads.errors import InvalidCursor

    lease = await _claim(store)
    await store.commit(
        KEY, _change("op", 0, lease, append_messages=_msgs("a"))
    )
    for broken in ("не-курсор", "нет|даты|x", "2026-01-01T00:00:00Z"):
        await _raises(
            InvalidCursor,
            store.list_threads(ThreadQuery(cursor=broken)),
            f"курсор {broken!r}",
        )


async def check_delete(store: ThreadStore, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op", 0, lease, append_messages=_msgs("a"))
    )
    before = (await store.get_thread(KEY)).generation
    await store.delete(KEY, op_id="d")
    _expect(await store.get_thread(KEY) is None, "delete — треда нет")
    _expect(await store.read_stream(KEY, "messages") == [], "история удалена")
    record = await store.claim(KEY, LeaseRequest(owner="a", ttl=MINUTE))
    _expect(record.generation > before, "новое поколение после delete")


async def check_prune_receipts(store: ThreadStore, clock: FakeClock) -> None:
    lease = await _claim(store, ttl=None)
    old = _change("op1", 0, lease, append_messages=_msgs("a"))
    await store.commit(KEY, old)
    clock.advance(timedelta(days=8))
    fresh = _change("op2", 1, lease, append_messages=_msgs("b"))
    receipt = await store.commit(KEY, fresh)
    pruned = await store.prune_receipts(timedelta(days=7))
    # Старую квитанцию могло убрать и само хранилище (файловое чистит
    # квитанции треда при записи, раз в сутки).
    _expect(pruned in (0, 1), "удаляется только старая квитанция")
    _expect(await store.commit(KEY, fresh) == receipt, "свежая живёт")
    # Квитанции нет — повтор защищает ревизия, а не тихая повторная запись.
    await _raises(ThreadConflict, store.commit(KEY, old), "повтор после prune")
    _expect(await _ids(store) == ["a", "b"], "история не задвоилась")


async def check_watch_default(store: ThreadStore, _clock: FakeClock) -> None:
    lease = await _claim(store)
    seen: list[int] = []

    async def consume():
        async for entry in store.watch(
            KEY, "messages", after_seq=0, poll_interval=0
        ):
            seen.append(entry.seq)

    async def until(count: int) -> None:
        while len(seen) < count:
            await asyncio.sleep(0)

    task = asyncio.create_task(consume())
    try:
        await store.commit(
            KEY, _change("op1", 0, lease, append_messages=_msgs("a"))
        )
        await asyncio.wait_for(until(1), _WAIT)
        await store.commit(
            KEY, _change("op2", 1, lease, append_messages=_msgs("b"))
        )
        await asyncio.wait_for(until(2), _WAIT)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _expect(seen == [1, 2], "watch отдаёт новые записи по порядку")


async def check_watch_across_replace(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("m1", "m2"))
    )
    # Курсор читателя — явно (как в check_watch_across_delete): отметка
    # «на момент старта» снимается первым опросом, а у хранилища с
    # настоящим вводом-выводом замена истории может его опередить.
    record = await store.get_thread(KEY)
    seen: list[str] = []

    async def consume():
        async for entry in store.watch(
            KEY,
            "messages",
            after_seq=2,
            generation=record.generation,
            history_version=record.history_version,
            poll_interval=0,
        ):
            seen.append(entry.data["id"])

    async def until(count: int) -> None:
        while len(seen) < count:
            await asyncio.sleep(0)

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0)
        summary = Compaction(messages=_msgs("s1"), archive=True)
        await store.commit(
            KEY, _change("op2", 1, lease, replace_history=summary)
        )
        await asyncio.wait_for(until(1), _WAIT)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _expect(
        seen == ["s1"],
        "после замены истории watch начинает новую историю с начала",
    )


async def check_watch_across_delete(store, _clock: FakeClock) -> None:
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op1", 0, lease, append_messages=_msgs("m1", "m2"))
    )
    record = await store.get_thread(KEY)
    seen: list[str] = []

    async def consume():
        async for entry in store.watch(
            KEY,
            "messages",
            after_seq=2,
            generation=record.generation,
            history_version=record.history_version,
            poll_interval=0,
        ):
            seen.append(entry.data["id"])

    async def until(count: int) -> None:
        while len(seen) < count:
            await asyncio.sleep(0)

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0)
        await store.delete(KEY, op_id="d")
        again = await _claim(store)
        await store.commit(
            KEY, _change("op2", 0, again, append_messages=_msgs("n1"))
        )
        await asyncio.wait_for(until(1), _WAIT)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _expect(seen == ["n1"], "после удаления тред читается с начала")


# --- конкурентная запись ---------------------------------------------------
#
# Два писателя на одном хранилище одновременно (``asyncio.gather``): у
# хранилищ с пулом соединений — настоящая гонка в базе, у файлового и
# in-memory — чередование на точках ожидания.


async def check_concurrent_same_revision(
    store: ThreadStore, _clock: FakeClock
) -> None:
    lease = (await store.claim(KEY, LeaseRequest(owner="w", ttl=MINUTE))).lease
    outcomes = await asyncio.gather(
        *(
            store.commit(
                KEY,
                ThreadChange(
                    op_id=f"c{i}",
                    expected_revision=0,
                    lease=lease,
                    append_messages=[{"content": str(i)}],
                ),
            )
            for i in range(2)
        ),
        return_exceptions=True,
    )
    conflicts = [o for o in outcomes if isinstance(o, ThreadConflict)]
    _expect(
        len(conflicts) == 1,
        f"две записи по ревизии 0: ровно одна — ThreadConflict, а {outcomes!r}",
    )
    entries = await store.read_stream(KEY, "messages")
    _expect(len(entries) == 1, f"записалось не одно сообщение: {entries!r}")


async def check_concurrent_same_op_id(
    store: ThreadStore, _clock: FakeClock
) -> None:
    lease = (await store.claim(KEY, LeaseRequest(owner="w", ttl=MINUTE))).lease
    change = ThreadChange(
        op_id="same",
        expected_revision=0,
        lease=lease,
        append_messages=[{"content": "x"}],
    )
    first, second = await asyncio.gather(
        store.commit(KEY, change), store.commit(KEY, change)
    )
    _expect(first == second, "один op_id одновременно — разные квитанции")
    entries = await store.read_stream(KEY, "messages")
    _expect(len(entries) == 1, "один op_id одновременно записан дважды")


async def check_concurrent_alias_race(
    store: ThreadStore, _clock: FakeClock
) -> None:
    other = ThreadKey(system="cli", thread="other")

    async def take(key):
        record = await store.claim(
            key, LeaseRequest(owner=key.thread, ttl=MINUTE)
        )
        return await store.commit(
            key,
            ThreadChange(
                op_id=f"a-{key.thread}",
                expected_revision=0,
                lease=record.lease,
                add_aliases=["shared"],
            ),
        )

    outcomes = await asyncio.gather(
        take(KEY), take(other), return_exceptions=True
    )
    refused = [o for o in outcomes if isinstance(o, ValueError)]
    _expect(
        len(refused) == 1,
        f"общий алиас достался обоим тредам или никому: {outcomes!r}",
    )
    owner = await store.resolve_alias("shared")
    winner = KEY if not isinstance(outcomes[0], ValueError) else other
    _expect(owner == winner, f"алиас разрешается в {owner}, а не {winner}")


async def check_concurrent_create(
    store: ThreadStore, _clock: FakeClock
) -> None:
    await asyncio.gather(
        store.append_side(
            KEY, SideChange(op_id="s1", set_frontend=("a", {"x": 1}))
        ),
        store.append_side(
            KEY, SideChange(op_id="s2", set_frontend=("b", {"y": 2}))
        ),
    )
    record = await store.get_thread(KEY)
    _expect(
        record is not None and set(record.frontend) == {"a", "b"},
        "одновременное создание треда потеряло запись сбоку",
    )


async def check_conditional_frontend(store: ThreadStore, clock: FakeClock):
    del clock
    await store.append_side(
        KEY,
        SideChange(
            op_id="first",
            expected_frontend={},
            patch_frontend=("a", {"owner": "one"}),
        ),
    )
    try:
        await store.append_side(
            KEY,
            SideChange(
                op_id="stale",
                expected_frontend={},
                patch_frontend=("a", {"owner": "two"}),
                append=[("inbox", [{"content": "must not land"}])],
            ),
        )
    except ThreadConflict:
        pass
    else:
        raise ConformanceError("устаревший снимок frontend принят")
    record = await store.get_thread(KEY)
    _expect(
        record.frontend == {"a": {"owner": "one"}}, "потерян победитель CAS"
    )
    _expect(
        not await store.read_stream(KEY, "inbox"), "отказ CAS оставил входящее"
    )


async def check_lease_token_fences_commit_and_release(
    store: ThreadStore, clock: FakeClock
):
    del clock
    old = await _claim(store)
    await store.release(KEY, old)
    await store.delete(KEY, op_id="delete")
    current = (
        await store.claim(KEY, LeaseRequest(owner=old.owner, ttl=MINUTE))
    ).lease
    _expect(
        current.token is not None and current.token != old.token,
        "повтор токена аренды",
    )
    forged = old.model_copy(update={"epoch": current.epoch})
    await store.release(KEY, forged)
    _expect(
        (await store.get_thread(KEY)).lease == current,
        "старая аренда сняла новую",
    )
    try:
        await store.commit(
            KEY, _change("stale", 0, forged, append_messages=_msgs("old"))
        )
    except OwnershipError:
        pass
    else:
        raise ConformanceError("старое удостоверение записало новый тред")


CHECKS: tuple[Callable[..., Awaitable[None]], ...] = (
    check_missing_thread,
    check_claim_creates_thread,
    check_commit_requires_claim,
    check_append_and_read_messages,
    check_revision_conflict,
    check_op_id_idempotent,
    check_op_id_mismatch,
    check_receipt_beats_lease,
    check_lease_expiry_and_fencing,
    check_claim_held_by_other,
    check_claim_extends_own,
    check_claim_after_own_expiry_bumps_epoch,
    check_release,
    check_untimed_lease,
    check_state_usage_info_roundtrip,
    check_every_state_field_roundtrip,
    check_commit_sets_runtime,
    check_extra_roundtrip,
    check_explicit_fields_survive,
    check_non_finite_numbers_survive,
    check_store_owns_timestamps_and_count,
    check_replace_history_renumbers,
    check_discard_message,
    check_reset_history,
    check_side_does_not_bump_revision,
    check_side_creates_thread,
    check_patch_frontend_merges,
    check_turns_stream,
    check_model_choices_stream,
    check_side_cannot_touch_messages,
    check_frontend_streams,
    check_aliases,
    check_blobs,
    check_list_threads_filters,
    check_list_pagination_is_stable,
    check_list_rejects_broken_cursor,
    check_delete,
    check_prune_receipts,
    check_watch_default,
    check_watch_across_replace,
    check_watch_across_delete,
    check_expired_lease_rejected_without_takeover,
    check_commit_with_pre_extension_lease,
    check_lease_checked_before_revision,
    check_epoch_survives_delete,
    check_commit_applies_in_order,
    check_replace_without_archive,
    check_seq_never_reused,
    check_alias_collision,
    check_op_id_shared_by_commit_and_side,
    check_side_receipt_has_no_lease,
    check_commit_receipt_carries_lease,
    check_side_writes_to_durable_thread,
    check_set_info_replaces,
    check_set_info_keeps_title,
    check_updated_at_only_on_commit,
    check_inbox_moves_updated_at,
    check_delete_spares_a_durable_thread,
    check_stored_data_is_isolated,
    check_concurrent_same_revision,
    check_concurrent_same_op_id,
    check_concurrent_alias_race,
    check_concurrent_create,
    check_conditional_frontend,
    check_lease_token_fences_commit_and_release,
)


async def _run_closing(check, store: ThreadStore, clock: FakeClock) -> None:
    try:
        await check(store, clock)
    finally:
        close = getattr(store, "close", None)
        if inspect.iscoroutinefunction(close):
            await close()


def assert_conforms(factory: Callable[[FakeClock], ThreadStore]) -> list[str]:
    """Прогнать все проверки; вернуть имена пройденных.

    Каждая проверка — свежие часы и свежее хранилище. Первая нарушенная
    поднимает :class:`ConformanceError` с именем проверки.
    """
    passed: list[str] = []
    for check in CHECKS:
        clock = FakeClock()
        store = factory(clock)
        try:
            asyncio.run(_run_closing(check, store, clock))
        except ConformanceError as exc:
            raise ConformanceError(f"{check.__name__}: {exc}") from exc
        passed.append(check.__name__)
    return passed


async def acheck_prune_drops_old_tombstones(
    store: ThreadStore, clock: FakeClock
) -> None:
    """Надгробие удалённого треда (поколение и эпоха для фенсинга) живёт
    сутки: ``prune_receipts`` убирает старые, и тред с тем же ключом
    заводится с нуля. Свежее надгробие остаётся. Проверка не входит в
    общий набор: файловое хранилище чистит надгробия по mtime, а не по
    часам контракта."""
    lease = await _claim(store)
    await store.commit(
        KEY, _change("op", 0, lease, append_messages=_msgs("a"))
    )
    await store.delete(KEY, op_id="d")
    await store.prune_receipts(timedelta(days=7))
    fresh = await store.claim(KEY, LeaseRequest(owner="a", ttl=MINUTE))
    _expect(fresh.generation == 1, "свежее надгробие держит поколение")
    await store.release(KEY, fresh.lease)
    await store.delete(KEY, op_id="d2")
    clock.advance(timedelta(days=2))
    await store.prune_receipts(timedelta(days=7))
    reborn = await store.claim(KEY, LeaseRequest(owner="a", ttl=MINUTE))
    _expect(reborn.generation == 0, "старое надгробие убрано")
