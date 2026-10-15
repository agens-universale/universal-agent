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
from typing import Any

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
    _expect(pruned == 1, "удаляется ровно старая квитанция")
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
    seen: list[str] = []

    async def consume():
        async for entry in store.watch(
            KEY, "messages", after_seq=2, poll_interval=0
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
    check_side_cannot_touch_messages,
    check_frontend_streams,
    check_aliases,
    check_blobs,
    check_list_threads_filters,
    check_list_pagination_is_stable,
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
    check_set_info_replaces,
    check_updated_at_only_on_commit,
    check_stored_data_is_isolated,
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
