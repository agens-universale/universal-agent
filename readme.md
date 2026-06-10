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
uv tool install -e .
gc-mcp-install --config recommended_mcp/config.local.yml   # MCP → ~/.local/share/gc/recommended_mcp/venv
```

Подробнее про MCP: [`recommended_mcp/README.md`](recommended_mcp/README.md).

```bash
gc-cli -a --config demo_config_openai.yml -q "создай подпапку test_project. В ней напиши на Python модуль
вычисления чисел Фибоначчи. В отдельном файле напиши тесты. Прогони тесты,
посчитай первые 1000 чисел Фибоначчи и сохрани результат в JSON там же.
Пиши быстрый код с кешированием. Скажи, чему равно 1000-е число Фибоначчи,
умноженное на 3."
```

Ключ `-a` запускает все рекомендванные mcp в проекте.
Ключ `-q` определяет входной запрос к агенту.
Клюс `--config` переопределяет конфиг агента на кастомный.

Конфиг по умолчанию — `config.yml` рядом с установленным пакетом; свой путь — `--config ./config.yml`. Подробнее см. документацию ниже.

## Документация

- [Установка и запуск](docs/usage.md) — установка (uv/pip), запуск, ключи CLI.
- [Тесты](docs/testing.md) — как запускать `tests/`, `external/*` и полный прогон.
- [Конфигурация](docs/configuration.md) — `config.yml`, приоритет и переопределение, security policy, MCP-серверы, runtime-пути, project-local config.
- [Скиллы и пакеты скиллов](docs/skills.md) — трёхслойный поиск, манифест пакета, canonical IDs, встроенные паки.
- [Серверный режим](docs/server.md) — REST-обёртка (FastAPI), конфиги сервера, эндпоинты, Swarm-режим (Linux).
- [RootBox режим](docs/rootbox.md) — Roox box режим, конфиги (command_moderation, read_guard, root_box (write_guard), url_policy)
