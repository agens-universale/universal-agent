# computer — computer-mcp

Интеграция `computer-mcp` в background-agent. Даёт агенту управлять
Windows GUI через UI Automation: запускать приложения, кликать, вводить
текст, читать содержимое окон.

Исходники — в `external/computer-mcp/`. Пакет ставится `install.py` через
`pip install ./external/computer-mcp` (PEP 517 build из исходников).

## Требования

- **Windows 10 / 11.** MCP работает только на Windows — обращается к UIA API
  хост-системы через COM-библиотеку `uiautomation`. На Linux / macOS
  `install.py` пропустит этот MCP (`platforms: [win32]` в манифесте).
- **Python 3.10+.**
- **Активный venv агента** перед запуском `install.py`.

## Установка

Из корня background-agent:

```bash
python recommended_mcp/install.py
```

Для разработки (live-edit исходников в `external/computer-mcp/` без
переустановки):

```bash
python recommended_mcp/install.py --editable
```

После установки в PATH активного venv появится команда `computer-mcp`.

## Подключение к агенту

Скопируй содержимое `agent_config.snippet.yml` в секцию `mcp-servers:` твоего
`config.yml` агента.

## Запуск

Отдельного запуска не требует — агент сам спавнит `computer-mcp` по stdio
при первом вызове `computer_*`-тула. `run.py` этот MCP пропускает
(в манифесте нет блока `run:`).

## Конфигурация через env

Секция `env:` из snippet пробрасывается в процесс MCP через config.yml агента.

| Переменная | Назначение |
|---|---|
| `COMPUTER_MCP_READONLY` | `true` — блокирует мутирующие тулы (только `get_state` / `wait`). |
| `COMPUTER_MCP_ENABLE_VISION` | `true` — активирует vision-тулы (screenshot). |
| `COMPUTER_MCP_DISABLED_TOOLS` | CSV список, напр. `drag,shortcut` — убирает тулы из набора. |
| `COMPUTER_MCP_SESSION_ID` | Namespace state-файла. `"default"` если агент один; уникальный ID при параллельных агентах на одной машине. |
| `COMPUTER_MCP_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |

## Инструкции для модели

Скилл `computer-use` в `skills/computer-use/SKILL.md` агента содержит правила
вызова тулов: порядок действий, работу с `element_id`, обработку ошибок.
Модель подгружает его через `skills_load("computer-use")`.

## Обновление

Правки идут в `external/computer-mcp/`. После правок:

- В обычном режиме — пере-запустить `python recommended_mcp/install.py`
  (с бампом `version` в `external/computer-mcp/pyproject.toml`, иначе pip
  не перетрёт уже стоящую копию).
- В editable-режиме — изменения подхватываются на следующем запуске MCP
  без переустановки.
