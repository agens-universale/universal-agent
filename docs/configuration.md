[Назад к README](../readme.md)

# Конфигурация

## Конфиг клиента

- Требуется запущенный GigaCode Agent с API `v1/responses` или с режимом `backend_api_mode: chat_completions`. Адрес сервиса задаётся в `config.yml`.
- **Где ищется базовый `config.yml`** (первый найденный источник побеждает):
  1. путь из `--config` (если задан);
  2. иначе `config.yml` в каталоге «Config» из таблицы [«Где gc-cli хранит runtime-данные»](#где-gc-cli-хранит-runtime-данные) — `~/.config/gc/config.yml` на Linux/macOS, `%USERPROFILE%\.gc\config.yml` на Windows (учитывает `GC_CONFIG_DIR` / `XDG_CONFIG_HOME` / `GC_HOME`). Это **полная база** (кладите полный конфиг — частичный файл здесь потеряет пакетные дефолты);
  3. иначе `config.yml` рядом с установленным пакетом (каталог, откуда был выполнен `pip install -e .`, подкаталог `src`).

  Поверх выбранной базы дополнительно deep-merge'ится project-local `<cwd>/.gc/config.yml` (см. [«Project local config (опционально)»](#project-local-config-опционально)). Чтобы указать свой путь явно:
  ```commandline
  gc-cli -q "ваш вопрос" --config ./config.yml
  ```
- Пример конфигурации: `demo_config.yml`. Скопируйте в `config.yml` и отредактируйте под свой бэкенд и MCP-серверы.
- В конфиге можно задать количество шагов агента (`max_steps`), логирование и другие параметры.
- Для режима `backend_api_mode: chat_completions` можно передавать токен провайдера
  (например OpenRouter) через `chat_completions_auth_token` (или алиас `openrouter_api_key`);
  тогда клиент отправит `Authorization: Bearer <token>` в запрос к `completions_url`.
  Опционально для OpenRouter поддерживаются заголовки `openrouter_http_referer`
  и `openrouter_x_title` (в запросе это `HTTP-Referer` и `X-OpenRouter-Title`).
- Для хранения истории тредов:
  - по умолчанию используется папка `threads` в корне проекта (`mini-cli-pub/threads`);
  - опциональный параметр `threads_data_path` в `config.yml` должен быть **абсолютным путём** к директории хранения; если не задан — используется кросс-платформенный дефолт `<state>/projects/<project-slug>/threads/` (на Linux/macOS — `~/.local/state/gc/...`, на Windows — `~/.gc/...`);
  - параметры суммаризации в `chat_completions` задаются в секции `summary_params`:
    - `summary_params.chat_completions_summary_trigger_chars`:
      - `<= 0` (или отсутствует) — суммаризация выключена (дефолт в коде: `0`); рекомендуемое значение для моделей с контекстом ~131k — `180000`;
      - `> 0` — при достижении/превышении порога тред архивируется (`data_*`/`meta_*` с суффиксом `__summarized_<YYYYMMDD_HHMMSS>`) и продолжается от compact summary;
    - `summary_params.incremental_batch_chars` — размер батча для кумулятивной суммаризации; если не задан, используется `int(summary_params.chat_completions_summary_trigger_chars * 1.4)`;
    - `summary_params.chat_completions_summary_prompt_path` (опционально), по умолчанию `prompts/chat_completions_summary_prompt.md`;
    - `summary_params.max_message_chars` — максимум символов на одно сообщение при подготовке истории к суммаризации (по умолчанию `50000`);
    - `summary_params.max_dialogue_chars` — максимум символов на весь диалог для суммаризации (по умолчанию `1000000`, берется хвост целыми сообщениями);
    - `summary_params.max_accumulated_summary_chars` — лимит длины накопленного summary; при превышении запускается дополнительное сжатие summary-of-summary (по умолчанию `20000`);
    - `summary_params.original_user_messages_max_chars` — лимит символов для `meta.user_messages_log` (единственный источник user-запросов для суммаризации); сохраняются последние сообщения целиком (по умолчанию `20000`);
    - `summary_params.last_dialog_steps_count` — сколько последних сообщений включать;
    - `summary_params.last_dialog_step_message_max_chars` — максимум символов на одно сообщение (лишнее обрезается).
  - **Поток суммаризации** (только при `backend_api_mode: chat_completions` и положительном `summary_params.chat_completions_summary_trigger_chars`):
    - перед стартом цикла агента выполняется **pre-run** compaction (если объём диалога в `state.messages` уже достиг порога);
    - после шага с исходящим результатом инструмента (**post-step**, сразу после обработки `function_call_output`) снова проверяется порог и при необходимости запускается compaction;
    - источник данных для решения «суммаризировать или нет» — только текущие сообщения сессии (`state.messages`); при запуске с `-t` после успешного compaction дополнительно обновляются файлы треда через `thread_store` (архив старых `data_*/meta_*` и запись compact-истории).
  - `gc-cli -q "..." -t my-thread` — продолжить диалог по треду;
  - `gc-cli -q "..." -t my-thread -n` (или `--new`) — начать тред заново с тем же id и удалить старые файлы этого треда (`data_*`/`meta_*`);
  - `-n/--new` можно использовать только вместе с `-t/--thread-id`.
  - в директории для тредов создаются файлы `data_<thread_id>.jsonl` (сообщения) и `meta_<thread_id>.json` (метаданные, включая `previous_response_id`).
- По умолчанию логи будут сохраняться в той папке, из которой запущена команда.
- **TUI**: секция `tui` управляет поведением интерфейса:
  - `tui.stream` (по умолчанию `true`) — мастер-переключатель live-стриминга ответа в TUI.
    При `false` ответ ждётся целиком; при `true` — чанки поступают по мере генерации.
    **Флаг задействован ТОЛЬКО в TUI:** на CLI/server/swarm не влияет — там всегда
    non-stream (стрим гейтится наличием `on_stream_delta`, который ставит только TUI).
    Legacy: если `tui.stream` не задан, читается старый `backend.stream` (backward-compat).
  - `tui.stream_display` (`minimal | answer | answer_reasoning`, по умолчанию `minimal`) —
    что показывать во время стриминга (`minimal` — только индикатор, `answer` — прогрессивный
    текст ответа, `answer_reasoning` — + однострочный «глазок» рассуждения).
    Актуально только при `backend_api_mode: chat_completions` и `tui.stream: true`.
  - `tui.text_answer_mode` (`deny | judge | allow`, по умолчанию `judge`) — как поступать
    с текстовым ответом агента без вызова инструмента:
    `deny` — всегда bounce (как CLI), `judge` — intent_guard решает, `allow` — принять текст.
  - `tui.file_view.enabled` / `max_file_bytes` / `page_lines` (`null` = авто по высоте)
    — постраничный просмотр `/view` в TUI (см. [tui.md](tui.md#просмотр-файла-view)).
- **Обновление**: секция `update` управляет самообновлением агента:
  - `update.check` (`auto | off`, по умолчанию `auto`) — проверять ли при запуске, вышла ли
    новая версия. Вердикт виден в TUI (`/settings` → Обновление) и по `gc-cli --check-update`.
  - `update.extra_args` (список, по умолчанию пустой) — дополнительные аргументы к
    `uv tool install` при обновлении. **В корпоративной сети обязателен**
    `["--system-certs"]`: без него установка упирается в TLS-перехват и падает —
    как из `/settings`, так и по `gc-cli --update`.
- **Слои инструкций** (`paths.agent_md_path`, `tui.agentsmd_override`) — см. раздел
  [«Слои инструкций (AGENTS.md)»](#слои-инструкций-agentsmd) ниже.

## Секреты и env-интерполяция (`${ENV:VAR}`)

В любом строковом значении конфига можно использовать плейсхолдер `${ENV:VAR}` — он
подставляется из переменной окружения **до валидации** (и в file-конфиге, и в CLI-override).
Удобно, чтобы не держать секрет в файле:

```yaml
backend:
  chat_completion_headers:
    Authorization: "Bearer ${ENV:GC_API_KEY}"   # затем: export GC_API_KEY=sk-...
mcp:
  servers:
    github:
      token: "${ENV:GITHUB_TOKEN}"
```

- Поддерживается несколько плейсхолдеров в одной строке (`"Bearer ${ENV:A}/${ENV:B}"`).
- Отсутствующая переменная → пустая строка + один warning с именем переменной (само значение не логируется).
- Переменная, заданная пустой строкой, подставляется как есть, без warning.

## Логи: формат, уровень, редакция секретов

- `logging.format` (`text | jsonl`, по умолчанию `text`):
  - `text` — человекочитаемый вывод (историческое поведение);
  - `jsonl` — один JSON-объект на строку (`{"event": ...}`): `agent_start`,
    `backend_request/response`, `dialog_message`, `final_answer`, `run_usage`, `summary_*` и др.
    В jsonl-режиме человекочитаемый рендер в консоль **подавляется** — на stdout идёт только
    event-поток (удобно для парсеров/оркестраторов, напр. Task2PR-врапера).
- `logging.log_level` (`debug | info | warning | error`, по умолчанию `info`) — уровень per-run
  логгера (актуален для jsonl). На `debug` добавляются объёмные события
  `backend_request` / `backend_response` / `agent_tools`.
- `logging.redact_secrets` — маскировка секретов (Bearer/JWT/`sk-`/PEM/AWS/GCP/GitLab/npm/Slack)
  перед записью в лог и stdout → `***REDACTED***`. По умолчанию **выключено** (поведение как раньше):
  - `enabled: true|false`;
  - `extra_keys: []` — доп. подстроки имён чувствительных ключей (к встроенным);
  - `extra_patterns: []` — доп. value-regex (к встроенным).

  Счётчики токенов (`prompt_tokens`, `completion_tokens`, `max_tokens`) не маскируются;
  одиночное слово-ключ `token` / `*_token` — маскируется.

  **Важно — text vs jsonl:** маскировка **по имени ключа** (`extra_keys` и встроенные имена)
  работает только в `format: jsonl` — там редактируется структурный словарь события, и значение
  поля с совпавшим именем целиком становится `***REDACTED***`. В `format: text` редактируется
  финальная строка (словаря нет), поэтому применяются только **value-regex** (`extra_patterns`
  + встроенные Bearer/`sk-`/JWT/PEM/…), а `extra_keys` **не действует**. Для гарантированного
  маскирования в обоих режимах используй `extra_patterns` (по формату значения).

## Backend TLS / mTLS

- `backend.use_ssl` — `true | false | "/path/ca-bundle.pem"`. Строка трактуется как путь к
  CA-bundle (self-signed / корпоративный CA); `bool` — как раньше (обратно совместимо).
- `backend.cert_path` / `backend.key_path` — клиентский сертификат (mTLS). Заданы **оба** →
  клиент предъявляет `(cert, key)`; ни одного → без клиентского сертификата (дефолт). Пути
  раскрывают `~` и env-переменные. (Опционально; не обязательны для работы бэкенда.)
- `backend.request_timeout_sec` — HTTP-таймаут одного запроса к LLM (секунды), по умолчанию
  `600` (10 минут). Для стрима лимит действует на connect и на паузу между чанками, а не на
  длительность всего ответа. Не путать с `mcp.meta.request_timeout_sec` (таймаут MCP).
- `agent.exec_timeout_sec` — дефолтный таймаут `execute_command` / `run_python_code`
  (секунды), если модель не передала `timeout_seconds`. По умолчанию `120`. Для длинных
  контейнерных сборок (Harbor) обычно поднимают до `600`.

## Слои инструкций (AGENTS.md)

Системные инструкции агента формируются из нескольких файлов, мерджимых в порядке broad→narrow
(каждый следующий слой уточняет/перекрывает предыдущий):

| Слой | Файл | Описание |
|------|------|----------|
| L0 (shipped) | `paths.agent_md_path` | Базовые инструкции из пакета (или кастомный путь) |
| L1 (global) | `~/.gc/AGENTS.md` (Linux/macOS: `~/.config/gc/AGENTS.md`) | Глобальные инструкции пользователя |
| L2 (project) | `<cwd>/AGENTS.md` | Инструкции проекта |
| L3 (project) | `<cwd>/.gc/AGENTS.md` | Уточнения для конкретного проекта |

- `paths.agent_md_path` (по умолчанию — shipped `prompts/demo_cli_agent.md`) задаёт L0.
  Укажите свой файл, если хотите заменить базовые инструкции.
- `tui.agentsmd_override` (`false` по умолчанию) — только TUI; при `true` оставить лишь
  корневой `AGENTS.md` (L3), отбросив глобальный (L1) и `.gc/AGENTS.md` слои.
  Игнорируется, если задан `paths.agent_md_path` (путь главнее флага).
  Из CLI: `--agentsmd-override`.
- Файл `AGENTS.md` генерируется командой `gc-cli --init` (агент сканирует репозиторий и
  создаёт/обновляет `AGENTS.md` через `write_to_file`).

## Security policy

Клиент поддерживает несколько независимых слоёв безопасности для файловых изменений,
чтения файлов, запуска команд/кода и сетевых запросов. Все слои включены по умолчанию
с режимом `ask` (secure-by-default), **кроме `protected_paths`** — он включён с режимом
deny (floor-защита без подтверждения).

```yaml
security:
  command_moderation:
    enabled: true        # on/ask по дефолту
    mode: ask            # allow | ask | deny
    allowed_command_examples: []
  write_guard:
    enabled: true        # on/ask по дефолту
    mode: ask            # allow | ask | deny
    extra_paths: []
  read_guard:
    enabled: true        # on/ask по дефолту
    mode: ask            # allow | ask | deny
  url_policy:
    enabled: true        # on/ask по дефолту
    mode: ask            # allow | ask | deny
    allowed_schemes: ["http", "https"]
    url_blacklist: []
  protected_paths:
    enabled: true        # floor-защита — всегда on
    extra: []
```

**Контракт режимов:**

- `ask` (дефолт) — при нарушении периметра (out-of-perimeter операция):
  - **TUI** — показывает confirm-баннер; пользователь подтверждает или отменяет.
  - **Headless** (CLI/server/swarm без интерактивного confirmer) — fail-closed **deny**
    с инструктивным сообщением агенту: операция заблокирована, нужно выбрать другой путь.
  - In-perimeter операции (запись/чтение внутри `launch_root`) проходят молча в любом режиме.
- `deny` — всегда блокировать, без запроса подтверждения.
- `allow` — всегда пропускать, записать в audit log как `out_of_perimeter`.

**Escape-hatch:** `mode: allow` или `enabled: false` — отключают enforcement для конкретного слоя.

### `security.write_guard` (файловый периметр)

- `enabled` (по умолчанию `true`) — при `false` периметр выключен, запись в любой путь разрешена.
- `extra_paths` — дополнительные директории, куда разрешена запись помимо `launch_root`.
  Относительные пути разрешаются от директории `config.yml`.
- `mode` (`allow | ask | deny`, по умолчанию `ask`) — реакция на нарушение периметра.

Включить/выключить из CLI: `--write-guard true|false` или `--no-write-guard`.

### `security.command_moderation` (LLM judge для команд)

- `enabled` (по умолчанию `true`) — при `false` judge не вызывается, все команды проходят.
- `mode` (`allow | ask | deny`, по умолчанию `ask`) — управляет только `execute_command` и `run_python_code`:
  - `allow` — выполнять без judge (эквивалент `enabled: false`);
  - `ask` — опасные команды: TUI → confirm-баннер; headless → fail-closed deny с сообщением;
  - `deny` — опасные команды блокировать без запроса.
- `allowed_command_examples` — positive examples для LLM judge; не строгий allowlist,
  а ориентир безопасного профиля команд проекта.
- `denied_command_examples` — negative examples (операторный deny-list). Явное совпадение
  (или тривиальная вариация: флаги/пути/кавычки/пробелы) → verdict `potentially_dangerous`
  + risk-флаг `operator_denied`, даже если команда сама по себе выглядит безобидно.
  Операторный интент главнее собственной оценки судьи. Работает при `mode: ask|deny`.
- **Трейдофф:** LLM-судья вызывается на каждую команду — добавляет задержку. При недоступности
  судьи: `deny` → fail-CLOSED; `ask` → headless deny, TUI → confirm-баннер.
  Отключить: `enabled: false`.

Переключить режим из CLI: `--command-moderation allow|ask|deny`.

### `security.read_guard` (периметр на чтение)

- `enabled` (по умолчанию `true`) — при `false` чтение файлов не ограничивается.
- `mode` (`allow | ask | deny`, по умолчанию `ask`) — реакция на нарушение.
- Дополнительные поля: `allowed_read_roots`, `allowed_read_paths`, `denied_read_paths`,
  `denied_file_patterns`, `denied_extensions`.

Включить/выключить из CLI: `--read-guard true|false` или `--no-read-guard`.
Режим: `--read-guard-mode allow|ask|deny`.

### `security.url_policy` (URL-политика)

- `enabled` (по умолчанию `true`) — при `false` проверки URL не выполняются.
- `mode` (`allow | ask | deny`, по умолчанию `ask`) — реакция на нарушение.
- `allowed_schemes` и `url_blacklist` проверяются для исходного URL
  в `fetch_url`/`simple_fetch_url` до сетевого запроса. Redirect targets повторно не проверяются.

### `security.protected_paths` (floor-защита)

- `enabled` (по умолчанию `true`) — защищает операционный каталог `.gc` от записи и чтения
  даже когда остальные guard'ы выключены. Также ставит детерминированный tripwire:
  команды вроде `rm -rf .gc` блокируются до запуска subprocess, без обращения к LLM-судье.
- `extra` — дополнительные пути с floor-защитой.

### Основные policy-ошибки

- `write_guard_violation` — файловое изменение вне разрешённых roots (write_guard);
- `unsafe_command_blocked` — опасная команда/код заблокированы в `deny`;
- `unsafe_command_cancelled_by_user` — пользователь не подтвердил опасное действие в `ask`;
- `url_validation_failed` — URL не прошёл схему/формат;
- `url_blocked_by_policy` — URL совпал с blacklist.

Все policy-решения пишутся в run logger как `security_policy_decision` без полного payload команды/кода.

## Переопределение настроек из CLI

Любой параметр из `config.yml` (или новый, которого там нет) можно переопределить при запуске через дополнительные аргументы командной строки.

- Значения из CLI **всегда имеют приоритет** над значениями из `config.yml`.
- Поддерживаются следующие формы:
  - `--param value`
  - `--param=value`
  - `--flag` (без значения, в конфиг уйдёт `true`)
- Значения автоматически приводятся к типам с помощью `yaml.safe_load`, поэтому:
  - `--max_steps 50` → `config_dict["max_steps"] == 50` (число),
  - `--verbose true` → `config_dict["verbose"] == True` (булево),
  - `--client_name "my_client"` → `config_dict["client_name"] == "my_client"` (строка).

**Примеры:**

- Отключить файловый периметр записи для разового запуска:
  ```bash
  gc-cli -q "..." --no-write-guard
  ```

- Переключить режим command moderation:
  ```bash
  gc-cli -q "..." --command-moderation ask
  ```

- Переопределить существующие ключи:
  ```bash
  gc-cli -q "..." --agent_md_path "my path" --max_steps 50
  ```
  В рантайме будет использовано:
  - `config_dict["agent_md_path"] == "my path"`,
  - `config_dict["max_steps"] == 50`,
  даже если в `config.yml` заданы другие значения.

- Добавить новый параметр, которого нет в `config.yml`:
  ```bash
  gc-cli -q "..." --my_custom_flag true --extra_param "some value"
  ```
  В `config_dict` появятся:
  - `config_dict["my_custom_flag"] == True`,
  - `config_dict["extra_param"] == "some value"`.

## MCP-серверы

Клиент поддерживает MCP (Model Context Protocol): агент может вызывать инструменты
внешних MCP-серверов (поиск, RAG, файловая система, выполнение кода и т.д.).

Настройка — в `config.yml`, секция `mcp-servers`. Каждый сервер задаётся ключом-именем
(например `rag`, `search`, `python`); этот ключ служит префиксом имён инструментов
(например `search_web_search`). Используйте короткие имена из латинских букв, цифр и символов `-_`, без пробелов.

Поддерживаются шесть типов транспорта MCP:

**Сессионные транспорты** (один запрос — один ответ, при необходимости используется `mcp-session-id`):

- **http** — запросы по HTTP. Обязательно: `transport: http`, `url`, `description`, `server-name`. Опционально: `token`, `verify-ssl`.
- **sse** — обмен через Server-Sent Events. Обязательно: `transport: sse`, `url`, `description`, `server-name`. Опционально: `token`, `verify-ssl`.
- **stdio** — MCP-сервер как локальный процесс (stdin/stdout); процесс запускается на каждый запрос. Обязательно: `transport: stdio`, `command`, `description`, `server-name`. Опционально: `args`, `cwd`, `env`.

**Стриминговые транспорты** (через MCP SDK, без сессий; подходят для серверов, поддерживающих streamable-протокол):

- **stream_http** — Streamable HTTP (MCP 1.26+). Обязательно: `transport: stream_http`, `url`, `description`, `server-name`. Опционально: `token`, `verify-ssl`. Endpoint обычно `.../mcp`; при 404 пробуется базовый URL.
- **stream_sse** — SSE в формате MCP SDK. Обязательно: `transport: stream_sse`, `url`, `description`, `server-name`. Опционально: `token`, `verify-ssl`.
- **stream_stdio** — stdio через MCP SDK (процесс на каждый запрос). Обязательно: `transport: stream_stdio`, `command`, `description`, `server-name`. Опционально: `args`, `cwd`, `env`.

**Общие параметры:** для любого транспорта можно задать `verify-ssl: true` (по умолчанию) или `verify-ssl: false` (отключить проверку SSL, например для self-signed сертификатов).

**Кроссплатформенность (stdio / stream_stdio):** в `command` и `args` указывайте команду, доступную в PATH на вашей ОС (например, на Windows — `python` или `py`, а не только `python3`). Пути к скриптам в `args` лучше задавать в формате, принятом на целевой ОС, или использовать переменные окружения в `env`.

В секции `mcp-meta` можно задать:
- `tools-cache-ttl` — время жизни кэша списка инструментов (секунды);
- `request_timeout_sec` — таймаут запроса к MCP (секунды).

Полные примеры для http, sse, stdio и комментарии по stream_http/stream_sse/stream_stdio — в `demo_config.yml`. Скопируйте нужный блок, подставьте свои `url` или `command`/`args`.

### Встроенные интеграции — `recommended_mcp/`

В корне репозитория есть каталог `recommended_mcp/` с готовыми рецептами
подключения нескольких MCP.

**Основной способ — `gc-mcp install`** (автоматически ставит пакет и прописывает в `~/.gc/config.local.yml`):

```bash
gc-mcp install computer   # пример: установить computer MCP
gc-mcp list               # список установленных
gc-mcp remove computer    # удалить
```

**Фолбэк — ручная установка** (если `gc-mcp` недоступен):

```bash
python recommended_mcp/install.py
```

После ручной установки нужно скопировать сниппеты из
`recommended_mcp/<name>/agent_config.snippet.yml` в секцию `mcp-servers:`
своего `config.yml`. На момент написания доступны:

- **`computer`** — управление Windows GUI через UI Automation (запуск
  приложений, клики, ввод, чтение состояния окон). Только Windows.
  Исходники: `external/computer-mcp/`.
- **`office`** — чтение офисных документов (pdf, docx, xls, xlsx, pptx,
  csv, html, msg) → markdown. Кросс-платформенно. Исходники:
  `external/office-mcp/`.

Подробности про каждую интеграцию — в её `recommended_mcp/<name>/README.md`.
Общая документация по `install.py` / `run.py` / структуре каталога —
в `recommended_mcp/README.md`.

Для итеративной разработки наших MCP в `external/`:

```bash
python recommended_mcp/install.py --editable
```

### Ручная установка (для MCP не из `recommended_mcp/`)

Если нужный MCP не входит в `recommended_mcp/` — ставь как обычный пакет
и подключай через `mcp-servers:`. Пример с DDGS-поиском:

```bash
python3 -m pip install ddgs-mcp
ddgs-mcp --http
```

В `config.yml` агента укажи нужный транспорт (`http`, `stream_http` и т.д.)
и endpoint подключения (см. раздел [MCP-серверы](#mcp-серверы) выше).

## Где gc-cli хранит runtime-данные

Кросс-платформенная раскладка. По умолчанию:

| ОС | Логи / треды / swarm | Cache (пока не используется)          | Config |
|---|---|---------------------------------------|---|
| Linux | `~/.local/state/gc/projects/<slug>/` | `~/.cache/gc/` *(резерв)*             | `~/.config/gc/` |
| macOS | `~/.local/state/gc/projects/<slug>/` *(как Linux)* | `~/.cache/gc/` *(резерв)*             | `~/.config/gc/` |
| Windows | `%USERPROFILE%\.gc\projects\<slug>\` | `%USERPROFILE%\.gc\cache\` *(резерв)* | `%USERPROFILE%\.gc\` |

`<slug>` — путь к проекту с заменой `/`, `\`, `:` на `-`, в стиле `~/.claude/projects/`.

Override через переменные окружения: `GC_HOME` (корневой), `GC_STATE_DIR` / `GC_CONFIG_DIR` / `GC_CACHE_DIR` / `GC_DATA_DIR` (точечные). На Linux/macOS дополнительно работают `XDG_STATE_HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_DATA_HOME`.

Каталог «Config» — это где `gc-cli` ищет базовый `config.yml` (tier между `--config` и пакетным дефолтом; подробнее — [«Где ищется базовый config.yml»](#конфиг-клиента) в начале страницы).

Каталог «Cache» — зарезервирован в кросс-платформенной XDG-раскладке, но `gc-cli` **пока не использует файловый кэш**: путь вычисляется (и переопределяется `GC_CACHE_DIR` / `XDG_CACHE_HOME`), но каталог не создаётся и в него ничего не пишется.

См. также пояснения в `demo_config.yml` (секция `paths`).

## Project local config (опционально)

Если в корне проекта есть файл `<your-repo>/.gc/config.yml`, gc-cli мерджит его поверх глобального `config.yml` через layered loading:

- **scalar** (число, строка, bool) — project переопределяет global.
- **dict** — рекурсивный deep merge: project расширяет/переопределяет ключи внутри.
- **list / array** — конкатенация + dedup: project только **расширяет** список, не сбрасывает (важно для security-полей вроде `agent.stop_tools` или `command_moderation.url_policy.url_blacklist`).

Пример проектного `<your-repo>/.gc/config.yml`:

```yaml
agent:
  max_steps: 200             # этот проект — длиннее цепочки рассуждений
  stop_tools: [browse_url]    # добавить локальный запрет (к глобальному списку)
mcp:
  servers:
    my_local_db:               # MCP только для этого проекта
      transport: stdio
      command: psql
```

Файл `.gc/config.yml` следует той же схеме, что и глобальный `config.yml` — указывайте только те поля, которые хотите переопределить или дополнить. Папка `.gc/` создаётся автоматически при первом запуске `gc-cli` (та же папка, что и для project-скиллов).

CLI-флаги (`--max_steps 999` и т.п.) перебивают и global, и project. Файл опционален; если его нет — используется только global.

Если через `--config` явно передан тот же файл, что и `<repo>/.gc/config.yml` — gc-cli применяет его как обычный config без второго слоя (особый случай для запусков с явным путём).
