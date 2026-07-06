# GigaCode Universal Agent

Background и CLI-клиент для работы с агентом GigaCode: отправка запросов, вызов инструментов и MCP-серверов. Требуется запущенный бэкенд агента 
- с API `v1/responses` (deprecated) 
- или режимом `chat_completions` (активно поддерживается) с доступом к модели (chat/completions — openrouter, vllm и др.).

**Требования:** Python 3.10 или новее.

## Возможности

- CLI-агент: запросы, инструменты, треды, скиллы.
- Два режима бэкенда: `chat_completions` и `v1/responses` (deprecated).
- MCP: 6 транспортов, готовые рецепты в `recommended_mcp/`.
- Скиллы и пакеты скиллов (встроенные паки `supercoder` / `codebase` / `articles`).
- Слои инструкций `AGENTS.md` (L0 shipped → L1 `~/.gc/AGENTS.md` → L2 проект → L3 `.gc/AGENTS.md`) — задать агенту проектный/глобальный контекст; `--init` генерирует `AGENTS.md` по структуре репо.
- Защита файловой системы: `write_guard` ограничивает запись в `launch_root`, `security.protected_paths` включена по умолчанию.
- Серверный режим: REST-обёртка на FastAPI.
- Swarm-режим (только Linux): изолированные песочницы (bubblewrap).

## Связь с разработчиками и поддержка

Баги, пожелания, обратная связь - пишите сюда:
- [В Телеграм](https://t.me/+2AwDQhHxaAdhNmUy)
- [В Сберчат](https://sberchat.sberbank.ru/@gc-ua-support)


## Быстрый старт

> 📄 **Наглядная инструкция по установке (HTML):** [docs/gc-ua-install.html](docs/gc-ua-install.html) — пошаговая страница, откройте локально в браузере.

```bash
uv tool install -e .   # gc-cli + gc-ua (TUI по умолчанию). При реинсталле добавьте --force
gc-mcp install duckduckgo_search   # поставить один MCP + авто-подключить в ~/.gc/config.local.yml
# gc-mcp install   # поставить все MCP из манифеста
```

```bash
# установка без клонирования репо gc-cli + gc-ua (TUI по умолчанию)
uv tool install git+ssh://git@gitverse.ru/gigacode-ai/background-agent
gc-mcp install   # MCP → ~/.local/share/gc/recommended_mcp/venv + авто-wire в ~/.gc/config.local.yml
# Примечание: source-MCP (office/science/computer) устанавливаются напрямую из git-подкаталога репо
# (pip install "git+...#subdirectory=external/<name>") — клонировать репо не нужно.
# Требуется: git на PATH + доступ к сети.
```

После установки `gc-ua` / `gc-cli` работают **без `--config`**: при первом запуске конфиг создаётся (Windows — `~/.gc/config.yml`, macOS/Linux — `~/.config/gc/config.yml`), и агент **останавливается с подсказкой** — впишите `backend.completions_url` и токен (`backend.chat_completion_headers.Authorization`), при необходимости `agent.model`, затем запустите снова. Подготовить конфиг заранее, не запуская агента: `gc-cli --init-config` (или `gc-ua --init-config`).

> **Миграция с gc-cli-pub:** если ранее была установлена старая версия дистрибутива, сначала удалите её: `uv tool uninstall gc-cli-pub`, затем выполните установку выше.

Headless-установка только `gc-cli` (без TUI): см. [docs/usage.md](docs/usage.md#минимальная-установка-gc-cli-без-tui).

Подробнее про MCP: [`recommended_mcp/README.md`](recommended_mcp/README.md).

Запуск без графического режима
```bash
gc-cli -a --config demo_config_openai.yml -q "создай подпапку test_project. В ней напиши на Python модуль
вычисления чисел Фибоначчи. В отдельном файле напиши тесты. Прогони тесты,
посчитай первые 1000 чисел Фибоначчи и сохрани результат в JSON там же.
Пиши быстрый код с кешированием. Скажи, чему равно 1000-е число Фибоначчи,
умноженное на 3."
```

Запуск в текстовом графическом интерфейсе
```bash
gc-ua -a
```

Ключ `-a` запускает все рекомендванные mcp в проекте.
Ключ `-q` определяет входной запрос к агенту.
Клюс `--config` переопределяет конфиг агента на кастомный.

Конфиг по умолчанию: Windows — `~/.gc/config.yml`, macOS/Linux — `~/.config/gc/config.yml` (создаётся автоматически при первом запуске); свой путь — `--config ./config.yml`. Подробнее см. документацию ниже.

## Документация

- [Установка и запуск](docs/usage.md) — установка (uv/pip), запуск, ключи CLI.
- [Тесты](docs/testing.md) — как запускать `tests/`, `external/*` и полный прогон.
- [Конфигурация](docs/configuration.md) — `config.yml`, приоритет и переопределение, security policy, MCP-серверы, runtime-пути, project-local config.
- [Скиллы и пакеты скиллов](docs/skills.md) — трёхслойный поиск, манифест пакета, canonical IDs, встроенные паки.
- [Серверный режим](docs/server.md) — REST-обёртка (FastAPI), конфиги сервера, эндпоинты, Swarm-режим (Linux).
- [Swarm-режим](docs/swarm.md) — изолированные bubblewrap-песочницы для каждой задачи, pipeline запроса, эндпоинты, AR-1..AR-9, troubleshooting.
- [TUI режим (gc-ua)](docs/tui.md) — кроссплатформенный TUI поверх агента: установка, запуск, основные команды и экраны.
- [RootBox режим](docs/rootbox.md) — конфиги защиты: `command_moderation`, `read_guard`, `write_guard` (бывший `root_box`), `url_policy`
