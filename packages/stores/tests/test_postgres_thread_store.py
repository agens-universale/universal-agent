"""То, чего конформанс не видит: два инстанса на одной базе, данные байт в
байт, часы базы, создание схемы."""

import asyncio
import builtins
import json
import math
import uuid
from datetime import timedelta

import pytest
from gc_core.threads.errors import ThreadConflict
from gc_core.threads.models import (
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
)
from gc_core.threads.rules import instant
from gc_stores.postgres import PostgresThreadStore

KEY = ThreadKey(system="cli", thread="pg")
TTL = timedelta(seconds=60)


async def _claim(store, owner="a", key=KEY):
    record = await store.claim(key, LeaseRequest(owner=owner, ttl=TTL))
    return record.lease


def _change(op_id, lease, *ids, revision=0):
    return ThreadChange(
        op_id=op_id,
        expected_revision=revision,
        lease=lease,
        append_messages=[{"id": i, "role": "user", "content": i} for i in ids],
    )


async def _ids(store, key=KEY):
    return [e.data["id"] for e in await store.read_stream(key, "messages")]


@pytest.fixture
async def second_instance(postgres_dsn, pg_schema):
    """Второй инстанс: свой пул на той же схеме."""
    async with PostgresThreadStore(postgres_dsn, schema=pg_schema) as store:
        yield store


async def test_two_instances_same_revision_one_wins(pg_store, second_instance):
    lease = await _claim(pg_store)
    results = await asyncio.gather(
        pg_store.commit(KEY, _change("op-a", lease, "a")),
        second_instance.commit(KEY, _change("op-b", lease, "b")),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, ThreadConflict)]
    assert len(conflicts) == 1, results
    assert await _ids(pg_store) in (["a"], ["b"])
    assert (await pg_store.get_thread(KEY)).revision == 1


async def test_same_op_id_twice_at_once_writes_once(pg_store, second_instance):
    """Повтор после обрыва ответа приходит, пока первый ещё пишет."""
    lease = await _claim(pg_store)
    change = _change("op", lease, "m1", "m2")
    first, again = await asyncio.gather(
        pg_store.commit(KEY, change), second_instance.commit(KEY, change)
    )
    assert first == again
    assert await _ids(pg_store) == ["m1", "m2"]


async def test_alias_race_has_one_owner(pg_store, second_instance):
    other = ThreadKey(system="cli", thread="pg-other")
    results = await asyncio.gather(
        pg_store.append_side(KEY, SideChange(op_id="s1", add_aliases=["r"])),
        second_instance.append_side(
            other, SideChange(op_id="s2", add_aliases=["r"])
        ),
        return_exceptions=True,
    )
    failed = [r for r in results if isinstance(r, ValueError)]
    assert len(failed) == 1, results
    owner = await pg_store.resolve_alias("r")
    loser = other if failed[0] is results[1] else KEY
    assert owner is not None and owner != loser
    # Проигравший не записал ничего: даже тред не создан.
    assert await pg_store.get_thread(loser) is None


async def test_history_reads_back_exactly(pg_store):
    """NUL в выводе тула и порядок ключей переживают базу (тип json)."""
    lease = await _claim(pg_store)
    message = {
        "id": "m",
        "z": "вывод\x00с нулём",
        "a": [1.5, {"y": None, "b": "ж"}],
        "type": "function_call_output",
    }
    await pg_store.commit(
        KEY,
        ThreadChange(
            op_id="op",
            expected_revision=0,
            lease=lease,
            append_messages=[message],
        ),
    )
    [entry] = await pg_store.read_stream(KEY, "messages")
    assert json.dumps(entry.data, ensure_ascii=False) == json.dumps(
        message, ensure_ascii=False
    )


async def _db_now(postgres_dsn):
    import asyncpg

    conn = await asyncpg.connect(postgres_dsn)
    try:
        return await conn.fetchval("SELECT clock_timestamp()")
    finally:
        await conn.close()


async def test_database_clock_counts_lease(pg_store, postgres_dsn):
    before = await _db_now(postgres_dsn)
    record = await pg_store.claim(KEY, LeaseRequest(owner="a", ttl=TTL))
    after = await _db_now(postgres_dsn)
    assert before + TTL <= record.lease.expires_at <= after + TTL
    assert before <= instant(record.info.created_at) <= after


async def test_write_that_waited_for_the_lock_is_stamped_after_it(
    pg_store, postgres_dsn, pg_schema
):
    """Время записи — после захвата строки, а не начала транзакции:
    иначе ``updated_at`` откатывался бы назад, и инкрементальный опрос
    списка (``updated_after``) терял бы запись."""
    import asyncpg

    lease = await _claim(pg_store)
    holder = await asyncpg.connect(postgres_dsn)
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute(
            f'SELECT 1 FROM "{pg_schema}".gc_threads '
            "WHERE system = $1 AND thread_id = $2 FOR UPDATE",
            KEY.system,
            KEY.thread,
        )
        locked_at = await holder.fetchval("SELECT clock_timestamp()")
        writer = asyncio.create_task(
            pg_store.commit(KEY, _change("op", lease, "m"))
        )
        await asyncio.sleep(0.3)
        await tx.commit()
        await writer
    finally:
        await holder.close()
    updated = instant((await pg_store.get_thread(KEY)).info.updated_at)
    assert updated - locked_at >= timedelta(seconds=0.25)


async def test_races_hold_under_repeatable_read_default(
    postgres_dsn, pg_schema
):
    """``default_transaction_isolation = repeatable read`` на сервере (частая
    настройка DBA) не превращает обычные гонки в ``SerializationError``."""
    options = {
        "server_settings": {"default_transaction_isolation": "repeatable read"}
    }
    stores = [
        PostgresThreadStore(
            postgres_dsn, schema=pg_schema, pool_options=options
        )
        for _ in "ab"
    ]
    try:
        lease = await _claim(stores[0])
        results = await asyncio.gather(
            stores[0].commit(KEY, _change("op-a", lease, "a")),
            stores[1].commit(KEY, _change("op-b", lease, "b")),
            return_exceptions=True,
        )
        assert sorted(type(r).__name__ for r in results) == [
            "Receipt",
            "ThreadConflict",
        ], results
        side = SideChange(op_id="s", patch_frontend=("tui", {"k": 1}))
        other = ThreadKey(system="cli", thread="pg-new")
        first, again = await asyncio.gather(
            stores[0].append_side(other, side),
            stores[1].append_side(other, side),
        )
        assert first == again
    finally:
        await asyncio.gather(*(s.close() for s in stores))


async def test_thread_deleted_between_create_and_lock_is_created_again(
    postgres_dsn, pg_schema, pg_store
):
    """Вставку опередил параллельный вызов, а его тред успели удалить до
    нашей блокировки: ``claim`` создаёт тред заново, а не падает."""
    import asyncpg
    from gc_stores.postgres.rows import ThreadRow

    reached, go = asyncio.Event(), asyncio.Event()

    class Racer(PostgresThreadStore):
        locks = 0

        async def _lock(self, conn, key):
            Racer.locks += 1
            if Racer.locks == 2:
                reached.set()
                await go.wait()
            return await super()._lock(conn, key)

    holder = await asyncpg.connect(postgres_dsn)
    racer = Racer(postgres_dsn, schema=pg_schema)
    tx, committed = holder.transaction(), False
    try:
        await tx.start()
        now = await holder.fetchval("SELECT clock_timestamp()")
        row = ThreadRow(key=KEY, created_at=now, updated_at=now)
        await holder.execute(pg_store._sql.insert_thread, *row.values())
        claim = asyncio.create_task(
            racer.claim(KEY, LeaseRequest(owner="b", ttl=TTL))
        )
        await _until_someone_waits_for_lock(postgres_dsn)
        await tx.commit()
        committed = True
        await asyncio.wait_for(reached.wait(), 5)
        await pg_store.delete(KEY, op_id="d")
        go.set()
        record = await asyncio.wait_for(claim, 5)
    finally:
        # Держатель отпускает строку до закрытия пула гонщика: иначе пул
        # ждал бы соединение, застрявшее на блокировке, — тест повис бы.
        if not committed:
            await tx.rollback()
        go.set()
        await holder.close()
        await asyncio.wait_for(racer.close(), 10)
    assert record.lease is not None and record.lease.owner == "b"
    assert record.generation == 1, "тред — новое поколение после delete"


async def _until_someone_waits_for_lock(postgres_dsn) -> None:
    """Отдельное соединение без транзакции: внутри транзакции Postgres
    отдаёт ``pg_stat_activity`` одним снимком до её конца."""
    import asyncpg

    conn = await asyncpg.connect(postgres_dsn)
    try:
        for _ in range(250):
            waiting = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock'"
            )
            if waiting:
                return
            await asyncio.sleep(0.02)
    finally:
        await conn.close()
    raise AssertionError("вторая транзакция так и не встала на блокировку")


async def test_shared_aliases_in_any_order_do_not_deadlock(
    pg_store, second_instance
):
    """Две записи на разные треды с общими алиасами в разном порядке: одна
    проходит, вторая — ``ValueError``, а не ``DeadlockDetectedError``."""
    # Пулы прогреты, алиасов много: вставки двух транзакций идут
    # одновременно, и разный порядок без сортировки давал взаимное ожидание.
    await pg_store.get_thread(KEY)
    await second_instance.get_thread(KEY)
    for n in range(3):
        a = ThreadKey(system="cli", thread=f"al-a{n}")
        b = ThreadKey(system="cli", thread=f"al-b{n}")
        aliases = [f"x{n}-{i}" for i in range(50)]
        results = await asyncio.gather(
            pg_store.append_side(
                a, SideChange(op_id="s", add_aliases=aliases)
            ),
            second_instance.append_side(
                b, SideChange(op_id="s", add_aliases=aliases[::-1])
            ),
            return_exceptions=True,
        )
        failed = [r for r in results if isinstance(r, Exception)]
        assert len(failed) == 1 and isinstance(failed[0], ValueError), results


async def test_non_finite_numbers_and_odd_ids_survive(pg_store):
    """NaN/Infinity — как у файлового хранилища (``json`` их пишет и
    читает); id сообщения с NUL или длиннее строки индекса удаляется."""
    lease = await _claim(pg_store)
    odd = ["a\x00b", "x" * 20_000]
    messages = [
        {"id": odd[0], "v": float("nan")},
        {"id": odd[1], "v": float("inf")},
        {"id": "keep", "v": -float("inf")},
    ]
    await pg_store.commit(
        KEY,
        ThreadChange(
            op_id="op",
            expected_revision=0,
            lease=lease,
            append_messages=messages,
            set_extra={"score": float("nan")},
        ),
    )
    read = [e.data for e in await pg_store.read_stream(KEY, "messages")]
    assert math.isnan(read[0]["v"]) and read[1]["v"] == float("inf")
    assert math.isnan((await pg_store.get_thread(KEY)).extra["score"])
    await pg_store.commit(
        KEY,
        ThreadChange(
            op_id="op2",
            expected_revision=1,
            lease=lease,
            discard_message_ids=odd,
        ),
    )
    assert await _ids(pg_store) == ["keep"]
    assert (await pg_store.get_thread(KEY)).info.messages_count == 1


def test_store_outlives_the_loop_it_was_opened_in(postgres_dsn, schemas):
    """``asyncio.run(store.ensure_schema())`` на старте, дальше — свой loop
    приложения: пул закрытого loop брошен, открывается новый."""
    store = PostgresThreadStore(postgres_dsn, schema=schemas.name())
    asyncio.run(store.ensure_schema())
    asyncio.run(_claim(store))

    async def read_and_close():
        try:
            return await store.get_thread(KEY)
        finally:
            await store.close()

    assert asyncio.run(read_and_close()) is not None


async def test_facade_threads_over_postgres(pg_store):
    """Фасад ``Threads`` (фронтенды) поверх Postgres: история с повтором,
    состояние фронтенда, поток фронтенда с алиасом, удаление."""
    from gc_core.threads.facade import Threads

    threads = Threads(pg_store, owner="inst-1")
    messages = [{"id": "m1", "role": "user", "content": "привет"}]
    assert await threads.append_messages(KEY, messages) == 1
    assert await threads.append_messages(KEY, messages) == 0
    await threads.update_frontend(KEY, "tui", {"title": "t"})
    await threads.append_frontend(
        KEY, "chat.responses", [{"id": "r1"}], aliases=["resp-1"]
    )
    assert [e.data["id"] for e in await threads.history(KEY)] == ["m1"]
    assert (await threads.get(KEY)).frontend["tui"] == {"title": "t"}
    assert await threads.resolve("resp-1") == KEY
    listed = [r.key async for r in threads.iter_threads(system="cli")]
    assert listed == [KEY]
    await threads.delete(KEY)
    assert await threads.get(KEY) is None


async def test_ensure_schema_twice_and_at_once(postgres_dsn, schemas):
    name = schemas.name()
    stores = [PostgresThreadStore(postgres_dsn, schema=name) for _ in "ab"]
    try:
        await asyncio.gather(*(s.ensure_schema() for s in stores))
        await stores[0].ensure_schema()
        await _claim(stores[1])
        assert await stores[0].get_thread(KEY) is not None
    finally:
        await asyncio.gather(*(s.close() for s in stores))


async def test_ensure_schema_for_user_without_create_right(
    postgres_dsn, pg_schema
):
    """Таблицы создал администратор, у пользователя приложения права
    ``CREATE`` на схему нет: старт (``ensure_schema``) и запись проходят."""
    import asyncpg

    role, password = f"gc_app_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    admin = await asyncpg.connect(postgres_dsn)
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("у пользователя теста нет права создавать роли")
    try:
        await admin.execute(f'GRANT USAGE ON SCHEMA "{pg_schema}" TO {role}')
        await admin.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            f'IN SCHEMA "{pg_schema}" TO {role}'
        )
        app = PostgresThreadStore(
            postgres_dsn,
            schema=pg_schema,
            pool_options={"user": role, "password": password},
        )
        try:
            await app.ensure_schema()
            lease = await _claim(app)
            await app.commit(KEY, _change("op", lease, "m"))
            assert await _ids(app) == ["m"]
        finally:
            await app.close()
    finally:
        await admin.execute(f"DROP OWNED BY {role}")
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()


async def test_foreign_pool_stays_open(postgres_dsn, pg_schema):
    import asyncpg

    pool = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=2)
    try:
        store = PostgresThreadStore(pool=pool, schema=pg_schema)
        await _claim(store)
        await store.close()
        assert await store.get_thread(KEY) is not None
    finally:
        await pool.close()


# --- без базы --------------------------------------------------------------------


@pytest.mark.parametrize("schema", ["", "1x", 'a"; drop', "x" * 64])
def test_bad_schema_name_refused(schema):
    with pytest.raises(ValueError, match="имя схемы"):
        PostgresThreadStore("postgresql://localhost/db", schema=schema)


def test_needs_exactly_one_of_dsn_and_pool():
    with pytest.raises(ValueError, match="ровно один"):
        PostgresThreadStore()
    with pytest.raises(ValueError, match="ровно один"):
        PostgresThreadStore("postgresql://localhost/db", pool=object())


async def test_missing_driver_names_the_extra(monkeypatch):
    real_import = builtins.__import__

    def no_asyncpg(name, *args, **kwargs):
        if name == "asyncpg":
            raise ImportError("No module named 'asyncpg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_asyncpg)
    store = PostgresThreadStore(f"postgresql://localhost/{uuid.uuid4().hex}")
    with pytest.raises(ImportError, match=r"gc-stores\[postgres\]"):
        await store.get_thread(KEY)
