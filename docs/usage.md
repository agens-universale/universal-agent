[Назад к README](../readme.md)

# Установка и запуск

После установки доступны две команды: **`gc-ua`** — основной режим с текстовым интерфейсом (TUI), **`gc-cli`** — разовые запросы одной строкой. Требуется Python ≥ 3.11 (`uv` подберёт сам).

> **Простой путь без клонирования** (в т.ч. для не-разработчика): пошаговая страница [docs/gc-ua-install.html](gc-ua-install.html) — установка одной командой, корпоративный флаг `--system-certs`, заполнение конфига. Ниже — установка для разработки и справка по ключам.

## Установка в режиме разработки (из клона)

### uv (рекомендуется)

В корне проекта выполните:

```bash
uv sync                              # окружение разработки (dev + test)
uv tool install -e packages/cli     # только ядро
uv tool install -e .                 # полный агент
```

> В корпоративной сети Сбера к командам `uv tool install` добавляйте `--system-certs` — иначе TLS-перехват ломает установку.

### Профили установки

Профиль — отдельный дистрибутив со своим пакетом (`packages/<профиль>/src/gc_*`). Код чужих
профилей физически не попадает на диск.

| Команда | Ядро | TUI | server |
|---|---|---|---|
| `gc-ua` | да | да | да |
| `gc-cli` | да | — | — |
| `gc-tui` | да | да | — |
| `gc-server` | да | — | да |

```bash
uv tool install "gc-ua @ git+https://gitverse.ru/gigacode-ai/universal-agent"
uv tool install "gc-cli @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/cli" --with-executables-from gc-mcp
uv tool install "gc-tui @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/tui" --with-executables-from gc-mcp
uv tool install "gc-server @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/server" --with-executables-from gc-mcp
```

Хвост `#subdirectory=` обязателен у всех, кроме `gc-ua`: uv читает из git только
корневой `pyproject.toml`, а корень — это полный профиль. Устройство раскладки
описано в [docs/packaging.md](packaging.md).

Полный профиль и любой листовой дистрибутив рядом не ставятся: `gc-ua`
объявляет объединение команд листьев, и `uv` отвечает `error: Executables already
exist`. Мета уже содержит всё, что даёт лист, так что ставить оба обычно незачем.
Если всё-таки нужно — `uv tool install --force`; после этого на `gc-mcp`
претендуют два инструмента, и `uv tool uninstall gc-mcp` унесёт команду у полного
профиля, а не только у отдельного менеджера.

Смена профиля — переустановка, нужен `--force`. `gc-ua --update` сохраняет
текущий профиль: агент определяет его по метаданным установки и переустанавливает
тем же дистрибутивом.

Каждый дистрибутив кладёт в PATH только свою команду: `gc-cli` → `gc-cli`,
`gc-tui` → `gc-ua`, `gc-server` → `gc-serve`, полный `gc-ua` — все пять.
Менеджер MCP — отдельный дистрибутив. `uv tool` не экспортирует команды
зависимостей, поэтому у узкого профиля стоит штатный флаг:

```bash
uv tool install "gc-cli @ ...#subdirectory=packages/cli" --with-executables-from gc-mcp
```

Флаг резолвит `gc-mcp` из того же коммита (через `[tool.uv.sources]`), пишется в
`uv-receipt.toml` и переживает `uv tool upgrade` — набирать его нужно один раз.

Если профилей на машине несколько, флаг не годится: второй профиль столкнётся с
первым на именах `gc-mcp*`. Тогда ставьте менеджер отдельным инструментом:

```bash
uv tool install "gc-mcp @ ...#subdirectory=packages/mcp"
```

Подробности в [упаковке](packaging.md#команды-каждый-дистрибутив-объявляет-только-свои).

Проверка после установки: `uv sync` и `uv run pytest` — см. [Тесты](testing.md).

### Через pip — не работает

`pip install -e .` в чистом окружении падает:

```
ERROR: Could not find a version that satisfies the requirement gc-cli==0.3.0
```

Причина не в опечатке и не чинится флагами. Корневой манифест — мета-пакет
`gc-ua`, который точно пинит четыре листа (`gc-core==0.3.0` и остальные), а
резолвятся они через `[tool.uv.sources] workspace = true`. Этой секции pip не
понимает: он идёт искать листья на PyPI, где их нет и не будет — пакеты не
публикуются, установка только из git.

Ставьте через `uv` (см. выше). Для конкретного интерпретатора:

```bash
uv tool install --python 3.12 -e .
```

Если при установке возникает ошибка, связанная с `distutils`, обновите pip:

```bash
python3.12 -m ensurepip --upgrade
```

## Конфиг при первом запуске

При первом запуске `gc-ua`/`gc-cli` без `--config` создаётся пользовательский `config.yml` (Windows — `~/.gc/config.yml`, macOS/Linux — `~/.config/gc/config.yml`). Дальше поверхности расходятся: `gc-ua` открывает **мастер настройки** (адрес, токен, модель, необязательный шаг лёгкой и максимальной моделей, MCP-серверы, голосовой ввод) и по завершении сразу стартует; `gc-cli` **останавливается с подсказкой**: впишите `backend.completions_url`, токен (`backend.chat_completion_headers.Authorization`), при необходимости `agent.model`, затем запустите снова — или пройдите тот же мастер командой `gc-cli --init-config`. Подробнее — [Конфигурация](configuration.md).

## MCP-серверы

Устанавливаются и **подключаются** одной командой (авто-wiring в `~/.gc/config.local.yml`, конфиг пользователя не трогается):

```bash
gc-mcp install                     # все рекомендованные
gc-mcp install duckduckgo_search   # один по имени
gc-mcp list                        # доступные / установленные
gc-mcp remove duckduckgo_search    # удалить по имени
gc-mcp remove --all                # снести все подключённые
```

`gc-mcp remove` с флагом `--keep-package` только отключает сервер (un-wire), оставляя пакет в venv для быстрой повторной активации. В `gc-ua` HTTP/SSE MCP-daemon'ы для серверов из конфига поднимаются по умолчанию; в `gc-cli` — флаг `-a` / `--auto-mcp`. Отключить в TUI: `gc-ua --no-auto-mcp`. Подробнее — [`recommended_mcp/README.md`](../packages/mcp/src/recommended_mcp/README.md).

## Запуск

```bash
gc-ua                 # TUI (HTTP/SSE MCP-daemon'ы из конфига — по умолчанию)
gc-ua --no-auto-mcp   # TUI без автозапуска daemon'ов
gc-cli -a -q "запрос" # CLI с автозапуском HTTP/SSE MCP-daemon'ов
gc-cli -q "запрос"    # разовый запрос без интерфейса
```

Пример разового запроса:

```commandline
gc-cli -q "создай подпапку test_project. В ней напиши на Python модуль
вычисления чисел Фибоначчи. В отдельном файле напиши тесты. Прогони тесты,
посчитай первые 1000 чисел Фибоначчи и сохрани результат в JSON там же.
Пиши быстрый код с кешированием. Скажи, чему равно 1000-е число Фибоначчи,
умноженное на 3."
```

## VS Code (ACP)

Редактор поднимает агента отдельным процессом `gc-acp` и говорит с ним по
Agent Client Protocol. В VS Code своего клиента нет: поставьте расширение
[ACP Client](https://marketplace.visualstudio.com/items?itemName=formulahendry.acp-client)
(`formulahendry.acp-client`) — Extensions, поиск `ACP Client`, либо
`code --install-extension formulahendry.acp-client`.

Команда приезжает вместе с полным агентом (`uv tool install -e .` или
установка `gc-ua` из раздела выше) и лежит в `PATH` рядом с `gc-ua`. Путь
для настройки: `which gc-acp` (обычно `~/.local/bin/gc-acp`). В клоне для
разработки после `uv sync` это `<корень>/.venv/bin/gc-acp`. Узкий профиль
`gc-cli` эту команду наружу не кладёт: `uv tool` экспортирует только скрипты
запрошенного дистрибутива. Тогда отдельно
`uv tool install -e packages/acp`.

В `settings.json` аргументы оставьте пустыми. Конфиг ищется так же, как у
`gc-cli`, без `--config`. Канал ACP по умолчанию: проза без tool-call разрешена
(`acp.text_answer_mode: allow`), finish-gate выключен (`off`), todo-reminder выключен,
плюс hint «discussion ≠ implementation». TUI-шный `tui.text_answer_mode: judge`
на этот канал не действует. Явный профиль-оверлей —
`packages/acp/profiles/acp.yml` (скопируйте в `~/.config/gc/profiles/acp.yml`
и при желании укажите `"args": ["--profile", "acp"]`):

```json
{
  "acp.agents": {
    "gc-acp": {
      "command": "/абсолютный/путь/gc-acp",
      "args": []
    }
  }
}
```

Панель ACP на боковой панели, агент `gc-acp`, Connect. В конфиге нужен
`os_backend: local`. Между кусками ответа клиент показывает, какой тул
вызван и короткое превью аргументов.

## Ключи запуска gc-cli

**Ключи запуска gc-cli** (см. `packages/cli/src/gc_cli/arg_parser.py`):

| Ключ | Описание |
|------|----------|
| `-q`, `--query` | Текст запроса к агенту (**обязателен**). |
| `-v`, `--verbose` | Подробный вывод о ходе запроса. |
| `-T`, `--technical` | Обернуть запрос в теги `<technical type="technical">…</technical>`. чтобы при суммаризации это сообщение не считалось как интент пользователя. нужно например, чтобы продолжить прерванную сессию |
| `-s`, `--skill` *SKILL_NAME* | Решать задачу через указанный skill. Резолвится детерминированно **до старта агента**: точный canonical / `<pack-id>` → bootstrap пакета (напр. `supercoder` → `supercoder:using-supercoder`) / краткое имя / единственное близкое при опечатке. Неоднозначность / промах / сбой каталога → `warning:` в stderr и **мягкий фолбэк**: имя передаётся агенту как есть, агент стартует (`-s` — подсказка-намерение, не жёсткий контракт). Пустой `-s` — no-op. |
| `--config` *путь* | Путь к файлу `config.yml` вместо дефолтного. |
| `-t`, `--thread-id` *id* | Продолжить диалог в существующем треде. |
| `-n`, `--new` | Начать тред с тем же `thread-id` заново (удалить старые `data_*`/`meta_*`). Разрешён **только вместе** с `-t` / `--thread-id`. |
| `--write-guard true\|false` | Включить/выключить `write_guard`: ограничение файловых изменений `launch_root` и `write_guard.extra_paths`. Не сочетать с `--no-write-guard`. |
| `--no-write-guard` | Явно отключить `write_guard` (эквивалент `--write-guard false`), приоритет над конфигом. |
| `--read-guard true\|false` | Включить/выключить `read_guard`: ограничение чтения файлов по `allowed_read_roots`/`denied_*`. Не сочетать с `--no-read-guard`. |
| `--no-read-guard` | Явно отключить `read_guard` (эквивалент `--read-guard false`), приоритет над конфигом. |
| `--read-guard-mode` | Режим `read_guard`: `allow` — без проверок, `ask` — подтверждение для подозрительного, `deny` — блокировка. |
| `--command-moderation` | Режим для `execute_command` / `run_python_code`: `allow` — без judge, `ask` — подтверждение опасного, `deny` — блокировка опасного. |
| `--agentsmd-override` | Только TUI: оставить лишь корневой `AGENTS.md` (L3), отбросив shipped, глобальный `~/.gc/AGENTS.md` и `.gc/AGENTS.md`. Вне TUI слои не грузятся вовсе — агент говорит об этом на старте. |
| `--profile` | Активировать профиль `<имя>` поверх указателя `profile:` из `config.yml` (`<проект>/.gc/profiles/` или `profiles/` рядом с базовым `config.yml` — Windows `~/.gc/profiles/`, Linux/macOS `~/.config/gc/profiles/`; при `--config` — каталог указанного файла). |
| `--level` | Активный уровень модели на этот запуск: `light` / `default` / `max` (перекрывает `config.yml` и профиль). |
| `-a`, `--auto-mcp` | Поднять HTTP/SSE MCP-демоны из `recommended_mcp/` перед запуском (если каталог есть в cwd); по завершении гасятся только процессы этого вызова. |
| `--reset-config` | Сбросить `config.yml` к пакетному умолчанию (подтверждение `y/N`, старый файл — в бэкап; профили и сессионные оверрайды не трогает) и выйти. |
| `--init-config` | Мастер настройки `~/.gc/config.yml`. В `gc-cli` — настроить и выйти; в `gc-ua` — по завершении запускается TUI. |
| `--init` | Сгенерировать `AGENTS.md` сканом репозитория и выйти (агент не запускается). |
| `--sync-config` | Обновить `config.yml` до текущей версии (новые поля, переименования; старый файл — в бэкап) и выйти. |
| `--check-update` | Проверить, доступна ли новая версия агента (без установки) и выйти. |
| `--update` | Обновить агента (`uv tool install` после завершения текущего процесса) и выйти. Аргументы установки — `update.extra_args` в конфиге. |

Любые **дополнительные** аргументы вида `--имя_ключа значение`, `--имя_ключа=значение` или одиночный `--флаг` (→ `true` в конфиге) трактуются как переопределения полей `config.yml` и имеют наивысший приоритет; типы значений разбираются через `yaml.safe_load` (подробнее — [Конфигурация](configuration.md#переопределение-настроек-из-cli)).

## Слои инструкций (AGENTS.md)

Агент получает инструкции из четырёх слоёв, которые объединяются от широкого к узкому:

| Слой | Источник | Описание |
|------|---------|----------|
| L0 | shipped (`paths.agent_md_path`) | Встроенные инструкции дистрибутива. |
| L1 | `~/.gc/AGENTS.md` | Персональные глобальные инструкции пользователя. |
| L2 | `.gc/AGENTS.md` в проекте | Проектные инструкции (локальные, не для VCS). |
| L3 | `AGENTS.md` в корне проекта | Проектные инструкции (общие); самый узкий слой. |

Сгенерировать `AGENTS.md` для текущего репозитория: `gc-cli --init` (сканирует структуру проекта и создаёт файл, агент не запускается). Флаг `--agentsmd-override` оставляет только L3 — корневой `AGENTS.md`, — отбрасывая shipped, глобальный и `.gc/AGENTS.md`.
