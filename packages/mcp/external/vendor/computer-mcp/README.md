# computer-mcp

MCP-сервер для управления графическим интерфейсом Windows через UI Automation. Даёт фоновому агенту инструменты для работы с окнами, мышью, клавиатурой и скриншотами. Транспорт — stdio.

## Подход

UIA-подход и скелет тулов взяты из [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) (CursorTouch, MIT) — этот проект обходит UI-дерево Windows через COM-библиотеку `uiautomation` и экспонирует элементы агенту по числовым `element_id`. Реализация computer-mcp переписана с нуля и расширена: scope-aware snapshot (`foreground` / `window` / `desktop`), `scope_hwnd` для resolve в поддереве активного окна, HWND-diff детекция окна после `launch_app`, UIA `ValuePattern` для чтения и программной очистки значений полей — ничего из этого в upstream нет.

## Требования

- Windows 10/11
- Python 3.10+
- [uv](https://docs.astral.sh/uv/) для управления зависимостями

## Установка

```bash
pip install -e .
# или
uv sync
```

После установки в окружении будет доступна команда `computer-mcp`:

```bash
computer-mcp --version
computer-mcp --help
```

## Подключение к background-agent

Добавьте секцию в `config.yml` background-agent'а:

```yaml
mcp-servers:
  computer:
    transport: stream_stdio
    command: computer-mcp
    description: Control Windows GUI via UI Automation (launch apps, click, type, drag, scroll, window management)
    server-name: computer
    env:
      COMPUTER_MCP_LOG_LEVEL: INFO
      COMPUTER_MCP_READONLY: "false"
      COMPUTER_MCP_ENABLE_VISION: "false"
```

После этого модель увидит тулы с префиксом `computer_`: `computer_get_state`, `computer_click`, `computer_type`, и т.д. Готовые примеры — в [examples/](examples/).

Инструкции по использованию тулов для модели живут как скилл в самом background-agent — `skills/computer-use/SKILL.md`. Модель подгружает его через `skills_load("computer-use")` когда задача требует работы с GUI. В этом репозитории инструкция намеренно не дублируется: MCP-сервер отвечает за тулы и схемы, скилл — за то как их применять.

## Инструменты

### Основные (UIA, всегда доступны)

| Тул | Назначение |
|---|---|
| `get_state` | Структурированное дерево видимых UI-элементов с номерами; scope управляется параметром (см. ниже) |
| `click` | Кликнуть по элементу из последнего снимка |
| `type` | Ввести текст в поле; `clear=True` программно очищает значение через UIA `ValuePattern.SetValue` (с откатом на Ctrl+A/Delete) |
| `scroll` | Прокрутить окно или элемент (up/down/left/right) |
| `drag` | Перетащить один элемент на другой |
| `key` | Нажать одну клавишу (`enter`, `tab`, `f5`, ...) |
| `shortcut` | Комбинация клавиш (`ctrl+s`) |
| `resize` | Изменить размер/положение окна |
| `switch` | Переключить фокус на окно по подстроке заголовка |
| `launch_app` | Запустить приложение; возвращает `window_title` / `window_hwnd` запущенного окна, чтобы следующий `get_state` мог сразу использовать `scope="window"` без лишнего desktop-обхода |
| `wait` | Пауза до 30 сек |

### Опциональные (vision, под флагом)

| Тул | Назначение |
|---|---|
| `capture_screenshot` | PNG экрана/области |
| `analyze_screen_region` | PNG конкретного прямоугольника |

Активируются через `COMPUTER_MCP_ENABLE_VISION=true`.

## Параметры `get_state`

Параметр `scope` ограничивает область UIA-обхода:

| `scope` | Поведение | Когда использовать |
|---|---|---|
| `"foreground"` (по умолчанию) | Дерево текущего focused-окна | Большинство задач — модель работает с активным приложением |
| `"window"` (`window_title="..."`) | Дерево окна по substring-совпадению имени | Есть конкретное целевое окно, не обязательно активное |
| `"desktop"` | Весь рабочий стол (старое поведение) | Нужен список окон, screenshot-like обзор |

Дополнительно:

- `max_depth: int` — отбросить элементы глубже N уровней от корня scope.
- `exclude_shell: bool = True` — для `scope="desktop"` отбросить Taskbar / Program Manager.
- `only_interactive: bool = False` — оставить только кликабельные/редактируемые контролы и их предков.

Снимок также содержит поле `value="..."` у элементов `Edit` / `ComboBox` / `Document` / `Spinner`, если контрол несёт текущее значение отдельно от своего имени — считывается через UIA `ValuePattern`. Это критично для полей с автозаполнением: модель видит `Edit "Ваше имя" value="Дмитрий"` и может не затирать значение, если пользователь не просил его менять.

**Почему `foreground` по умолчанию.** Типичный запрос «открой X и сделай Y» работает с одним окном. При `scope="desktop"` снапшот ~160 КБ (все окна + рабочий стол + IDE агента), при `scope="foreground"` — несколько КБ. Экономия токенов ×20+ в типичном прогоне.

## scope_hwnd и scope-aware resolve

`get_state(scope=...)` пишет в snapshot поле `scope_hwnd` (HWND окна-корня). Следующие `click` / `type` / `drag` используют этот HWND, чтобы искать элемент только в поддереве этого окна, а не шарить по всему рабочему столу — критично для Electron-приложений (SberJazz, Teams, VS Code), где полный DFS занимает 5+ секунд. Отдельный `launch_app` уже возвращает `window_title`, так что первый `get_state` может идти сразу с `scope="window"` без лишнего desktop-обхода.

## Переменные окружения

| Переменная | Тип | По умолчанию | Описание |
|---|---|---|---|
| `COMPUTER_MCP_READONLY` | bool | `false` | Только чтение экрана — блокирует мутирующие тулы |
| `COMPUTER_MCP_ENABLE_VISION` | bool | `false` | Включить vision-тулы |
| `COMPUTER_MCP_DISABLED_TOOLS` | csv | пусто | Список отключённых тулов (`drag,shortcut`) |
| `COMPUTER_MCP_LOG_LEVEL` | str | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `COMPUTER_MCP_LOG_DIR` | path | stderr only | Папка для JSONL-файла логов |
| `COMPUTER_MCP_SESSION_ID` | str | `"default"` | ID сессии — namespace state-файла. Задай явно для изоляции параллельных агентов на одной машине |
| `COMPUTER_MCP_STATE_TTL` | int | `3600` | TTL state-файла в секундах |

## Безопасность

- **Блэклист клавиш.** `shortcut` отказывается выполнять `ctrl+alt+delete`, `win+l`, `alt+f4`.
- **Readonly-режим.** `COMPUTER_MCP_READONLY=true` блокирует любые мутации — доступны только `get_state`, `wait` и (опционально) vision-тулы.
- **Отключение тулов по имени.** `COMPUTER_MCP_DISABLED_TOOLS=drag,shortcut` убирает их из экспонируемого набора.
- **Маскирование ввода.** Содержимое тула `type` не пишется в INFO-лог — только длина текста.
- **REST-режим не поддерживается в v0.1.** Не подключайте computer-use к background-agent в серверном режиме без дополнительных мер безопасности.

## Известные ограничения

- **Мультимодальность end-to-end.** Текущая версия background-agent превращает бинарные данные из MCP в строку `"binary data"`, поэтому vision-тулы реализованы, но картинки до модели как изображения не доходят. Тулы готовы к активации, если в background-agent будет добавлена мультимодальность.
- **Electron-приложения** (Jazz / SberJazz, Teams, Slack, Discord, VS Code) материализуют UIA-дерево только когда окно в фокусе. Корректный паттерн — `launch_app` → `wait(2)` → `switch(window_title=...)` → `get_state(scope="window", ...)`; без `switch` перед `get_state` снимок будет почти пустым. Скилл `computer-use` в background-agent явно инструктирует модель этому паттерну.
- **Параллельность.** Сервер хранит снимок последнего `get_state` в `%TEMP%\computer-mcp\last_state_<session>.json`. По умолчанию `session = "default"` — все агенты на одной машине делят один файл. Это ок для типового сценария (один dev, один работающий background-agent). Если нужно запускать параллельные агенты — задай каждому свой `COMPUTER_MCP_SESSION_ID` (любая уникальная строка).
- **Параллельные tool calls** в одном шаге модели могут конкурировать за файл-снимок.

## Разработка и тесты

```bash
pip install -e ".[dev]"
# или
uv sync --extra dev

pytest -v                    # все тесты (включая e2e с реальным UIA на Windows)
pytest -m "not windows"      # без Windows-зависимых e2e
ruff check .                 # линтер
```
