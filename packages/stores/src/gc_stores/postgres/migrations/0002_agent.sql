-- Миграция 2: хранилище агента (PostgresAgentStore).
--
-- Ресурсы — по (agent_id, project_id, kind, path): project_id пуст у
-- ресурсов агента, у ресурсов проекта (L2, L3, скиллы project) — id
-- проекта. Содержимое — байты (assets скиллов бывают бинарными); version —
-- sha256 содержимого; source — NULL у загруженного человеком,
-- bundled@<версия gc-core> у умолчаний пакета (gc-stores sync).

CREATE TABLE IF NOT EXISTS gc_agent_resources (
    agent_id   text        NOT NULL,
    project_id text        NOT NULL,
    kind       text        NOT NULL,
    path       text        NOT NULL,
    data       bytea       NOT NULL,
    version    text        NOT NULL,
    source     text,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (agent_id, project_id, kind, path)
);

-- Память проекта: version — sha256 текста, NULL у пустой.
CREATE TABLE IF NOT EXISTS gc_project_memory (
    agent_id   text        NOT NULL,
    project_id text        NOT NULL,
    text       text        NOT NULL,
    version    text,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (agent_id, project_id)
);
