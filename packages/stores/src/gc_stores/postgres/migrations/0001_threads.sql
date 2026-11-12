-- Схема PostgresThreadStore (контракт ThreadStore v2, gc_core/threads/store.py).
--
-- Миграция 1: таблицы хранилища тредов. Выполняет ensure_schema() под SET LOCAL search_path,
-- поэтому имена здесь без схемы. Повторный запуск ничего не меняет.
--
-- Документы — текст JSON ровно как записан (тип text): jsonb не хранит
-- \u0000 (вывод тулов бывает с NUL) и переставляет ключи, а json и jsonb
-- отвергают NaN/Infinity, которые принимают остальные хранилища. История
-- читается той, что записана. JSON-операторов в запросах нет: всё, по чему
-- ищут, лежит отдельными колонками (workspace, msg_key).

CREATE TABLE IF NOT EXISTS gc_threads (
    system           text        NOT NULL,
    thread_id        text        NOT NULL,
    -- info.workspace отдельно: фильтр списка тредов.
    workspace        text,
    info             text        NOT NULL,
    state            text        NOT NULL,
    usage            text        NOT NULL,
    runtime          text        NOT NULL,
    frontend         text        NOT NULL,
    archives         text        NOT NULL,
    extra            text        NOT NULL,
    -- Последний выданный seq каждого потока: удалённый хвост номер не
    -- освобождает.
    max_seq          text        NOT NULL,
    messages_count   integer     NOT NULL,
    revision         bigint      NOT NULL,
    generation       bigint      NOT NULL,
    history_version  bigint      NOT NULL,
    -- Аренда: lease_owner NULL — аренды нет; lease_epoch — последняя
    -- выданная эпоха, переживает release.
    lease_owner      text,
    lease_epoch      bigint      NOT NULL,
    lease_expires_at timestamptz,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    PRIMARY KEY (system, thread_id)
);

CREATE INDEX IF NOT EXISTS gc_threads_by_updated
    ON gc_threads (updated_at DESC, system COLLATE "C", thread_id COLLATE "C");

-- Списки фронтендов фильтруют по своей системе и по workspace.
CREATE INDEX IF NOT EXISTS gc_threads_by_system_updated
    ON gc_threads (system, updated_at DESC, thread_id COLLATE "C");

CREATE INDEX IF NOT EXISTS gc_threads_by_workspace_updated
    ON gc_threads (workspace, updated_at DESC)
    WHERE workspace IS NOT NULL;

CREATE TABLE IF NOT EXISTS gc_stream_entries (
    system    text        NOT NULL,
    thread_id text        NOT NULL,
    stream    text        NOT NULL,
    seq       bigint      NOT NULL,
    ts        timestamptz NOT NULL,
    -- sha256 от data["id"], если это строка: по нему discard_message_ids
    -- (хэш, а не сам id: id приходит извне — NUL, длина больше строки
    -- индекса).
    msg_key   text,
    data      text        NOT NULL,
    PRIMARY KEY (system, thread_id, stream, seq)
);

CREATE INDEX IF NOT EXISTS gc_stream_entries_by_msg_key
    ON gc_stream_entries (system, thread_id, msg_key)
    WHERE msg_key IS NOT NULL;

-- История, ушедшая в архив при replace_history.
CREATE TABLE IF NOT EXISTS gc_archives (
    system     text        NOT NULL,
    thread_id  text        NOT NULL,
    archive_id text        NOT NULL,
    seq        bigint      NOT NULL,
    ts         timestamptz NOT NULL,
    data       text        NOT NULL,
    PRIMARY KEY (system, thread_id, archive_id, seq)
);

CREATE TABLE IF NOT EXISTS gc_blobs (
    system     text        NOT NULL,
    thread_id  text        NOT NULL,
    name       text        NOT NULL,
    mime       text        NOT NULL,
    data       bytea       NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (system, thread_id, name)
);

-- Алиас глобален: один на всё хранилище.
CREATE TABLE IF NOT EXISTS gc_aliases (
    alias     text NOT NULL PRIMARY KEY,
    system    text NOT NULL,
    thread_id text NOT NULL
);

CREATE INDEX IF NOT EXISTS gc_aliases_by_thread
    ON gc_aliases (system, thread_id);

CREATE TABLE IF NOT EXISTS gc_receipts (
    system     text        NOT NULL,
    thread_id  text        NOT NULL,
    op_id      text        NOT NULL,
    digest     text        NOT NULL,
    receipt    text        NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (system, thread_id, op_id)
);

CREATE INDEX IF NOT EXISTS gc_receipts_by_created
    ON gc_receipts (created_at);

-- Удалённые треды: тред, созданный заново под тем же ключом, продолжает
-- поколение и эпоху аренды — иначе запись по аренде удалённого треда прошла
-- бы фенсинг.
CREATE TABLE IF NOT EXISTS gc_tombstones (
    system      text   NOT NULL,
    thread_id   text   NOT NULL,
    generation  bigint NOT NULL,
    lease_epoch bigint NOT NULL,
    PRIMARY KEY (system, thread_id)
);
