# Настройка Temporal

В пакете два отдельных протокола. **Native** (`gc-temporal-native-worker`,
`gc-temporal-native`, server `temporal-native`) использует настоящий агентский
цикл `gc-core`. **Legacy** (`gc-temporal-worker`, `gc-temporal`, server `temporal`)
сохраняет прежние workflow и их histories. Форматы worker JSON различаются;
legacy-профиль ниже не является конфигурацией native worker.

Общая приёмка native рабочей ветки продолжается; проверенные сценарии и границы
описаны в [разделе 14 архитектуры](temporal-architecture.md#14-реализация-в-рабочей-ветке).
Локальный `config.yml` и запуск без Temporal остаются прежними.

## Native quickstart

### Конфигурация и размещение

Готовый [examples/native-worker.json](../packages/temporalio/examples/native-worker.json)
читает `NativeWorkerConfig`. `profiles.production.core` — полный обычный
структурированный `AppConfig`, а не сокращённая legacy-схема. Неизвестные поля
отклоняются при валидации. Пример не содержит секретов:

```json
{
  "address": "localhost:7233",
  "namespace": "default",
  "workflow_queue": "ua-native-workflow",
  "model_queue": "ua-native-model",
  "resource_queue": "ua-native-resource-local",
  "worker_id": "native-local",
  "receipts_database": "${ENV:UA_NATIVE_STATE}/receipts.sqlite",
  "thread_stores": {
    "files": {
      "kind": "file",
      "path": "${ENV:UA_NATIVE_STATE}/threads"
    }
  },
  "profiles": {
    "production": {
      "revision": "v1",
      "workspace": "${ENV:UA_NATIVE_WORKSPACE}",
      "store_id": "files",
      "preview_stream": false,
      "core": {
        "backend": {
          "api_mode": "chat_completions",
          "completions_url": "${ENV:UA_MODEL_URL}",
          "chat_completion_headers": {
            "Authorization": "Bearer ${ENV:UA_MODEL_API_TOKEN}"
          },
          "chat_completion_kwargs": {
            "stream": false
          }
        },
        "agent": {
          "model": "${ENV:UA_MODEL_NAME}"
        }
      }
    }
  }
}
```

Перед запуском задайте `UA_NATIVE_WORKSPACE`, `UA_NATIVE_STATE`, `UA_MODEL_NAME`,
`UA_MODEL_URL` и `UA_MODEL_API_TOKEN` в окружении. Workspace и state — абсолютные
пути; state размещайте **вне workspace**, чтобы служебная БД и история не попадали
в результаты файловых инструментов. Каталоги создайте заранее. `${ENV:...}` и
`${FILE:...}` раскрываются при загрузке; отсутствующая переменная даёт
предупреждение и пустое значение, поэтому проверьте окружение до старта.

FileThreadStore выбран явно и остаётся default для истории. Для SQLite замените
store на `{"kind":"sqlite","path":"/absolute/state/threads.sqlite"}`.
`receipts_database` — отдельный технический журнал операций, эффектов, delivery
checkpoint и операторских решений; он не заменяет выбранный ThreadSession store.

Текущий native deployment — **один процесс, один workspace**, в котором работают
workflow/model/resource queues. Все профили этого worker должны указывать тот же
workspace; `store_id` профиля должен существовать в `thread_stores`. Cwd процесса
обязан совпадать с workspace. Worker проверяет это и не меняет cwd. Для второго
workspace нужны другой процесс и отдельные очереди; раздельные model/resource
процессы этим entrypoint пока не поддержаны. Несколько процессов не должны
конкурировать за очередь, владеющую живыми MCP/background ресурсами.

### Запуск worker и клиента

Нужен доступный Temporal Server (`localhost:7233` в примере). Для отдельного
локального стенда можно запустить `temporal server start-dev`; существующий
сервер пересоздавать не нужно. Из checkout репозитория пакет устанавливается
через optional extra `temporal`. Ниже замените абсолютные пути своими:

```bash
export UA_REPO=/absolute/path/universal-agent
export UA_NATIVE_WORKSPACE=/absolute/path/project
export UA_NATIVE_STATE=/absolute/path/agent-state
export UA_MODEL_NAME=your-model
export UA_MODEL_URL=https://your-provider/v1/chat/completions
# UA_MODEL_API_TOKEN заранее предоставлен окружением/хранилищем секретов.
mkdir -p "$UA_NATIVE_WORKSPACE" "$UA_NATIVE_STATE"
cd "$UA_NATIVE_WORKSPACE"
uv run --project "$UA_REPO" --extra temporal gc-temporal-native-worker \
  --config "$UA_REPO/packages/temporalio/examples/native-worker.json"
```

`uv run --project` выбирает Python project, **сохраняя cwd вызывающего процесса**;
это проверено запуском Python из другого каталога. Не заменяйте его на
`--directory "$UA_REPO"`: worker тогда окажется вне workspace. При установленном
пакете можно вызвать `gc-temporal-native-worker` напрямую из workspace.

В другом терминале с тем же `UA_REPO`:

```bash
uv run --project "$UA_REPO" --extra temporal gc-temporal-native \
  --address localhost:7233 --namespace default \
  --workflow-queue ua-native-workflow start \
  --profile production --revision v1 --store files --system cli --thread example \
  --resource-queue ua-native-resource-local --model-queue ua-native-model \
  --prompt 'Прочитай README рабочей папки и кратко опиши проект'
```

Клиент печатает workflow ID и не читает worker JSON: profile/revision/store и
очереди должны совпадать с конфигурацией worker. Для `status WORKFLOW_ID`,
`result WORKFLOW_ID`, `stop WORKFLOW_ID` используется тот же префикс команды.
Старт с `--keep-alive` сохраняет сессию для `send WORKFLOW_ID --message-id UNIQUE_ID
--text TEXT`; `close WORKFLOW_ID` закрывает её после принятой работы.
`answer WORKFLOW_ID --request-id REQUEST_ID --answer TEXT` отвечает человеку.
Structured output включается `start --structured-output-format /absolute/format.txt`.
Для неопределённых исходов предусмотрены `inspect`, `resolve`, `resume` и
[отдельный порядок восстановления](#native-operator-recovery).

### Модель, ревизии и живые ресурсы

Поддержаны общий Chat Completions и Responses. Для Responses задайте
`core.backend.api_mode: "responses"` и соответствующий endpoint в
`core.backend.completions_url`, например `/v1/responses`. Это тот же модельный
порт ядра; Responses cache-miss восстанавливает контекст общим механизмом.
`preview_stream: true` — явный opt-in потокового preview, требующий совместимого
streaming-провайдера; пример оставляет JSON Chat Completions без preview.

Полный раскрытый профиль закрепляется по имени и ревизии в техническом журнале.
Изменение настроек требует нового `revision`; это относится и к изменённым
значениям headers после `${ENV:...}`. Сохраняйте старые конфигурации и их worker
очереди для активных histories; автоматической миграции на другую ревизию нет.
Credentials не входят в публичный prepared context workflow.

MCP задаётся обычным `core.mcp`, субагенты — штатными настройками `core.agent`
и registry. Legacy `mcp_host_port`, top-level `mcp_servers` и таблица `subagents`
ниже к native JSON не применяются. Native MCP discovery и teardown исполняются
в одной задаче `WorkerResourceHost`; background-владелец создаётся при первом
реальном фоновом эффекте. Потеря процесса не разрешает скрытый reconnect/repeat.
File/model контексты без живых ресурсов восстанавливаются из pinned profile.

## Legacy: минимальный worker

Оставшаяся legacy-конфигурация нужна `gc-temporal-worker --config` и сохранена
для старого протокола; она не ограничивает `NativeWorkerConfig`.

Все пути абсолютные. Workspace создаётся заранее. Ключ провайдера передаётся
через окружение, например `UA_MODEL_API_KEY`; значение ключа не записывайте в JSON.

```json
{
  "address": "localhost:7233",
  "namespace": "default",
  "workflow_queue": "ua-workflow",
  "model_queue": "ua-model",
  "resource_queue": "ua-resource-local",
  "database": "/absolute/path/threads.sqlite",
  "artifacts": "/absolute/path/artifacts",
  "profiles": {
    "demo": {
      "revision": "v1",
      "workspace": "/absolute/path/workspace",
      "model": "your-model",
      "completions_url": "https://your-provider/v1/chat/completions",
      "api_key_env": "UA_MODEL_API_KEY",
      "max_steps": 20,
      "max_tokens": 4096,
      "max_tool_calls": 200,
      "session_token_limit": 200000,
      "request_timeout_seconds": 90,
      "inline_output_chars": 8000,
      "summary_trigger_chars": 60000,
      "summary_trigger_tokens": 16000,
      "preview_stream": false
    }
  }
}
```

Один worker process обслуживает три queues. Resource activities и MCP host
привязаны к одному хосту с workspace/SQLite/artifacts. Для другого хоста задайте
другую resource queue и отдельное хранилище. Эта версия подключения Temporal
ориентирована на локальный dev server/доверенный self-hosted namespace; настройки
Temporal Cloud TLS/API key не добавлены. REST-клиенты используют gc-server.

Профиль включает `system_prompt`, список `tools`, `mcp_servers` и `subagents`.
Изменение любой настройки требует нового `revision`; храните прежний worker
для уже запущенных сессий. Credential reference — имя переменной окружения;
ротация её значения не меняет профиль.

| Поле профиля | Ограничение |
|---|---|
| `max_steps` | 1–50 |
| `max_tokens` | 256–32000 |
| `max_tool_calls` | 1–500 |
| `session_token_limit` | 1–2000000; зависит от usage провайдера |
| `request_timeout_seconds` | 1–180 |
| `inline_output_chars` | 256–16000 |
| `summary_trigger_chars` | 64–120000 |
| `summary_trigger_tokens` | 0–64000; 0 отключает token trigger |
| `preview_stream` | `false` по умолчанию; `true` требует streaming Chat Completions |
| `command_moderation_mode` | `ask` либо `deny` |
| `text_answer_mode` | Только `deny`; завершение через `turn_finish` |
| `strict_turn_finish_validation` | Только `false` в этом deployment profile |

Без `tools` включены: `fs_read_file`, `session_memory`, `session_todo`,
`exec_calculator`, `exec_sleep`, `turn_intermediate_reply`, `turn_finish`,
`session_read_stored_tool_output`.

Дополнительно поддержаны:

- чтение: `fs_file_info`, `fs_list_files`, `fs_find_files_by_name`, `fs_find_in_files`;
- изменения файлов: `fs_write_file`, `fs_append_file`, `fs_apply_file_edits`,
  `fs_search_and_replace`, `fs_save_base64_file`, `download_file`;
- исполнение: `exec_command`, `exec_python`;
- управление: `turn_ask_human`, `session_compact`.

Переданный `tools` заменяет список целиком. В нём обязательны `turn_finish`
и `session_read_stored_tool_output`. `download_file` пишет в существующий
`downloads/` внутри workspace. Правила read/write guard исходного core сохраняются;
команды не превращаются в файловую песочницу и получают права процесса worker.

## Legacy: MCP host

Добавьте top-level конфигурацию и имя сервера в `profiles.demo.mcp_servers`:

```json
{
  "mcp_host_port": 7335,
  "mcp_host_token_env": "UA_MCP_HOST_TOKEN",
  "mcp_lease_seconds": 300,
  "mcp_servers": {
    "tools": {
      "revision": "v1",
      "transport": "stream_stdio",
      "persistent_session": true,
      "command": "/absolute/path/python",
      "args": ["/absolute/path/mcp_server.py"],
      "timeout_seconds": 120,
      "execute_on": "agent"
    }
  }
}
```

Фрагмент дополняет основной JSON. `UA_MCP_HOST_TOKEN` — общий случайный токен
worker и host. RPC слушает только loopback; это внутренний протокол управления
host, а не новый MCP transport. Запускайте host перед worker:

```bash
uv run gc-temporal-mcp-host --config /absolute/path/runtime.json
uv run gc-temporal-worker --config /absolute/path/runtime.json
```

Вариант подключения к HTTP MCP:

```json
{
  "revision": "v1",
  "transport": "stream_http",
  "persistent_session": true,
  "url": "https://mcp.example/mcp",
  "token_env": "MCP_SERVICE_TOKEN",
  "headers_env": {"X-Client-ID": "MCP_CLIENT_ID"},
  "verify_ssl": "/absolute/path/ca.pem",
  "client_cert": "/absolute/path/client.crt",
  "client_key": "/absolute/path/client.key",
  "timeout_seconds": 120,
  "execute_on": "agent"
}
```

`verify_ssl=true` использует стандартное доверие. TLS-поля передаются исходному
client port; не меняют гарантии восстановления прикладной сессии. Поддержаны
`stdio`, `stream_stdio`, `sse`, `stream_sse`, `http`, `stream_http`.
`execute_on` — `agent` или `os_backend` (в этом профиле backend локальный).
Для stateless сервера задайте `persistent_session=false`; логический receipt
вызова всё равно хранится host. `sampling` и `elicitation` разрешены только как
`false`. До 64 обнаруженных tools, имя до 64 символов; discovery сохраняется
в bootstrap snapshot, а не повторяется при replay.

## Legacy: субагенты

Добавьте отдельный профиль `worker` с тем же workspace и нужными разрешёнными
tools/model, затем в родительский профиль:

```json
{
  "subagents": {
    "subagent_worker": "worker",
    "subagent_code_generator": "worker"
  }
}
```

Имена берутся из встроенного реестра core: `subagent_worker`, `subagent_code_generator`,
`subagent_code_planner`, `subagent_planner`, `subagent_critic`,
`subagent_code_reviewer`, `subagent_special_agent`. Child profile не должен
содержать собственных `subagents`. Новая MCP-сессия ребёнка не наследует Python
handle родителя; общее состояние — переданные memory и файлы workspace.

## HTTP backend и удалённые CLI/TUI

В `config_server.yml` включите backend явно:

```yaml
server:
  host: 127.0.0.1
  port: 8000
  auth_token: ${ENV:GC_REMOTE_TOKEN}
  execution_backend: temporal
  temporal_config: /absolute/path/runtime.json
  temporal_profile: demo
```

Серверу нужны доступ к той же SQLite и соединение с Temporal. Он не исполняет
модель и инструменты. Обычные `/events/webhook`, `/runs/{system}/{task_id}` и
`/logs/{system}/{task_id}` используют Temporal adapter; webhook требует стабильный
`message_id`. Отключение API процесса не снимает workflow. Default
`execution_backend: local` сохраняет прежнее поведение сервера.

Основной remote API, под существующей Bearer-авторизацией сервера:

| Метод и суффикс `/temporal/{system}/{thread}` | Назначение |
|---|---|
| `POST /messages` | `{"message_id":"m1","text":"..."}`: start или следующий ввод |
| `GET /status` | Phase, ответ, pending human, active child, UNKNOWN |
| `GET /history?after=0&limit=100` | Подтверждённые сообщения с seq и execution meta |
| `GET /events?after=0&limit=200` | Preview/commit events с cursor |
| `GET /stream` | SSE; resume через `Last-Event-ID` или `after` |
| `POST /answer` | `request_id`, `reply_id`, `answer` |
| `POST /stop` | `{"turn_id":"..."}`: остановка текущего хода |
| `POST /close` | Дождаться принятой работы и закрыть сессию |
| `POST /reconcile` | Поля `ReconcileRequest`, как у CLI |
| `PATCH /ui` | Только `title`, `pinned`, `archived` |
| `GET /metrics` | Attempts, UNKNOWN/SESSION_LOST и projection lag |

Remote SDK: `gc_ua_temporalio.remote.RemoteSessionProvider`. Прямой SDK:
`gc_ua_temporalio.client.TemporalAgentClient`. Методы `status`, `submit_message`,
`answer_human`, `stop_turn`, `reconcile_operation`, `close_session`, `result`.
Прямой SDK получает уже подключённый `temporalio.client.Client`, поэтому
встраивающее приложение может само настроить TLS/auth соединения.

### Native HTTP backend

`execution_backend: temporal` сохраняет прежний совместимый runtime и его
histories. Новый путь включается отдельно:

```yaml
server:
  execution_backend: temporal-native
  temporal_config: /absolute/path/native-worker.json
  temporal_profile: production
  temporal_store: files
```

`native-worker.json` — тот же `NativeWorkerConfig`, который загружает native
worker. `temporal_store` должен совпадать с `store_id` выбранного pinned profile.
API-процессу нужен доступ к выбранному File/SQLite ThreadStore и технической
receipts DB; модель и инструменты исполняет worker. Помещайте receipts DB вне
workspace агента (например, `/var/lib/universal-agent/native/receipts.sqlite`),
чтобы служебные записи не попадали в результаты файловых инструментов.

Remote CLI/TUI и HTTP-суффиксы `status`, `history`, `events`, `messages`, `answer`,
`stop`, `close` остаются прежними. Native `history` читает настоящие сообщения и
метаданные ThreadStore. Поле `token` проверяет уже прочитанный префикс; при
компактировании/замене истории сервер возвращает `reset: true`, и TUI обновляет
историю целиком. UI title/pinned/archived записываются в `meta.tui` того же store.
`events` содержит только необязательные previews с operation/attempt/cursor;
canonical результат остаётся в ThreadStore. Preview включается явно через `profiles.<name>.preview_stream: true` в native
worker config. Это подключает штатный stream callback; `tui_stream`/backend stream
policy по-прежнему может его отключить. По умолчанию callback отсутствует,
поэтому транспорт остаётся тем же, что у локального CLI/server.

Native workflow ID имеет отдельное пространство имён. Чужой workflow owner
не принимается как native-сессия; переключение backend не мигрирует историю.
Сервер стартует native-сессии с `keep_alive`, поэтому отключение remote клиента
не завершает исполнение. `/close` закрывает сессию после принятой работы,
`/stop` проверяет актуальный `turn_id`. Native `/reconcile` явно отклоняется:
операторское разрешение неопределённого эффекта выполняется через effect ledger,
а не через legacy SessionEngine.

### Native operator recovery

Если worker потерян после начала внешней операции без сохранённой квитанции,
workflow переходит в `needs_reconciliation`. `status.pending_operation` содержит
activity, owner, operation ID и digest. Для субагента ожидание и операторские
команды доступны через родителя; идентичность операции остаётся дочерней.
Инструмент или модель автоматически повторно не вызываются.

Для активного ожидания доступны `gc-temporal-native inspect WORKFLOW_ID`,
`resolve WORKFLOW_ID --receipt resolution.json` и
`resume WORKFLOW_ID --operation-id OPERATION_ID --digest DIGEST`.
HTTP-эквиваленты: POST `/temporal/{system}/{thread}/operations/inspect`,
`operations/resolve`, `operations/resume`. Это отдельные native-контракты;
legacy `/reconcile` не используется.

`resolution.json` содержит `request_id`, `operation_id`, `digest`,
`ledger_digest`, `expected_status`, полный canonical `result` и `evidence` с
`source`, `note`, `result_sha256`. Digest результата — SHA256 от
`json.dumps(result, sort_keys=True, ensure_ascii=False).encode()`.
Используйте реально сохранённую квитанцию/ответ и проверенные наблюдения;
пустой или предположительный ответ модели не заменяет потерянный результат.
Для live-ресурса дополнительно нужен полный `ledger_result` типа
`ResourceEffectResult`, включая сведения о фоновых процессах.

`resolve` проверяет вид результата, оба digest, статус эффекта и fence треда,
затем одной транзакцией сохраняет evidence/audit, effect receipt и canonical
operation receipt. Повтор идентичного `request_id` идемпотентен. `resume` ничего
не исполняет: он принимает только уже существующую подтверждённую квитанцию.
Обе команды продолжают тот же workflow. Они не восстанавливают память MCP и
не превращают `OWNER_LOST` в новое соединение.

`stop` или `close` во время `needs_reconciliation` прерывают ожидание с явным
`OutcomeUnknown`; неизвестный эффект и ownership fence сохраняются. Для уже
завершившегося/упавшего workflow есть отдельный CLI, работающий с выбранным
реальным ThreadStore и Temporal:

```bash
uv run python -m gc_ua_temporalio.native_operator_cli \
  --config /absolute/path/native-worker.json --store files \
  --system production --thread task-1 inspect-closed

uv run python -m gc_ua_temporalio.native_operator_cli \
  --config /absolute/path/native-worker.json --store files \
  --system production --thread task-1 resolve-closed --receipt closed-receipt.json

uv run python -m gc_ua_temporalio.native_operator_cli \
  --config /absolute/path/native-worker.json --store files \
  --system production --thread task-1 release-thread \
  --request-id recovery-1 --evidence 'Проверен terminal run и квитанции эффектов'
```

`closed-receipt.json` — объект `{"pending": ..., "resolution": ...}`.
`inspect-closed` выводит исходный `pending` для обычных native effects и точные
operation IDs неопределённых операций. Для live effect без сохранённого
NativeCall необходим исходный DTO из ActivityScheduled в Temporal history;
данные инструмента и resource owner должны точно совпасть с ledger.
`resolve-closed` урегулирует квитанцию, не возобновляя завершённый workflow.

`release-thread` допускается только для последнего terminal run, совпадающего
с owner треда, при отсутствии STARTED/UNKNOWN effects этого run и его дочерней
lineage. Освобождение использует revision/generation/owner/epoch CAS и отдельный
audit. Повтор команды не освобождает следующего владельца. После освобождения
можно явно запустить новый native workflow с новым workflow ID; reset старого
workflow и автоматическое пересоздание MCP эта команда не выполняет.

Native JSON-конфиг поддерживает общую интерполяцию core `${ENV:VAR}` и
`${FILE:path}` до валидации. Например, в `profiles.production.core.backend`:

```json
{
  "completions_url": "${ENV:MODEL_COMPLETIONS_URL}",
  "chat_completion_headers": {
    "Authorization": "Bearer ${ENV:MODEL_API_TOKEN}"
  }
}
```

Значения раскрываются только при загрузке на worker/API-процессе; исходный файл
не переписывается. Transport URL, headers и credentials не входят в публичный
prepared context workflow. Отсутствующие переменные обрабатываются общей
политикой core: предупреждение с именем переменной и пустая подстановка.
