-- Схема MySQLThreadStore (контракт ThreadStore v2, gc_core/threads/store.py).
--
-- Миграция 1: таблицы хранилища тредов, раскладка — как у Postgres.
-- Документы — текст JSON ровно как записан (LONGTEXT, не тип JSON: тот не
-- хранит \u0000, переставляет ключи и отвергает NaN). Ключи — VARCHAR(255)
-- с коллацией utf8mb4_0900_bin: сравнение и порядок — по кодам символов,
-- как у остальных хранилищ, и NO PAD — «a» и «a » разные ключи
-- (utf8mb4_bin с PAD SPACE счёл бы их одним: чужой алиас или op_id с
-- хвостовым пробелом выдавал бы себя за занятый). Время — DATETIME(6) в UTC.

CREATE TABLE IF NOT EXISTS gc_threads (
    `system`           VARCHAR(255)  NOT NULL,
    `thread_id`        VARCHAR(255)  NOT NULL,
    `workspace`        VARCHAR(1024),
    `info`             LONGTEXT      NOT NULL,
    `state`            LONGTEXT      NOT NULL,
    `usage`            LONGTEXT      NOT NULL,
    `runtime`          LONGTEXT      NOT NULL,
    `frontend`         LONGTEXT      NOT NULL,
    `archives`         LONGTEXT      NOT NULL,
    `extra`            LONGTEXT      NOT NULL,
    `max_seq`          LONGTEXT      NOT NULL,
    `messages_count`   INT           NOT NULL,
    `revision`         BIGINT        NOT NULL,
    `generation`       BIGINT        NOT NULL,
    `history_version`  BIGINT        NOT NULL,
    `lease_owner`      VARCHAR(512),
    `lease_epoch`      BIGINT        NOT NULL,
    `lease_expires_at` DATETIME(6),
    `created_at`       DATETIME(6)   NOT NULL,
    `updated_at`       DATETIME(6)   NOT NULL,
    PRIMARY KEY (`system`, `thread_id`),
    KEY gc_threads_by_updated (`updated_at` DESC, `system`, `thread_id`),
    KEY gc_threads_by_system_updated (`system`, `updated_at` DESC),
    KEY gc_threads_by_workspace_updated (`workspace`(255), `updated_at` DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS gc_stream_entries (
    `system`    VARCHAR(255) NOT NULL,
    `thread_id` VARCHAR(255) NOT NULL,
    `stream`    VARCHAR(64)  NOT NULL,
    `seq`       BIGINT       NOT NULL,
    `ts`        DATETIME(6)  NOT NULL,
    -- sha256 от data["id"], если это строка: по нему discard_message_ids.
    `msg_key`   CHAR(64),
    `data`      LONGTEXT     NOT NULL,
    PRIMARY KEY (`system`, `thread_id`, `stream`, `seq`),
    KEY gc_stream_entries_by_msg_key (`system`, `thread_id`, `msg_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- История, ушедшая в архив при replace_history.
CREATE TABLE IF NOT EXISTS gc_archives (
    `system`     VARCHAR(255) NOT NULL,
    `thread_id`  VARCHAR(255) NOT NULL,
    `archive_id` VARCHAR(64)  NOT NULL,
    `seq`        BIGINT       NOT NULL,
    `ts`         DATETIME(6)  NOT NULL,
    `data`       LONGTEXT     NOT NULL,
    PRIMARY KEY (`system`, `thread_id`, `archive_id`, `seq`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS gc_blobs (
    `system`     VARCHAR(255) NOT NULL,
    `thread_id`  VARCHAR(255) NOT NULL,
    `name`       VARCHAR(255) NOT NULL,
    `mime`       VARCHAR(255) NOT NULL,
    `data`       LONGBLOB     NOT NULL,
    `created_at` DATETIME(6)  NOT NULL,
    PRIMARY KEY (`system`, `thread_id`, `name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- Алиас глобален: один на всё хранилище.
CREATE TABLE IF NOT EXISTS gc_aliases (
    `alias`     VARCHAR(255) NOT NULL PRIMARY KEY,
    `system`    VARCHAR(255) NOT NULL,
    `thread_id` VARCHAR(255) NOT NULL,
    KEY gc_aliases_by_thread (`system`, `thread_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS gc_receipts (
    `system`     VARCHAR(255) NOT NULL,
    `thread_id`  VARCHAR(255) NOT NULL,
    `op_id`      VARCHAR(255) NOT NULL,
    `digest`     CHAR(64)     NOT NULL,
    `receipt`    LONGTEXT     NOT NULL,
    `created_at` DATETIME(6)  NOT NULL,
    PRIMARY KEY (`system`, `thread_id`, `op_id`),
    KEY gc_receipts_by_created (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- Удалённые треды: тред, созданный заново под тем же ключом, продолжает
-- поколение и эпоху аренды.
CREATE TABLE IF NOT EXISTS gc_tombstones (
    `system`      VARCHAR(255) NOT NULL,
    `thread_id`   VARCHAR(255) NOT NULL,
    `generation`  BIGINT       NOT NULL,
    `lease_epoch` BIGINT       NOT NULL,
    PRIMARY KEY (`system`, `thread_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
