-- Миграция 2: хранилище агента (SQLiteAgentStore).
--
-- Ресурсы — по (agent_id, project_id, kind, path): project_id пуст у
-- ресурсов агента, у ресурсов проекта (L2, L3, скиллы project) — id
-- проекта. Содержимое — байты (assets скиллов бывают бинарными); version —
-- sha256 содержимого; source — NULL у загруженного человеком,
-- bundled@<версия gc-core> у умолчаний пакета (gc-stores sync).

CREATE TABLE IF NOT EXISTS gc_agent_resources (
    agent_id   TEXT NOT NULL,
    project_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    data       BLOB NOT NULL,
    version    TEXT NOT NULL,
    source     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, project_id, kind, path)
);

-- Память проекта: version — sha256 текста, NULL у пустой.
CREATE TABLE IF NOT EXISTS gc_project_memory (
    agent_id   TEXT NOT NULL,
    project_id TEXT NOT NULL,
    text       TEXT NOT NULL,
    version    TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, project_id)
);
