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
`${FILE:...}` раскрываются при загрузке строго: незаданная переменная (или
нечитаемый файл) — отказ старта воркера со списком всех таких ссылок (файл +
ключ). Необязательное значение пометьте умолчанием: `${ENV:VAR:-умолчание}`
(пустое — `${ENV:VAR:-}`).

FileThreadStore выбран явно и остаётся default для истории. Для SQLite замените
store на `{"kind":"sqlite","path":"/absolute/state/threads.sqlite"}`.
`receipts_database` — техническая SQLite worker'а: результаты завершённых
activities и preview. Она не заменяет выбранный ThreadStore.

Без `thread_stores` и `payload_storage` воркер кладёт треды и payload рядом с
`receipts_database` (каталоги `threads` и `payloads`), а не в workspace агента.
Все служебные пути (вместе с `-wal`/`-shm` баз, в форме `abspath` и `realpath`)
попадают в `security.protected_paths.extra` профиля: агент не читает и не правит
историю сессий, квитанции и payload. Файлы баз создаются с правами 0600,
каталоги payload — 0700.

Раз в 10 минут воркер обслуживает себя: освобождает контекст хода, чей workflow
уже не исполняется (terminate посреди хода), вместе с его MCP-процессами;
удаляет квитанции и preview workflow, которых Temporal уже не знает (истёк
retention namespace); удаляет квитанции хранилища тредов старше 7 дней.

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
  --profile production --store files --system cli --thread example \
  --resource-queue ua-native-resource-local --model-queue ua-native-model \
  --prompt 'Прочитай README рабочей папки и кратко опиши проект'
```

Клиент печатает workflow ID и не читает worker JSON: profile/store и
очереди должны совпадать с конфигурацией worker. Для `status WORKFLOW_ID`,
`result WORKFLOW_ID`, `stop WORKFLOW_ID` используется тот же префикс команды.
Старт с `--keep-alive` сохраняет сессию для `send WORKFLOW_ID --message-id UNIQUE_ID
--text TEXT`; `close WORKFLOW_ID` закрывает её после принятой работы.
`answer WORKFLOW_ID --request-id REQUEST_ID --answer TEXT` отвечает человеку.
`unlock --config native-worker.json --system SYSTEM --thread THREAD` снимает
владение тредом у сессии, которой больше нет (terminate, падение процесса):
после этого тред снова открывается локально в TUI/CLI. Живую сессию команда
не трогает.
Structured output включается `start --structured-output-format /absolute/format.txt`.
О поведении при сбоях — [ошибки и восстановление](#ошибки-и-восстановление).

### Модель, профили и живые ресурсы

Поддержаны общий Chat Completions и Responses. Для Responses задайте
`core.backend.api_mode: "responses"` и соответствующий endpoint в
`core.backend.completions_url`, например `/v1/responses`. Это тот же модельный
порт ядра; Responses cache-miss восстанавливает контекст общим механизмом.
`preview_stream: true` — явный opt-in потокового preview, требующий совместимого
streaming-провайдера; пример оставляет JSON Chat Completions без preview.

Workflow знает профиль только по имени (как pydantic-ai — модель по ключу
реестра): каждая activity берёт текущий профиль процесса воркера. Чтобы сменить
настройки или токен, перезапустите воркер с новым JSON — живые сессии
продолжат работу, следующие шаги пойдут уже с новыми настройками. Имя профиля и
`store_id` должны оставаться прежними: переименование профиля — это новый
профиль, и сессии старого имени получат ошибку. Если правка меняет
последовательность шагов внутри хода, ход, начатый на старом коде, может не
пройти replay — на время разработки такие истории чистятся.
Credentials не входят в публичный prepared context workflow.

MCP задаётся обычным `core.mcp`, субагенты — штатными настройками `core.agent`
и registry. Native MCP discovery и teardown исполняются
в одной задаче `WorkerResourceHost`; background-владелец создаётся при первом
реальном фоновом эффекте. Потеря процесса не разрешает скрытый reconnect/repeat.
File/model контексты без живых ресурсов восстанавливаются из профиля воркера.

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
worker. `temporal_store` должен совпадать с `store_id` выбранного профиля.
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

Чтения (`fs_read_file` и другие fs-чтения, `fetch_url`, `fs_view_image`, чтение
памяти проекта) после сбоя воркера повторяются, а не дают «исход неизвестен».
Лимит activity инструмента — его `timeout` (для `exec_command` без него —
`agent.exec_timeout_sec`) плюс 5 минут, не меньше 15 минут.

Текст сообщения — до 1 000 000 символов. Дедупликация по `message_id` держит
последние 1000 ID в пределах сессии; после закрытия сессии повтор того же ID
открывает новый run и обрабатывается снова — отправителю с доставкой
«хотя бы раз» нужна своя дедупликация. Сообщение, отброшенное остановкой хода
(`undelivered`), при повторе с тем же ID принимается заново.

История Temporal хранит промпты и вывод инструментов открытым текстом (крупные
payload — в `payload_storage`): шифрования (`PayloadCodec`) нет, доступ к
Temporal UI и namespace — как к самим данным агента.

Упавшая или закрытая сессия продолжается новым сообщением с тем же workflow ID:
Update-With-Start открывает новый run, и владелец треда — сама сессия — совпадает.
Закрытая, упавшая или отменённая сессия снимает владение тредом сама. Если
владелец не смог этого сделать (terminate, падение процесса), следующая сессия с
любым ID проверяет его workflow: не RUNNING или не найден — владение снимается
и тред захватывается; живой владелец по-прежнему не уступает.

Run становится FAILED только от доменного отказа: ошибки ядра
(`ExecutionFailure`) или исхода activity/child workflow. Недетерминизм после
деплоя, запрет песочницы и баг в коде workflow роняют лишь workflow task: run
остаётся RUNNING и продолжает ход на исправленном воркере. Если сбой случился
внутри кода, который успел запланировать уборку (`finally` с activity), replay
исправленного кода может разойтись с историей — тогда помогает `temporal
workflow reset` на событие до хода.

### Деплой новых версий: Worker Versioning

Внутри workflow работает весь цикл агента, поэтому правка кода, меняющая шаги
хода, ломает replay хода, начатого на старом коде. Штатный механизм Temporal —
Worker Versioning; включается секцией `versioning`:

```json
"versioning": {"enabled": true, "deployment_name": "ua-native"}
```

Номер сборки по умолчанию — версия пакета `gc-ua-temporalio` (`build_id` задаёт
явно). Новый код — новый номер, иначе Temporal сочтёт его той же сборкой.

- Ход закреплён за сборкой, на которой начался, и доигрывается ею (PINNED),
  включая ожидание человека.
- После хода Continue-As-New переводит сессию на текущую сборку: следующий ход
  идёт на новом коде. Простаивающая сессия переходит сразу после объявления
  новой сборки текущей.
- Деплой: поднять воркер новой сборки, объявить её текущей
  (`temporal worker deployment set-current-version --deployment-name ua-native
  --build-id <версия>`), погасить старый воркер, когда его версия опустела
  (`temporal worker deployment describe-version`). Для одного воркера и
  локальной разработки — `"set_current_on_start": true`.

По умолчанию versioning выключен: тогда несовместимый деплой оставляет ход,
шедший в момент релиза, ждать исправленного воркера (workflow task
повторяется), а на время разработки такие истории чистятся.

### Таймауты и повторы activities

Как `ActivityConfig` у Pydantic AI: секция `activities` конфига воркера задаёт
поля Temporal (`start_to_close`, `schedule_to_close`, `heartbeat_timeout`,
`retry`: `initial_interval`, `backoff_coefficient`, `maximum_interval`,
`maximum_attempts` (`0` — без ограничения), `non_retryable_error_types`).
Длительности — ISO 8601 (`"PT10M"`) или секунды. Уровни накладываются:
умолчания → `default` → уровень → инструмент.

```json
"activities": {
  "default": {"retry": {"maximum_interval": "PT30S"}},
  "model": {"schedule_to_close": "PT10M"},
  "resource": {"retry": {"maximum_attempts": 5}},
  "thread_store": {"start_to_close": "PT2M"},
  "tools": {"exec_command": {"start_to_close": "PT2H"}}
}
```

| Уровень | Что покрывает | По умолчанию |
|---|---|---|
| `model` | `agent.model.request` | 15 мин на попытку, heartbeat 30 с; повторы без ограничения, пауза 1 с → ×2 → до 1 мин |
| `resource` | подготовка, guards, промпты, чтения, память, субагенты, инструменты | 15 мин, heartbeat 30 с, до 5 попыток |
| `thread_store` | `agent.thread.*` | 2 мин, до 5 попыток |
| `tools.<имя>` | один инструмент поверх `resource` | лимит — `timeout` инструмента + 5 мин, от 15 мин до суток |

**Сбой провайдера модели** — обрыв соединения, 5xx, 408/425/429 — повторяет
Temporal по политике `model`: ход ждёт провайдера и продолжается с того же
места, workflow остаётся RUNNING. Отказ по существу (400, 401/403/404,
переполнение контекста, отказ из-за картинок) не повторяется: ход завершается
ошибкой. Запрос субагента не повторяется Temporal — его fallback на модель
родителя решается в workflow. Чтобы получить отказ вместо долгого ожидания,
задайте `model.schedule_to_close` или `model.retry.maximum_attempts`.

Фреймворк всегда дописывает неповторяемые типы — доменные отказы и превышение
размера payload. Эффект инструмента исполняется не более одного раза при любой
политике: вторая попытка только читает квитанцию первой. Конфиг проверяется на
старте воркера; смена действует со следующего хода.

Повтор всего workflow (`retry_policy`, `execution_timeout` в
`NativeAgentClient.start`) по умолчанию не задан и не рекомендуется: ход
начнётся заново с тем же входом, и уже выполненные эффекты повторятся.

### Крупные payload и Continue-As-New

Вход запроса к модели несёт всю переписку, а вывод тула может весить мегабайты.
Temporal ограничивает payload 2 МБ, а историю — 50 МБ, поэтому external storage
Temporal SDK включён по умолчанию: каталог `payloads` рядом с
`receipts_database`. Задать явно:

```json
"payload_storage": {"kind": "file", "path": "/absolute/state/payloads", "threshold_bytes": 32768}
```

Payload больше порога хранятся в каталоге (или `{"kind": "s3", "bucket": "..."}`
с пакетом `aioboto3`), в истории остаётся sha256-ссылка. `"payload_storage": null`
выключает хранилище — тогда большой файл или длинный ход упрётся в лимиты
Temporal. Хранилище нужно worker'у, серверу и клиенту (`gc-temporal-native
--config` с тем же JSON). Temporal blob'ы не удаляет: для `kind: file` задайте
`"prune_after_days": N` — воркер сам удаляет blob'ы старше срока; для S3 —
lifecycle-правило. Срок — больше retention namespace и самого долгого ожидания
человека внутри хода. Каждая новая ссылка на blob и его чтение обновляют mtime,
а простаивающая сессия раз в сутки делает Continue-As-New и обновляет свои
blob'ы, поэтому очистка по возрасту не удаляет blob'ы живых сессий.

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
