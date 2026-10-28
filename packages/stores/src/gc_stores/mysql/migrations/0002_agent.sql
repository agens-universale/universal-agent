-- Миграция 2: хранилище агента (MySQLAgentStore).
--
-- Ресурсы — по (agent_id, project_id, kind, path): project_id пуст у
-- ресурсов агента, у ресурсов проекта — id проекта. Длины ключа — в пределах
-- первичного ключа InnoDB (3072 байта utf8mb4); коллация NO PAD, как у
-- тредов. version — sha256 содержимого; source — NULL у загруженного
-- человеком, bundled@<версия gc-core> у умолчаний пакета.

CREATE TABLE IF NOT EXISTS gc_agent_resources (
    `agent_id`   VARCHAR(128) NOT NULL,
    `project_id` VARCHAR(128) NOT NULL,
    `kind`       VARCHAR(32)  NOT NULL,
    `path`       VARCHAR(400) NOT NULL,
    `data`       LONGBLOB     NOT NULL,
    `version`    CHAR(64)     NOT NULL,
    `source`     VARCHAR(64),
    `updated_at` DATETIME(6)  NOT NULL,
    PRIMARY KEY (`agent_id`, `project_id`, `kind`, `path`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- Память проекта: version — sha256 текста, NULL у пустой.
CREATE TABLE IF NOT EXISTS gc_project_memory (
    `agent_id`   VARCHAR(128) NOT NULL,
    `project_id` VARCHAR(128) NOT NULL,
    `text`       LONGTEXT     NOT NULL,
    `version`    CHAR(64),
    `updated_at` DATETIME(6)  NOT NULL,
    PRIMARY KEY (`agent_id`, `project_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
