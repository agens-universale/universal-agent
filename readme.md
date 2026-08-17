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
- Подсчёт токенов и стоимости прогона (с учётом prompt-кэша).

## Связь с разработчиками и поддержка

Баги, пожелания, обратная связь - пишите сюда:
- [В Телеграм](https://t.me/+2AwDQhHxaAdhNmUy)
- [В Сберчат](https://sberchat.sberbank.ru/@gc-ua-support)
- [В Маттермост](https://mm.sberdevices.ru/sberdevices/channels/gigacode-unuversal-agent-support)


## Быстрый старт

> 📄 **Наглядная инструкция по установке (HTML):** [docs/gc-ua-install.html](docs/gc-ua-install.html) — пошаговая страница, откройте локально в браузере.

Агент ставится профилями. Профиль — это отдельный дистрибутив, собираемый из
своего пакета `packages/<профиль>/src/gc_*`: код чужих профилей на диск не попадает.

| Команда установки | Что приезжает |
|---|---|
| `gc-ua` | полный агент: ядро + TUI + server |
| `gc-cli` | только ядро (headless) |
| `gc-tui` | ядро + TUI |
| `gc-server` | ядро + FastAPI-режим и swarm |

```bash
uv tool install "gc-ua @ git+https://gitverse.ru/gigacode-ai/universal-agent"
uv tool install "gc-cli @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/cli"
uv tool install "gc-tui @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/tui"
uv tool install "gc-server @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/server"

gc-mcp install duckduckgo_search   # один MCP + авто-подключение в ~/.gc/config.local.yml
# gc-mcp install                   # все MCP из манифеста
```

Хвост `#subdirectory=` у частичных профилей обязателен: uv читает из git только
корневой `pyproject.toml`, а в корне лежит полный профиль. Подробно — в
[docs/packaging.md](docs/packaging.md).

> Любая установка кладёт в PATH все пять команд (`gc-cli`, `gc-ua`, `gc-serve`,
> `gc-mcp`, `gc-mcp-install`). Команда без своего профиля не падает трейсбеком, а
> печатает одну строку с тем, чем её доставить. Смена профиля — переустановка,
> нужен `--force`.
> независимо от extras, и без своего профиля печатают, чем доставить недостающее.

После установки `gc-ua` / `gc-cli` работают **без `--config`**: при первом запуске конфиг создаётся сам (Windows — `~/.gc/config.yml`, macOS/Linux — `~/.config/gc/config.yml`).

- **`gc-ua`** — первый запуск открывает **мастер настройки**: адрес сервера → токен → модель → MCP-серверы. По завершении настройки сохраняются, и агент сразу стартует.
- **`gc-cli`** — печатает подсказку и выходит: впишите `backend.completions_url` и токен (`backend.chat_completion_headers.Authorization`), при необходимости `agent.model`, затем запустите снова. Пройти мастер: `gc-cli --init-config` (настроить и выйти).

Поменять адрес, токен, модель, MCP или обновить агента потом можно прямо из TUI — команда `/settings`.

> **Миграция с gc-cli-pub:** если ранее была установлена старая версия дистрибутива, сначала удалите её: `uv tool uninstall gc-cli-pub`, затем выполните установку выше.

Как это устроено внутри: [docs/packaging.md](docs/packaging.md).

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
gc-ua
```

В `gc-ua` HTTP/SSE MCP-daemon'ы для серверов из `mcp-servers` поднимаются **по умолчанию** (пересечение с манифестом `recommended_mcp`); отключить: `gc-ua --no-auto-mcp`. В `gc-cli` то же — явным ключом `-a` / `--auto-mcp`.
Ключ `-q` определяет входной запрос к агенту.
Ключ `--config` переопределяет конфиг агента на кастомный.

Конфиг по умолчанию: Windows — `~/.gc/config.yml`, macOS/Linux — `~/.config/gc/config.yml` (создаётся автоматически при первом запуске); свой путь — `--config ./config.yml`. Подробнее см. документацию ниже.

## Как работать в gc-ua (TUI)

Краткий тур после установки. Тот же блок — на экране **F1** `/help`. Подробности: [docs/tui.md](docs/tui.md).

1. **F6** `/settings` — URL сервера, токен, модель (и MCP при необходимости).
2. Опишите задачу в поле ввода → **Enter**. Сессия создастся сама.
3. **F1** — справка · **F2** — список диалогов · `/new` (**Ctrl+N**) — новый диалог · **Ctrl+R** — переименовать активную сессию (пустое имя → авто-имя из запросов пользователя).
4. **F3** `/diff` — посмотреть правки агента, принять или откатить.
5. `/view путь/к/файлу` — постраничный просмотр файла.
6. `! git status` — локальная команда shell (не агент); **Esc** — отмена.
7. `/` или `.` — палитра команд; `@` — приложить файл к сообщению.
8. **Ctrl+клик** по tool-вызову в трейсе — просмотр аргументов и полного результата.
9. **Копировать:** выделить мышью (сразу в буфер). **Вставить:** **Ctrl+V**.
   На Linux наружу нужны `wl-clipboard` / `xclip` / `xsel` (иначе: Ctrl+V в поле → Shift+мышь → Ctrl+Shift+C в окне терминала).
10. **Ctrl+C** — стоп агента (если ничего не выделено). **Esc** — назад / выход.
11. Перенос строки в поле — **Ctrl+J**. Агент спросил (`[asking]`) — ответ в том же поле + Enter. Опасная операция — баннер **Y** / **S** (на сессию) / **N**.

## Документация

- [Установка и запуск](docs/usage.md) — установка (uv/pip), запуск, ключи CLI.
- [Упаковка и профили](docs/packaging.md) — как пять дистрибутивов собираются из одного репозитория, почему у частичных профилей `#subdirectory=`, как обновление сохраняет профиль.
- [Тесты](docs/testing.md) — наборы при пакетах (`packages/*/tests`), общий тест-кит, `external/*`, полный прогон.
- [Конфигурация](docs/configuration.md) — `config.yml`, приоритет и переопределение, security policy, MCP-серверы, runtime-пути, project-local config.
- [Сабагенты](docs/subagents.md) — делегирование задач, кастомные сабагенты, лимиты и бюджет вызовов.
- [Скиллы и пакеты скиллов](docs/skills.md) — трёхслойный поиск, манифест пакета, canonical IDs, встроенные паки.
- [Серверный режим](docs/server.md) — REST-обёртка (FastAPI), конфиги сервера, эндпоинты, Swarm-режим (Linux).
- [Swarm-режим](docs/swarm.md) — изолированные bubblewrap-песочницы для каждой задачи, pipeline запроса, эндпоинты, AR-1..AR-9, troubleshooting.
- [TUI режим (gc-ua)](docs/tui.md) — кроссплатформенный TUI поверх агента: установка, запуск, основные команды и экраны.
- [RootBox режим](docs/rootbox.md) — Roox box режим, конфиги (command_moderation, read_guard, root_box (write_guard), url_policy)
- [Разбиение на модули](docs/module-split.md) — зачем ядро стало SDK `gc_core`, границы пакетов, цена и итоги переезда.
- [Токены и стоимость](docs/usage_pricing.md) — подсчёт токенов по категориям, секция `pricing`, учёт prompt-кэша, хранение в meta треда.
