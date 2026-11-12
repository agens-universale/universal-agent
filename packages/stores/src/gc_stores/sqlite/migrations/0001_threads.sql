-- Схема SQLiteThreadStore (контракт ThreadStore v2, gc_core/threads/store.py).
--
-- Миграция 1: таблицы хранилища тредов. Применяется при открытии
-- хранилища; режим журнала (WAL) ставит само хранилище — вне транзакции.
-- Документы — текст JSON ровно как записан (порядок ключей, \u0000, NaN),
-- время — ISO-8601 UTC фиксированной ширины (лексикографический порядок
-- совпадает с хронологическим). Раскладка — как у PostgresThreadStore.

CREATE TABLE IF NOT EXISTS gc_threads (
    system           TEXT    NOT NULL,
    thread_id        TEXT    NOT NULL,
    workspace        TEXT,
    info             TEXT    NOT NULL,
    state            TEXT    NOT NULL,
    usage            TEXT    NOT NULL,
    runtime          TEXT    NOT NULL,
    frontend         TEXT    NOT NULL,
    archives         TEXT    NOT NULL,
    extra            TEXT    NOT NULL,
    max_seq          TEXT    NOT NULL,
    messages_count   INTEGER NOT NULL,
    revision         INTEGER NOT NULL,
    generation       INTEGER NOT NULL,
    history_version  INTEGER NOT NULL,
    lease_owner      TEXT,
    lease_epoch      INTEGER NOT NULL,
    lease_expires_at TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (system, thread_id)
);

CREATE INDEX IF NOT EXISTS gc_threads_by_updated
    ON gc_threads (updated_at DESC, system, thread_id);

CREATE TABLE IF NOT EXISTS gc_stream_entries (
    system    TEXT    NOT NULL,
    thread_id TEXT    NOT NULL,
    stream    TEXT    NOT NULL,
    seq       INTEGER NOT NULL,
    ts        TEXT    NOT NULL,
    msg_key   TEXT,
    data      TEXT    NOT NULL,
    PRIMARY KEY (system, thread_id, stream, seq)
);

CREATE INDEX IF NOT EXISTS gc_stream_entries_by_msg_key
    ON gc_stream_entries (system, thread_id, msg_key)
    WHERE msg_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS gc_archives (
    system     TEXT    NOT NULL,
    thread_id  TEXT    NOT NULL,
    archive_id TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    ts         TEXT    NOT NULL,
    data       TEXT    NOT NULL,
    PRIMARY KEY (system, thread_id, archive_id, seq)
);

-- Вложения треда: put_blob (имя по содержимому) и файлы треда
-- (write_thread_file — картинки и медиа ядра).
CREATE TABLE IF NOT EXISTS gc_blobs (
    system     TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    mime       TEXT NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (system, thread_id, name)
);

CREATE TABLE IF NOT EXISTS gc_aliases (
    alias     TEXT NOT NULL PRIMARY KEY,
    system    TEXT NOT NULL,
    thread_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS gc_aliases_by_thread
    ON gc_aliases (system, thread_id);

CREATE TABLE IF NOT EXISTS gc_receipts (
    system     TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    op_id      TEXT NOT NULL,
    digest     TEXT NOT NULL,
    receipt    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (system, thread_id, op_id)
);

CREATE INDEX IF NOT EXISTS gc_receipts_by_created
    ON gc_receipts (created_at);

CREATE TABLE IF NOT EXISTS gc_tombstones (
    system      TEXT    NOT NULL,
    thread_id   TEXT    NOT NULL,
    generation  INTEGER NOT NULL,
    lease_epoch INTEGER NOT NULL,
    PRIMARY KEY (system, thread_id)
);
