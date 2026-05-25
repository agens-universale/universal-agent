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
- [Серверный режим](docs/rootbox.md) — Roox box режим, конфиги (command_moderation, read_guard, root_box (write_guard))
