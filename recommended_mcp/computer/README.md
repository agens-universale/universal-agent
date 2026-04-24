# computer — gc-computer-mcp

Интеграция `gc-computer-mcp` в background-agent. Даёт агенту управлять
Windows GUI через UI Automation: запускать приложения, кликать, вводить
текст, читать содержимое окон.

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

Или вручную:

```bash
pip install recommended_mcp/computer/dist/gc_computer_mcp-*.whl
```

После установки в PATH активного venv появится команда `gc-computer-mcp`.

## Подключение к агенту

Скопируй содержимое `agent_config.snippet.yml` в секцию `mcp-servers:` твоего
`config.yml` агента.

## Запуск

Отдельного запуска не требует — агент сам спавнит `gc-computer-mcp` по stdio
при первом вызове `computer_*`-тула. `run.py` этот MCP пропускает
(в манифесте нет блока `run:`).

## Конфигурация через env

Секция `env:` из snippet пробрасывается в процесс MCP через config.yml агента.

| Переменная | Назначение |
|---|---|
| `GC_COMPUTER_MCP_READONLY` | `true` — блокирует мутирующие тулы (только `get_state` / `wait`). |
| `GC_COMPUTER_MCP_ENABLE_VISION` | `true` — активирует vision-тулы (screenshot). |
| `GC_COMPUTER_MCP_DISABLED_TOOLS` | CSV список, напр. `drag,shortcut` — убирает тулы из набора. |
| `GC_COMPUTER_MCP_SESSION_ID` | Namespace state-файла. `"default"` если агент один; уникальный ID при параллельных агентах на одной машине. |
| `GC_COMPUTER_MCP_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |

## Инструкции для модели

Скилл `computer-use` в `skills/computer-use/SKILL.md` агента содержит правила
вызова тулов: порядок действий, работу с `element_id`, обработку ошибок.
Модель подгружает его через `skills_load("computer-use")`.

## Обновление версии

1. В репо `gc-computer-mcp`: `uv build` → свежий `.whl` в `dist/`.
2. Удали старый wheel из `recommended_mcp/computer/dist/`, положи новый.
3. Коммит: `recommended_mcp/computer: bump gc-computer-mcp to X.Y.Z`.
