# GigaCode CLI (gc-cli)

Background и CLI-клиент для работы с агентом GigaCode: отправка запросов, вызов инструментов и MCP-серверов. Требуется запущенный бэкенд агента с API `v1/responses` или режимом `chat_completions` с доступом к модели (chat/completions — openrouter, vllm и др.).

**Требования:** Python 3.10 или новее.

## Возможности

- CLI-агент: запросы, инструменты, треды, скиллы.
- Два режима бэкенда: `v1/responses` и `chat_completions` (+ суммаризация в chat_completions).
- MCP: 6 транспортов, готовые рецепты в `recommended_mcp/`.
- Скиллы и пакеты скиллов (встроенные паки `supercoder` / `codebase` / `articles`).
- Серверный режим: REST-обёртка на FastAPI.
- Swarm-режим (только Linux): изолированные песочницы (bubblewrap).

## Быстрый старт

```bash
uv sync
uv tool install -e .
```

```bash
gc-cli -q "создай подпапку test_project. В ней напиши на Python модуль
вычисления чисел Фибоначчи. В отдельном файле напиши тесты. Прогони тесты,
посчитай первые 1000 чисел Фибоначчи и сохрани результат в JSON там же.
Пиши быстрый код с кешированием. Скажи, чему равно 1000-е число Фибоначчи,
умноженное на 3."
```

Конфиг по умолчанию — `config.yml` рядом с установленным пакетом; свой путь — `--config ./config.yml`. Подробнее см. документацию ниже.

## Документация

- [Установка и запуск](docs/usage.md) — установка (uv/pip), запуск, ключи CLI.
- [Конфигурация](docs/configuration.md) — `config.yml`, приоритет и переопределение, security policy, MCP-серверы, runtime-пути, project-local config.
- [Скиллы и пакеты скиллов](docs/skills.md) — трёхслойный поиск, манифест пакета, canonical IDs, встроенные паки.
- [Серверный режим](docs/server.md) — REST-обёртка (FastAPI), конфиги сервера, эндпоинты, Swarm-режим (Linux).
- [Swarm-режим](docs/swarm.md) — изолированные bubblewrap-песочницы для каждой задачи, pipeline запроса, эндпоинты, AR-1..AR-9, troubleshooting.
- [RootBox режим](docs/rootbox.md) — Roox box режим, конфиги (command_moderation, read_guard, root_box (write_guard), url_policy)

## Swarm-режим (только Linux)

Swarm — серверный режим, в котором REST-обёртка форкает CLI в изолированную
bubblewrap-песочницу для каждого `POST /swarm/run`. Подробности в
[`docs/server.md`](docs/server.md) (раздел «Swarm-режим»).

### Краткая установка на bare Linux

```bash
# Системные зависимости
sudo apt-get install -y bubblewrap coreutils  # Debian/Ubuntu
# или: sudo dnf install -y bubblewrap coreutils  # Fedora

# Установить gc-cli в отдельный venv
python3 -m venv /opt/gc-cli/.venv
source /opt/gc-cli/.venv/bin/activate
pip install -e .
pip install psutil  # опционально, для memory/cpu enforcement
deactivate

# Настроить config_server.yml
swarm:
  enabled: true
  cli_venv_root: "/opt/gc-cli/.venv"
  agent_dep_dir: "/opt/gc-cli/mcp-deps"  # если используешь npx-MCP
  max_concurrent: 10
  ttl_seconds: 86400
  limits:
    timeout_seconds: 1800
    memory_mb: 1024
    disk_mb: 2048

# Запуск
gc-cli serve --config /etc/gc-cli/config_server.yml
```

### Эндпоинты

- `POST /swarm/run` — запуск sworm-задачи (idempotent по `task_id`).
- `GET /swarm/status/{system}/{task_id}` — текущий статус.
- `GET /swarm/logs/{system}/{task_id}` — meta, messages, stdout/stderr.

### Что enforced

- **Таймаут** — GNU timeout в wrapper.sh + Python-side через deadline_at.
- **Disk quota** — `du -sb` на sandbox-каталог раз в minute.
- **Memory/CPU** — psutil (если установлен).
- **Idempotency** — повторный POST с одним `task_id` → 409.
- **TTL janitor** — `scripts/cleanup_swarm_threads.py` чистит старые
  каталоги, запускается автоматически при `swarm.ttl_seconds > 0`.

### Известные ограничения MVP

- Только Linux (нужен bubblewrap + GNU coreutils).
- Docker/k8s deployment — отдельный этап (см. `~/swarm-lab/specs/swarm1_plan.md` шаг 3.8).
- Docker-MCP через stdio (`docker exec`) не поддерживается — используй
  TCP/HTTP MCP.
- `--share-net` делит network namespace с хостом (нужно для LLM API).
