# Настройка Temporal

Пакет `gc-ua-temporalio` запускает настоящий агентский цикл `gc-core` через
Temporal: worker `gc-temporal-native-worker`, клиент `gc-temporal-native`,
server backend `temporal-native`. Прежний экспериментальный протокол
(`gc-temporal-worker`, `gc-temporal`, server `temporal`) удалён вместе с его
workflow; его histories не поддерживаются и не мигрируются.

Проверенные сценарии и ограничения описаны в
[архитектуре](temporal-architecture.md#12-проверки).
Локальный `config.yml` и запуск без Temporal остаются прежними.

## Native quickstart

### Конфигурация и размещение

Готовый [examples/native-worker.json](../packages/temporalio/examples/native-worker.json)
читает `NativeWorkerConfig`. `profiles.production.core` — полный обычный
структурированный `AppConfig`, а не сокращённая схема. Неизвестные поля
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
`receipts_database` — техническая SQLite worker'а: результаты завершённых
activities и preview. Она не заменяет выбранный ThreadStore.

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
О поведении при сбоях — [ошибки и восстановление](#ошибки-и-восстановление).

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
и registry. Native MCP discovery и teardown исполняются
в одной задаче `WorkerResourceHost`; background-владелец создаётся при первом
реальном фоновом эффекте. Потеря процесса не разрешает скрытый reconnect/repeat.
File/model контексты без живых ресурсов восстанавливаются из pinned profile.

## HTTP backend

В `config_server.yml` включите backend явно:

```yaml
server:
  host: 127.0.0.1
  port: 8000
  auth_token: ${ENV:GC_REMOTE_TOKEN}
  execution_backend: temporal-native
  temporal_config: /absolute/path/native-worker.json
  temporal_profile: production
  temporal_store: files
```

Сервер не исполняет модель и инструменты. Обычные `/events/webhook`,
`/runs/{system}/{task_id}` и `/logs/{system}/{task_id}` используют Temporal
adapter; webhook требует стабильный `message_id`. Отключение API процесса не
снимает workflow. Default `execution_backend: local` сохраняет прежнее поведение
сервера.

HTTP API, под существующей Bearer-авторизацией сервера:

| Метод и суффикс `/temporal/{system}/{thread}` | Назначение |
|---|---|
| `POST /messages` | `{"message_id":"m1","text":"..."}`: start или следующий ввод |
| `GET /status` | Phase, ответ, pending human, active child, pending operation |
| `GET /history?after=0&limit=100&token=...` | Сообщения и meta из ThreadStore |
| `GET /events?after=0&limit=200` | Preview/commit events с cursor |
| `GET /stream` | SSE; resume через `Last-Event-ID` или `after` |
| `POST /answer` | `request_id`, `reply_id`, `answer` |
| `POST /stop` | `{"turn_id":"..."}`: остановка текущего хода |
| `POST /close` | Дождаться принятой работы и закрыть сессию |
| `PATCH /ui` | Только `title`, `pinned`, `archived` |
| `GET /metrics` | То же, что `status` |

Прямой SDK: `gc_ua_temporalio.native_client.NativeAgentClient` — методы `start`, `status`,
`submit_message`, `answer_human`, `stop_turn`, `stop`, `close_session`, `result`,
`submit_with_start`. Он получает уже
подключённый `temporalio.client.Client`, поэтому встраивающее приложение может
само настроить TLS/auth соединения.

### Native HTTP backend

`native-worker.json` — тот же `NativeWorkerConfig`, который загружает native
worker. `temporal_store` должен совпадать с `store_id` выбранного pinned profile.
API-процессу нужен доступ к выбранному File/SQLite ThreadStore и технической
receipts DB; модель и инструменты исполняет worker. Помещайте receipts DB вне
workspace агента (например, `/var/lib/universal-agent/native/receipts.sqlite`),
чтобы служебные записи не попадали в результаты файловых инструментов.

`history` читает настоящие сообщения и метаданные ThreadStore. Поле `token`
проверяет уже прочитанный префикс; при компактировании/замене истории сервер
возвращает `reset: true`, и клиенту нужно перечитать историю целиком. UI title/pinned/archived записываются в `meta.tui` того же store.
`events` содержит только необязательные previews с operation/attempt/cursor;
canonical результат остаётся в ThreadStore. Preview включается явно через `profiles.<name>.preview_stream: true` в native
worker config. Это подключает штатный stream callback; `tui_stream`/backend stream
policy по-прежнему может его отключить. По умолчанию callback отсутствует,
поэтому транспорт остаётся тем же, что у локального CLI/server.

Native workflow ID имеет отдельное пространство имён. Чужой workflow owner
не принимается как native-сессия. Сервер стартует native-сессии с `keep_alive`,
поэтому отключение HTTP-клиента не завершает исполнение. `/close` закрывает
сессию после принятой работы, `/stop` проверяет актуальный `turn_id`.
Сообщение отправляется через Update-With-Start: после `/close` или сбоя тот же
тред продолжается новым run.

### Ошибки и восстановление

Семантика как у Pydantic AI и как в local: ход продолжается, если модель может
отреагировать. Доменная ошибка activity (`ValueError`, `OSError`, изменившиеся
факты guard'а, потеря MCP-сессии) возвращается модели обычной ошибкой
инструмента. Запрос к модели и read-only операции Temporal повторяет. Внешний
эффект (запись, команда, MCP, project memory) исполняется не более одного раза:
повтор после падения worker'а только читает квитанцию первой попытки, а без неё
модель получает «исход неизвестен, проверьте состояние». Операторских команд нет.

Упавшая или закрытая сессия продолжается новым сообщением с тем же workflow ID:
Update-With-Start открывает новый run, и владелец треда — сама сессия — совпадает.

### Крупные payload и Continue-As-New

Вход запроса к модели несёт всю переписку. Чтобы история workflow не росла
квадратично, включите external storage Temporal SDK:

```json
"payload_storage": {"kind": "file", "path": "/absolute/state/payloads", "threshold_bytes": 32768}
```

Payload больше порога хранятся в каталоге (или `{"kind": "s3", "bucket": "..."}`
с пакетом `aioboto3`), в истории остаётся sha256-ссылка. Хранилище нужно
worker'у, серверу и клиенту (`gc-temporal-native --config`). Temporal blob'ы не
удаляет: настройте lifecycle S3 или периодически вызывайте
`gc_ua_temporalio.payload_storage.prune_file_payloads(path, max_age_seconds=...)`
со сроком больше retention namespace.

После каждого хода workflow делает Continue-As-New: новый run получает только
состояние доставки (последние 1000 ID сообщений и ответов), диалог остаётся в
ThreadStore.

### Когда писать тред

`profiles.<name>.core.thread_store.persist`: `turn` (по умолчанию в Temporal) —
шаги хода копятся в памяти workflow и пишутся в конце хода, перед компакцией,
перед ожиданием человека и при stop/ошибке; `step` — после каждого шага, как
в local. Локальный запуск допускает только `step`.

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
