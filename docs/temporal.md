# Temporal: агент в durable-исполнении

Пакет `gc-core-temporalio` исполняет обычного агента ядра (`AsyncAgent`,
`AgentExecutor`) внутри [Temporal](https://temporal.io): ход переживает падение
и перезапуск процесса, запрос к модели повторяется при сбое провайдера,
ожидание ответа человека может длиться сколько угодно, а история каждого шага
видна в Temporal UI. Агент тот же — те же настройки, инструменты, MCP,
субагенты, суммаризация; Temporal меняет только то, **где** исполняются модель,
инструменты и хранилище.

Temporal — необязательный режим. TUI, `gc-cli`, `gc-server`, ACP и chat
работают локально и к Temporal не обращаются.

## Как это выглядит

Три части:

- **Temporal Server** — хранит историю и раздаёт задачи (`temporal server
  start-dev` для разработки).
- **Воркер** — процесс, который исполняет ходы агента: нативные `Client` и
  `Worker` из `temporalio` с плагином `AgentPlugin`. Можно и без кода —
  командой `gc-temporal-worker --config worker.json`.
- **Приложение** — шлёт сообщения и получает ответы: тот же `AsyncAgent` с
  `durable=TemporalDurability(...)`, `AgentSession` или команда `gc-temporal`.

Одна сессия агента — один тред: workflow `UniversalAgentWorkflow` живёт между
сообщениями и после каждого хода начинает новый run (Continue-As-New), чтобы
история не росла.

## Быстрый старт

Установка из checkout репозитория — optional extra `temporal`:

```bash
uv sync --extra temporal
temporal server start-dev         # локальный сервер, UI — http://localhost:8233
```

Воркер (запускать **из рабочего каталога агента** — инструменты работают
относительно cwd, воркер не делает `chdir`):

```python
import asyncio
from datetime import timedelta

from gc_core.config import load_settings
from gc_core.threads.file_store import FileThreadStore
from gc_core_temporalio import AgentPlugin
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig


async def main():
    plugin = AgentPlugin(
        agents={
            "shop": load_settings("shop.yaml")
        },  # те же настройки, что у AsyncAgent
        thread_store=FileThreadStore("/var/ua/threads", durability="fsync"),
        model_activity=ActivityConfig(
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_interval=timedelta(minutes=1)),
        ),
    )
    client = await Client.connect("localhost:7233", plugins=[plugin])
    worker = Worker(
        client, task_queue="shop"
    )  # плагин добавит workflow и activities
    await worker.run()


asyncio.run(main())
```

Приложение — тот же `AsyncAgent`, ходы исполняет воркер:

```python
from gc_core.aio import AsyncAgent
from gc_core.config import load_settings
from gc_core_temporalio import TemporalDurability
from temporalio.client import Client

client = await Client.connect("localhost:7233")
agent = await AsyncAgent.create(
    load_settings("shop.yaml"),
    durable=TemporalDurability(client, task_queue="shop", agent="shop"),
)
async with agent:
    answer = await agent.run("Проверь заказ 42", thread_id="order-42")
    again = await agent.run("А доставка?", thread_id="order-42")  # тот же тред
```

## Воркер

### Из кода: `AgentPlugin`

`AgentPlugin` — плагин `temporalio` (`SimplePlugin`): подключается к `Client`
или к `Worker`, добавляет workflow `UniversalAgentWorkflow` и `UniversalSubagentWorkflow`,
activities агента и песочницу кода workflow. Всё, что относится к Temporal,
задаётся нативными объектами `temporalio`.

| Параметр | Что это |
|---|---|
| `agents` | Имя агента → его настройки: `LoadedSettings` (из `load_settings`, вместе с рантаймом), `AppConfig` или `AgentProfile`. Имя — то, что приложение передаёт как `agent=`; у живых сессий его не меняют |
| `thread_store` | Хранилище тредов контракта v2 **объектом**: `FileThreadStore` (`gc_core.threads.file_store`), `SQLiteThreadStore` (`gc_stores.sqlite`), `PostgresThreadStore` (`gc_stores.postgres`) или своё (см. [хранилища](#хранилища-тредов)) |
| `thread_stores` | Несколько хранилищ по именам (`store_id` у приложения) |
| `workspace` | Рабочий каталог агентов; по умолчанию cwd. Воркер должен быть запущен в нём |
| `activity`, `model_activity`, `resource_activity`, `thread_store_activity` | `temporalio.workflow.ActivityConfig` по уровням (см. [таймауты и повторы](#таймауты-и-повторы)) |
| `tool_activities` | `{имя инструмента: ActivityConfig}` |
| `worker_id` | Имя процесса для живых ресурсов; по умолчанию `хост-pid` |

Остальное — параметры самого `Worker` из `temporalio`: `task_queue`,
`identity`, `max_concurrent_activities`, `deployment_config` (версии, см.
[деплой](#деплой-новых-версий)). Песочницу кода workflow плагин ставит сам;
если у `Worker` свой `workflow_runner`, плагин его сохранит и предупредит, что
его запрет HTTP- и MCP-клиентов в коде workflow к нему не применён.
Параметры модели из настроек агентов проверяются при создании плагина, а не
на первом ходе.

### Без кода: `gc-temporal-worker --config`

Команда собирает те же объекты (`Client`, хранилища, `AgentPlugin`, `Worker`)
из JSON. Пример — [examples/worker.json](../packages/temporalio/examples/worker.json):

```json
{
  "address": "localhost:7233",
  "namespace": "default",
  "workflow_queue": "ua-workflow",
  "model_queue": "ua-model",
  "resource_queue": "ua-resource-local",
  "state_dir": "${ENV:UA_STATE}",
  "profiles": {
    "production": {
      "workspace": "${ENV:UA_WORKSPACE}",
      "core": {
        "backend": {
          "completions_url": "${ENV:UA_MODEL_URL}",
          "chat_completion_headers": {"Authorization": "Bearer ${ENV:UA_MODEL_API_TOKEN}"}
        },
        "agent": {"model": "${ENV:UA_MODEL_NAME}"}
      },
      "runtime": {"text_answer_mode": "allow"}
    }
  },
  "activities": {"model": {"schedule_to_close": "PT10M"}},
  "versioning": {"enabled": true, "deployment_name": "ua-agent"}
}
```

```bash
cd "$UA_WORKSPACE"
uv run --project /path/to/universal-agent --extra temporal \
  gc-temporal-worker --config /path/to/worker.json
```

| Поле | Что это | По умолчанию |
|---|---|---|
| `address`, `namespace` | Сервер Temporal | `localhost:7233`, `default` |
| `workflow_queue`, `model_queue`, `resource_queue` | Очереди: сессии, запросов к модели, остального. На каждую различную — свой `Worker` | `ua-workflow`, `ua-model`, `ua-resource-local` |
| `worker_id` | Имя процесса для живых ресурсов | `local` |
| `state_dir` | Каталог служебного состояния; без `thread_stores` треды — `<state_dir>/threads` | каталог файла конфига |
| `thread_stores.<имя>` | `{"kind": "file" \| "sqlite", "path": …}` | `files` в `state_dir` |
| `profiles.<имя>` | Агент: `workspace`, `store_id` (`files`), `core` — обычный `config.yml` целиком (`AppConfig`), `runtime` — рантайм агента | — |
| `activities` | Таймауты и повторы по уровням | см. ниже |
| `versioning` | Worker Versioning: `enabled`, `deployment_name`, `build_id`, `set_current_on_start` | выключен |

`${ENV:VAR}` и `${FILE:path}` раскрываются при загрузке **строго**: незаданная
переменная или нечитаемый файл — отказ старта со списком ссылок.
Необязательное значение — `${ENV:VAR:-умолчание}`. Лишний ключ — ошибка.
`uv run --project` сохраняет cwd вызывающего (не заменяйте на `--directory`).

### Логи и файлы на диске воркера

- **Лог прогона** под Temporal по умолчанию не пишется файлом: `save_logs:
  false` и `propagate: true` — записи идут в `logging` процесса воркера
  (stdout, сборщик логов) и в обработчики `AgentPlugin(log_handlers=[…])`.
  Различать прогоны — по `run_id` и `trace_id` (удобен `logging.format: jsonl`).
  Явное `logging.save_logs: true` в профиле возвращает файл. Раз лог уходит в
  общий сборщик, секреты в нём маскируются (`logging.redact_secrets.enabled:
  true` по умолчанию под Temporal); содержимое хода (промпты, выводы
  инструментов) пишется на уровне `debug` — уровень обработчиков решает,
  попадёт ли оно в сборщик.
- **Трасса сжатия контекста** — запись лога `summary_trace`, а не отдельный
  файл `summary_trace_*.jsonl`.
- **Картинки и медиа MCP** лежат в хранилище тредов — вложениями треда
  (`put_blob`, см. «Картинки и файлы треда» в docs/sdk/thread-store.md) или
  рядом с файловыми тредами; на диске воркера их нет, `fs_view_image` доступен
  при любом хранилище.
- **Памяти проекта** (`project_memory`) под Temporal нет при любом профиле: это
  файл в рабочей папке, общий для всех сессий проекта, — возможность TUI и CLI.
  Нет ни инструмента, ни статуса памяти в промпте, ни обращений к файлу.
  Долгоживущие факты сессии — `session_memory`, она в треде.

### Целый ход в одной activity

`AgentActivity` — дополнительный режим для приложений, которым нужен весь
ход SDK на одном процессе. Он не заменяет `AgentPlugin` и
`UniversalAgentWorkflow`: прежний пошаговый durable-запуск сохраняется.
В новом режиме модель, MCP, инструменты и обработка результата выполняются
внутри одной обычной activity `universal_agent.run`.

На воркере (профили и креды не передаются через историю Temporal):

```python
from datetime import timedelta
from gc_core_temporalio import AgentActivity
from temporalio.worker import Worker

async def publish_files(request, answer):
    # Выгрузить локальные файлы здесь, на том же процессе, где работал агент.
    # Вернуть JSON-данные: например, список ссылок на артефакты.
    return await upload_to_storage(request.context["folder"])

runner = AgentActivity(
    {"shop": settings},
    thread_store=store,  # общий PostgresThreadStore для нескольких реплик
    postprocess=publish_files,
)
async with Worker(
    client,
    task_queue="orders",
    workflows=[OrderWorkflow],
    activities=[runner.run],
    max_concurrent_activities=1,
    graceful_shutdown_timeout=timedelta(minutes=45),
):
    await stop_event.wait()
```

В бизнес-workflow импортируйте только контракт
`gc_core_temporalio.turn_activity_contracts` (он безопасен для песочницы):

```python
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from gc_core_temporalio.turn_activity_contracts import (
    AgentActivityRequest, AgentActivityResult,
)

result = await workflow.execute_activity(
    "universal_agent.run",
    AgentActivityRequest(
        agent="shop", prompt="Собери отчёт", thread_id="order-42",
        trace_id="trace-42", context={"folder": "artifacts/order-42"},
    ),
    result_type=AgentActivityResult,
    start_to_close_timeout=timedelta(minutes=45),
    schedule_to_close_timeout=timedelta(hours=3),
    heartbeat_timeout=timedelta(seconds=30),
    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    retry_policy=RetryPolicy(
        maximum_attempts=3, initial_interval=timedelta(minutes=1),
    ),
)
# result.answer — ответ SDK; result.output — результат publish_files.
```

Одна попытка остаётся на одном процессе, поэтому несколько реплик могут
слушать общую очередь. Heartbeat начинается до подготовки MCP и работает
до конца postprocess; по умолчанию интервал 10 секунд. Отмена доходит до
SDK, его контекст закрывается. Приложение останавливает Worker и затем
закрывает общее хранилище. При штатном обновлении дайте активной activity
время завершиться, согласовав таймаут остановки Worker и контейнера.

Ограничения этого режима:

- Повтор запускает **весь ход заново**, включая postprocess. Сохранённая
  история треда читается из хранилища, но это не replay вызовов инструментов.
  Уже выполненные внешние действия могут повториться. Пример явно выбирает
  три попытки; приложение вправе задать другую RetryPolicy.
- Таймаут/потеря heartbeat не доказывает, что старый процесс остановился.
  Защита треда остаётся в ThreadStore, а идемпотентность внешних действий —
  ответственность приложения и инструментов.
- Постоянный workspace общий для activities процесса. Для такого workspace
  начните с `max_concurrent_activities=1`; разные каталоги требуют отдельных
  процессов/изолированного OS-бэкенда. Этот API не меняет cwd.
- История в Postgres не переносит локальные файлы на другую реплику.
  Нужные следующему ходу файлы сохраняйте и восстанавливайте отдельно.
- Настройки — обычные настройки SDK; скрытых durable-переопределений нет.
  Отключите ненужную память проекта и файловые журналы в серверном профиле.
  Канал человека выключен; вопросы и подтверждения требуют отдельного
  прикладного протокола.

### Размещение пошагового режима

- **Один процесс — один рабочий каталог.** Cwd воркера равен `workspace`
  профилей; для другого каталога — другой процесс.
- **Живые ресурсы принадлежат процессу.** MCP-сессии и фоновые команды живут в
  памяти воркера, поэтому очередь ресурсов (при `AgentPlugin` — очередь
  сессии) должна быть своей у каждого процесса, если агент пользуется MCP или
  фоновыми командами. Несколько процессов на одной такой очереди — нельзя.
- **Запросы к модели можно вынести на отдельные воркеры**:
  `model_activity=ActivityConfig(task_queue="ua-model")` и ещё один `Worker`
  с тем же плагином на `ua-model` (в JSON — `model_queue`).
- **Хранилище тредов — вне рабочего каталога** агента: пути хранилищ
  попадают в `protected_paths`, агент не читает и не правит чужую историю.
- Раз в 10 минут воркер убирает за собой: освобождает контексты сессий, чей
  workflow уже не исполняется (terminate посреди хода), и старые служебные
  записи хранилища тредов.

## Приложение

### `AsyncAgent` с `durable=`

Тот же фасад, что локально; ходы исполняет воркер профиля `agent`.

| Метод | В Temporal |
|---|---|
| `run(prompt, thread_id=…, system=…, message_ids=…)` | Отправить сообщения в тред и дождаться ответа хода. Без `thread_id` — новый тред на каждый вызов (как локально без истории) |
| `stream(...)` | Потоковых дельт нет: ответ целиком — в `last_answer` |
| `send(message)` | Как локально: во время хода — в этот ход, и `run` дождётся хода, который его обработает (ответ — последнего); вне хода — ждёт следующего `run` и уходит вместе с ним, новый ход само не запускает. Сбой отправки поднимается следующим `run` |
| `stop()` | Кооперативная остановка идущего хода; вне хода ничего не делает (как локально). Присланное в остановленный ход не доигрывается — в лог `run_tail_dropped` |
| `aclose()` | Освобождает сторону приложения; сессия в Temporal живёт дальше |

Вложения (`attachments`) и параметры контекста (`auth_connector`,
`on_stream_delta`, `tools_cache`…) в этом режиме не принимаются: контекст
собирает воркер.

**Настройки агента читает воркер, а приложение их сверяет.** Ход идёт по
профилю воркера; `settings`, переданные в `AsyncAgent.create`, уходят в сессию
отпечатком по секциям (хэши, без значений и секретов), и воркер сравнивает его
со своим профилем на подготовке хода. Разошлись — ход не начинается:
`SessionFailed: Настройки приложения и профиля воркера 'shop' различаются в
секциях: agent…`. Отпечаток идёт и с каждым сообщением: приложение с другими
настройками пишет в идущую сессию — его сообщение отклоняется сразу
(`SettingsMismatch`), сессия работает дальше. Не сравниваются `paths`,
`logging`, `acp`, `pricing`, `tui` (кроме `agentsmd_override`), пути промптов
(`*_prompt_path`, абсолютные от каталога конфига) и рантайм. Смена настроек
или токена — перезапуск воркера с новыми настройками; приложению — те же.

### `AgentSession`

Сессия одного треда — для приложений, которым нужны ответ человека, статус или
свои ID сообщений:

```python
from gc_core_temporalio import AgentSession

session = AgentSession(
    client, agent="shop", thread_id="order-42", task_queue="shop"
)
answer = await session.ask("Проверь заказ")  # отправить и дождаться
message_id = await session.send("Уточнение: срочно")  # только отправить
reply = await session.wait(message_id)  # ответ хода, его номер, delivered
status = await session.status()  # фаза, вопрос человеку, ход
await session.answer_human(status["pending_human"]["request_id"], "да")
await session.stop()  # остановить ход
await session.close()  # закрыть сессию, снять владение тредом
```

- ID workflow сессии вычисляется из агента, хранилища, `system_id` и треда:
  один тред одного агента — одна сессия у любого клиента; упавшая или
  закрытая сессия продолжается новым сообщением с тем же ID
  (Update-With-Start). Другой агент на том же треде — другая сессия, и она
  получит явный отказ владения тредом.
- Повтор сообщения с тем же `message_id` — не второй ход (последние 1000 ID
  сессии). Сообщение, отброшенное остановкой хода, при повторе принимается
  заново. Текст — до 1 МБ в истории Temporal: не-ASCII символ там занимает 6
  байт, то есть около 166 тысяч символов кириллицы.
- `wait`/`ask` возвращают ответ **своего** хода — того, что обработал
  сообщение (сессия хранит итоги последних 8 ходов; для более старого —
  `answer_expired`). Ожидание — опрос query, а не долгий update: сколько
  угодно ожидающих клиентов не мешают ответу человека, `stop` и новым
  сообщениям (у сервера лимит одновременных update). Упавшая сессия —
  `SessionFailed` с исходной причиной; закрытая с необработанным сообщением —
  тоже `SessionFailed`; отброшенное остановкой хода у `ask` —
  `MessageNotProcessed`; неизвестный ID — `ValueError`. `wait(timeout=…)` —
  `TimeoutError` по истечении; задача workflow падает три раза подряд (сбой
  кода, недетерминизм) — `SessionStuck`: workflow жив и продолжит сам, когда
  сбой устранят.
- Ход, которому не хватило записи истории (ответ модели больше 2 МБ и т. п.),
  отказывает, а сессия — нет: у `wait` в ответе `turn_error` с размером,
  `ask` и durable `AsyncAgent.run` поднимают `TurnFailed` (это
  `ExecutionFailure`, как отказ хода в локальном режиме); следующее сообщение
  обрабатывается как обычно.
- Ответ последнего хода переживает Continue-As-New целиком до ~1 МБ в
  истории (ответ и форматированный ответ — каждый); больше — `ask` и durable
  `AsyncAgent.run` поднимают `AnswerTruncated` (`.answer` — начало ответа),
  полный — последним сообщением в треде. Это расхождение с локальным
  режимом: ход успешен, но ответ целиком через историю не передать. Ответы
  прошлых ходов укорачиваются до 256 КБ с пометкой `answer_truncated`; `wait`
  видит тот же вид до и после Continue-As-New, `status()` — флаг
  `answer_truncated`.
- `send_batch([(текст, ID), …])` — несколько сообщений в один ход.
- `AgentSession(..., settings=...)` включает ту же сверку настроек, что
  `AsyncAgent` с `durable=`.

Низкоуровневый клиент — `AgentClient` (`start`, `submit_with_start`,
`submit_message`, `answer_human`, `stop_turn`, `stop`, `close_session`,
`status`, `result`). Все клиенты принимают уже подключённый
`temporalio.client.Client`: TLS и авторизацию настраивает приложение.

### Командой: `gc-temporal`

```bash
gc-temporal --address localhost:7233 --workflow-queue ua-workflow start \
  --profile production --store files --system cli --thread example \
  --resource-queue ua-resource-local --model-queue ua-model \
  --prompt 'Прочитай README и опиши проект' [--keep-alive]
gc-temporal status ID | result ID | stop ID | close ID
gc-temporal send ID --message-id UNIQUE --text TEXT
gc-temporal answer ID --request-id REQUEST --answer TEXT
gc-temporal unlock --config worker.json --system SYSTEM --thread THREAD
```

`unlock` снимает владение тредом у сессии, которой больше нет (terminate,
падение процесса): тред снова открывается локально в TUI/CLI. Живую сессию не
трогает. `start --structured-output-format файл` включает структурированный
ответ.

### Агент шагом своего workflow

Свой бизнес-процесс в Temporal зовёт агента как обычный шаг — как
`await agent.run(...)` в workflow у Pydantic AI:

```python
from datetime import timedelta
from temporalio import workflow
from gc_core_temporalio import agent_step


@workflow.defn
class OrderWorkflow:
    @workflow.run
    async def run(self, order_id: str) -> str:
        await workflow.execute_activity(
            charge, order_id, start_to_close_timeout=timedelta(minutes=1)
        )
        step = await agent_step(
            "shop", f"Проверь заказ {order_id}", thread_id=f"order-{order_id}"
        )
        await workflow.execute_activity(
            send_email,
            step.answer,
            start_to_close_timeout=timedelta(minutes=1),
        )
        return step.answer
```

**Где настраивается агент.** В коде workflow — только его имя (`"shop"`).
Настройки — у воркера, который обслуживает очередь: `AgentPlugin` с этим
именем (модель, инструменты, `stop_tools`, хранилище, таймауты activities).
Тот же воркер исполняет и ваш workflow с его activities:

```python
settings = load_settings(profile_override="shop")  # или LoadedSettings из кода
plugin = AgentPlugin(
    {"shop": settings}, thread_store=SQLiteThreadStore("threads.sqlite")
)
worker = Worker(
    client,
    task_queue="orders",
    workflows=[OrderWorkflow],  # ваш workflow
    activities=[charge, send_email],  # ваши activities
    plugins=[plugin],  # агенты: UniversalAgentWorkflow и его activities
)
```

`agent_step("shop", …)` запускает дочерний `UniversalAgentWorkflow` с профилем
`"shop"` на очереди `task_queue` (по умолчанию — очередь вашего workflow);
воркер этой очереди находит профиль по имени. Сверки отпечатка настроек у
шага нет: источник настроек — профиль воркера.

**Запускать агента заранее не нужно.** Каждый шаг сам запускает новый
дочерний workflow на один ход и завершается вместе с ходом; тред с историей
живёт в хранилище, поэтому следующий шаг продолжает тот же разговор. Если в
этот момент не запущен воркер с `AgentPlugin`, ничего не теряется: задача
ждёт в очереди Temporal, ваш workflow ждёт результат шага, и как только
воркер поднимется, шаг выполнится — обычная durable-семантика Temporal.

- Шаг — один ход агента в дочернем `UniversalAgentWorkflow`: тот же агент, тред и
  гарантии (повторы модели, эффект — одна попытка).
  Несколько шагов одного треда идут подряд и видят общую историю.
- ID дочернего workflow предсказуем — `step_workflow_id(ID_родителя, шаг)`
  (шаг по умолчанию — ID треда). По нему приложение отвечает на вопрос агента
  и останавливает ход: `AgentSession(client, agent=…, thread_id=…,
  task_queue=…, workflow_id=step_workflow_id(...))`. Сессия с явным
  `workflow_id` подключается к идущему workflow: `send` его не запускает.
- Тред шага занят дочерним workflow: пока в том же треде идёт сессия или
  другой шаг, этот отказывает. Закрылся родитель (конец, terminate, таймаут) —
  ребёнку приходит отмена (`ParentClosePolicy.REQUEST_CANCEL` по умолчанию у
  `agent_step`), и он сам снимает владение тредом. С явным
  `parent_close_policy=TERMINATE` тред остаётся занятым до
  `gc-temporal unlock`. Параметры дочернего workflow (`parent_close_policy`,
  `execution_timeout`, `retry_policy`…) — именованными аргументами
  `agent_step`.
- Промпт больше 1 МБ в истории — `ApplicationError` типа
  `HistoryLimitExceeded` до запуска шага; ход не уложился в запись истории —
  `ApplicationError` типа `TurnFailed`.
- Внешний трейс шага (например, от оркестратора) — `agent_step(...,
  trace_id="…")` (или `RunRequest.trace_id`). Он уходит заголовком в каждый
  запрос к модели шага — при `agent.trace_header_enabled: true` — и к
  MCP-серверам с `trace_header_enabled: true`; субагенты шага шлют тот же
  трейс. Имя заголовка — `agent.trace_id_header_name` (по умолчанию
  `x_trace_id` для модели и `trace_id` для MCP), например `X-Trace-id`. У
  каждого шага свой трейс; без `trace_id` заголовка нет. Кривое значение
  (перевод строки, не latin-1, длиннее 200 символов) — `ApplicationError`
  типа `InvalidTraceId` до запуска шага.
- Воркер процесса несёт `AgentPlugin`. Сбой сессии шага — `ChildWorkflowError`
  в коде workflow. Код своего workflow не входит в `default_build_id()`: при
  versioning задавайте `build_id` с учётом своего кода.

## Настройки агента

- **Настройки — те же, что локально**: `core` в JSON или `LoadedSettings`/
  `AppConfig` в `AgentPlugin`. Модели, реестр `models`, MCP, субагенты, лимиты,
  guards, суммаризация — без отдельной схемы для Temporal.
- **Рантайм агента** — то, что локально задаёт поверхность (TUI, ACP, chat):
  `runtime.text_answer_mode` (`deny` — ход закрывается только `turn_finish`,
  `allow` — текстовый ответ тоже ответ), `is_tui` и др. В `AgentPlugin` — из
  `LoadedSettings.runtime`, в JSON — `profiles.<имя>.runtime`. `launch_root`
  всегда равен `workspace`.
- **Что меняет воркер**: `thread_store.persist` по умолчанию `turn` (шаги хода
  пишутся в тред в конце хода, перед компакцией, перед ожиданием человека и
  при stop/ошибке; `step` — после каждого шага), журнал правок агента
  (`agent_edits`) выключен, консольная трасса выключена.
- **Что попадает в историю Temporal**: снимок настроек без секретов и без
  адресов моделей — поля с пометкой `secret`/`worker_only`, `models.*.url`,
  токены и `os_backend.env` остаются на воркере. Словари параметров
  провайдера (`backend.chat_completion_kwargs`, `models.*.main`/`.auxiliary`,
  `completion_kwargs`) проходят схему известных параметров; ключ вне схемы —
  отказ на старте сессии (в тексте ошибки — путь, без значения).
- **Смена настроек или токена** — перезапуск воркера; живые сессии продолжают
  со следующего хода. Если правка меняет шаги хода, включите
  [versioning](#деплой-новых-версий).
- Поддержаны Chat Completions и Responses (`backend.api_mode`). Ответ модели
  воркер получает целиком, без потокового предпросмотра.

## Таймауты и повторы

Уровни накладываются: умолчания пакета → `default` (`activity=`) → уровень →
инструмент.

| Уровень | Что покрывает | По умолчанию |
|---|---|---|
| `model` | запрос к модели | 15 мин на попытку, heartbeat 30 с; повторы **без ограничения**, пауза 1 с → ×2 → до 1 мин |
| `resource` | подготовка хода, guards, промпты, чтения, память проекта, субагенты, инструменты | 15 мин, heartbeat 30 с, до 5 попыток |
| `thread_store` | чтение и запись треда | 2 мин, до 5 попыток |
| `tools.<имя>` | один инструмент поверх `resource` | лимит — `timeout` инструмента + 5 мин, от 15 мин до суток |

В коде — нативный `ActivityConfig`: `start_to_close_timeout`,
`schedule_to_close_timeout`, `heartbeat_timeout`, `retry_policy=RetryPolicy(...)`,
у модели ещё `task_queue`. В остальных уровнях (`activity=`, `resource_activity`,
`thread_store_activity`, `tool_activities`) `task_queue` — ошибка при создании
плагина: ресурсы и инструменты держат живые MCP-сессии процесса и идут в
очередь сессии. Прочие ключи `ActivityConfig` (`activity_id`, `summary`,
`priority`…) задаёт сам workflow — тоже ошибка. В JSON — те же поля без суффикса `_timeout`
(`start_to_close`, `schedule_to_close`, `heartbeat_timeout`, `retry`),
длительности — ISO 8601 (`"PT10M"`) или секунды:

```json
"activities": {
  "default": {"retry": {"maximum_interval": "PT30S"}},
  "model": {"schedule_to_close": "PT10M"},
  "tools": {"exec_command": {"start_to_close": "PT2H", "retry": {"maximum_attempts": 3}}}
}
```

У нативного `RetryPolicy` `maximum_attempts=0` (умолчание) — «не задано»: предел
уровня остаётся. Настроили только паузы в `activity=` — чтения треда и
ресурсы сохраняют свои 5 попыток, модель по-прежнему ждёт без ограничения, а
эффект инструмента — одна попытка (иначе неидемпотентная команда повторялась
бы бесконечно). Повторы — явным числом `maximum_attempts`; «без ограничения»
на другом уровне — `maximum_attempts: 0` в JSON-конфиге воркера. Если повторная попытка упирается в
изменившиеся факты guard, модель получает «исход неизвестен» (первая попытка
могла уже всё сделать).

**Запрос к модели** повторяется при сбое провайдера: обрыв и отказ
соединения, таймаут, 5xx, 408/425/429, ошибка в стриме или пустой ответ без
кода. Ход ждёт провайдера и продолжается с того же места. Не повторяются:
отказ по существу (400, 401/403/404), переполнение контекста, отказ из-за
картинок, код провайдера вроде `insufficient_quota`, незнакомый сбой без кода и
транспорт, который повтор не исправит (имя хоста, сертификат TLS, неверный
URL) — ход завершается ошибкой. `stop` во время повторов отменяет ожидающий
запрос. Чтобы получить отказ вместо долгого ожидания — `schedule_to_close` или
`maximum_attempts` у модели. Если воркер упал, когда ответ уже пришёл, запрос
повторится и будет оплачен второй раз.

**Эффект инструмента** (запись, команда, MCP-вызов, запись памяти проекта)
исполняется **одной попыткой**: после падения воркера или по лимиту времени
модель получает «исход неизвестен, проверьте состояние» и решает сама. Повторы
эффекта включаются только явно у инструмента (`tool_activities` /
`activities.tools.<имя>.retry.maximum_attempts`) — тогда после падения он может
выполниться второй раз. Чтения (`fs_read_file`, `fetch_url`, `fs_view_image`,
чтение памяти) повторяются.

Повтор всего workflow (`retry_policy`, `execution_timeout` в
`AgentClient.start`) не задан и не рекомендуется: ход начнётся заново, и уже
выполненные эффекты повторятся.

## MCP и фоновые команды

MCP задаётся обычной секцией `mcp` настроек. На каждом ходе воркер поднимает
серверы профиля и находит их инструменты; сессии живут в процессе воркера до
конца хода (`persistent_session` — одна сессия на ход, без него — сессия на
вызов, тоже на воркере). Вызов MCP-инструмента — одна activity с одной
попыткой; вывод больше 1 МБ обрезается с пометкой (модель листает его через
`read_stored_tool_output`). Субагент пользуется MCP-сессиями родителя.

Если воркер упал посреди хода, живые сессии потеряны: текущий вызов — «исход
неизвестен», следующие вызовы этого хода — ошибка инструмента
(`ResourceOwnerLost`); переподключений внутри хода нет. Следующий ход
поднимает серверы заново. Фоновые команды (`exec_command` с
`run_in_background`) — так же. Отсюда требование своей очереди ресурсов у
каждого процесса.

## Ответ человека и подтверждения

**По умолчанию человека нет** (`AgentPlugin(human_input=False)`, в JSON —
`"human_input": false`): агент работает автономно. Тогда:

- инструмента `turn_ask_human` у агента нет;
- режим `ask` охраны — `deny`: запись или чтение вне периметра, адрес вне
  политики, опасная по мнению судьи команда — отказ с причиной
  в выводе инструмента, модель ищет другой путь; безопасная команда
  выполняется. Явные `allow`/`deny` профиля не трогаются;
- если запрос к человеку всё же прозвучит (новый код, субагент), он сразу
  получает отказ — сессия не встаёт в ожидание ответа, которого не будет;
- воркер без `human_input` объявляет канал «человека нет»: вопросы режима
  разрешений (`manual`, пауза судьи в `auto`) и пола защищённых путей получают
  ответ «спросить некого», а не «не ответил вовремя». Запись в каталог `.git`,
  скиллы и настройки инструментов в таком воркере — отказ.

Режим разрешений под Temporal задаёт конфиг профиля или мета треда. API «одобрить
план» и «сменить режим» нет: у `AgentSession` и обновлений workflow таких операций
нет, поэтому `plan` без человека не выходит из `plan` (план остаётся в треде), а смена
режима — это смена профиля или меты. Режим сессии воркер читает из запроса инструмента,
а не из `default_mode` собственного конфига.

`human_input=True` включает вопросы и подтверждения, как описано ниже.

`turn_ask_human` и подтверждения guard'ов ставят ход на паузу без ограничения
по времени: `status()["pending_human"]` содержит вопрос и `request_id`, ответ —
`answer_human(request_id, answer)` (для подтверждения — `allow_once`,
`allow_remember`, `deny_once`, `deny_remember`). Перед ожиданием шаги хода
записываются в тред. Повтор ответа с тем же `reply_id` безопасен.

## Ошибки и восстановление

- **Доменная ошибка** (файл не найден, изменившиеся факты guard'а, потерянная
  MCP-сессия) — обычная ошибка инструмента для модели, ход продолжается.
- **Отказ хода** (ошибка ядра, исход activity или субагента) — run FAILED,
  сессия снимает владение тредом; следующее сообщение открывает новый run на
  том же треде.
- **Баг в коде workflow, недетерминизм после деплоя, запрет песочницы** —
  падает только задача workflow: run остаётся RUNNING и продолжит ход на
  исправленном воркере. Если исправленный код расходится с историей —
  `temporal workflow reset` на событие до хода.
- **Владение тредом**: тред одновременно ведёт одна сессия. Владелец, который
  не смог снять владение (terminate, падение), проверяется следующей сессией:
  не RUNNING — владение снимается; живой не уступает. Вручную — `gc-temporal
  unlock`.
- **История не шифруется** (`PayloadCodec` нет): промпты и вывод инструментов
  лежат в Temporal открытым текстом — доступ к namespace и UI как к данным
  агента. Свой кодек — `data_converter` у `Client.connect` воркера и клиента.

## Деплой новых версий

Внутри workflow работает весь цикл агента, поэтому правка кода, меняющая шаги
хода, ломает replay хода, начатого на старом коде. Решение Temporal — Worker
Versioning:

- ход закреплён за сборкой, на которой начался (PINNED), включая ожидание
  человека;
- после хода Continue-As-New переводит сессию на текущую сборку; простаивающая
  сессия переходит сразу после объявления новой сборки текущей.

В коде — нативный `deployment_config` воркера:

```python
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import WorkerDeploymentConfig
from gc_core_temporalio import default_build_id

Worker(
    client,
    task_queue="shop",
    deployment_config=WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(
            deployment_name="shop", build_id=default_build_id()
        ),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    ),
)
```

`default_build_id()` — версия пакета и хэш исходников `gc_core` и
`gc_core_temporalio` (`0.5.0+a1b2c3d4e5f6`): правка кода без подъёма версии —
тоже новая сборка. В JSON — секция `versioning` (`build_id` задаёт номер
явно, `set_current_on_start` — объявить сборку текущей на старте; для одного
воркера и разработки). Деплой: поднять воркер новой сборки, объявить её
текущей (`temporal worker deployment set-current-version --deployment-name …
--build-id …`), погасить старый, когда его версия опустела
(`temporal worker deployment describe-version`).

Без versioning воркер из JSON предупреждает на старте: ход, шедший в момент
несовместимого релиза, ждёт исправленного воркера.

## Хранилища тредов

Диалог агента живёт в хранилище тредов, история Temporal — только шаги ходов
(удаляется по retention namespace). Своей базы у воркера нет. Хранилище —
любое контракта v2 (`gc_core.threads.store.ThreadStore`, то же, что у
`AsyncAgent` локально): код workflow говорит с ним через activities
`universal_agent.thread.*`, ядро пишет тред так же, как локально.

- `FileThreadStore` (`gc_core.threads.file_store`) — файлы треда в прежнем
  формате плюс служебный каталог `.store/`; для воркера —
  `durability="fsync"` (так его создаёт `gc-temporal-worker` из JSON).
- `SQLiteThreadStore` (`gc_stores.sqlite`) — один файл базы; картинки и медиа
  треда — в той же базе.
- `PostgresThreadStore` (`gc_stores.postgres`, extra `gc-stores[postgres]`) —
  несколько воркеров на одной базе.
- **Своё** (S3, Redis…) — реализация контракта v2, проверка —
  `gc_testkit.thread_store_conformance_v2.assert_conforms`. Своё хранилище
  **обязано** объявить метод `protected_paths()` — пути своих данных на диске
  воркера (агент не сможет их читать и править) или `[]`, если на диске ничего
  нет; без него воркер не стартует.

**Владелец треда** — цепочка run сессии: в начале сессии workflow берёт
аренду `claim(LeaseRequest(owner="<workflow id>#<first_execution_run_id>",
ttl=None))`, бессрочно; она переживает Continue-As-New, её получают
субагенты, снимается она при закрытии или сбое сессии. Новый запуск с тем же
workflow ID (после terminate) — другой владелец: новая эпоха отсекает запоздавшие
записи прежней цепочки. Тред с такой арендой не пишет локальный TUI/CLI
(статус хода, потоки и алиасы сбоку — тоже: их пишет только владелец по своей
аренде, `SideChange.lease`; состояние фронтенда — любой) и не удаляет
уборщик. Сессию, которая упала или снята, не дождавшись уборки, следующая
сессия того же треда сменяет сама (цепочка старого владельца уже не RUNNING);
снять вручную — `gc-temporal unlock`. Аренду со сроком (локальный TUI/CLI)
сессия не отбирает — та истечёт сама.

**Повтор записи.** Запись — `commit` контракта с `op_id` из детерминированного
генератора workflow: повтор activity после записи (воркер упал до ответа)
хранилище узнаёт по `op_id` и отвечает прежней квитанцией, история не
задваивается.

## Хранилище агента

Промпты, AGENTS.md, скиллы и определения субагентов по умолчанию читаются
с диска воркера по путям конфига (`FileAgentStore`). Своё хранилище агента
(база, S3) передаётся в плагин: `AgentPlugin(..., agent_store=store)` — одно на
всех агентов, или словарь «агент → хранилище»; хранилище тредов и хранилище
агента вместе — `AgentPlugin(..., stores=Stores(threads=…, agent=…))` или
`stores=gc_stores.postgres.open_stores(dsn, agent_id=…)`
([хранилища на базах](sdk/stores.md)). Читается оно в activity
воркера; код workflow ресурсов агента не касается. Скрипты скиллов своего
хранилища перед запуском выкладываются в среду исполнения инструмента
(`os_backend` воркера), как локально. Подробно — [sdk/agent-store.md](sdk/agent-store.md).

## Размер истории

Temporal ограничивает запись истории 2 МБ, историю одного run — 50 МБ. Все
пределы пакета считаются в байтах истории: JSON-конвертер Temporal пишет
не-ASCII символ как `\uXXXX`, поэтому кириллица там втрое тяжелее, чем в UTF-8.
Пакет укладывается так:

- Continue-As-New после каждого хода: run — один ход.
- Тред читается страницами по 400 КБ.
- Activity модели получает целиком только новые сообщения шага; уже
  переданные (и ответы модели) — ссылкой на содержимое. Воркер держит их в
  памяти; если он их потерял (перезапуск, другой процесс), workflow записывает
  шаги хода в тред и повторяет запрос, где всё записанное снова идёт
  ссылками, — тред любого размера не уходит одной записью.
- При `persist: turn` шаги хода пишутся в тред частями по 400 КБ.
- Части записи (состояние сессии, расход, карточка, статус хода со снимком
  MCP, прочие ключи meta) идут разницей к записи треда, которую workflow уже
  видел; длинные тексты (запрос пользователя в журнале намерений) — ссылкой
  на сообщение этой же записи. Воркер собирает части из той же записи — из
  кэша или из хранилища, если отпечаток совпал. Не собрал (повтор после
  записи на другом воркере, чужая запись) — запись повторяется с частями
  целиком, и хранилище узнаёт повтор по `op_id` или отвечает конфликтом. Запись треда, которую workflow уже
  видел, приходит разницей к ней (после своей записи — признаком «не
  изменилась»), большой ответ — кусками по 400 КБ. Кэш `fetch_url`
  локального режима при этом не стирается и в историю не идёт. Хранилище
  получает полное состояние, журнал не урезается — тред, который вёл
  локальный режим, продолжается под Temporal и обратно без потерь.
- Служебный запрос к модели (сводка при лимите шагов) больше записи истории
  укорачивается посередине с пометкой — ход не отказывает.
- Трасса сжатия контекста (отладочный журнал) уходит частями меньше 256 КБ.
- Субагент получает облегчённое состояние родителя (без переписки и полных
  выводов инструментов).
- Вывод инструмента через границу activity — до 1 МБ; текст сообщения — до
  1 МБ; кэш страниц `fetch_url` — в памяти воркера (до 64 МБ), не в истории.
- Если вход или результат activity (ответ модели) либо вход субагента всё же
  больше лимита записи — отказ хода с размером в причине (`TurnFailed`), а не
  вечный сбой задачи workflow и не падение сессии. Состояние, переживающее
  Continue-As-New, и результат workflow укладываются в запись.

**Длинный ход — забота настроек агента**, как у Pydantic AI и Deep Agents:
посреди хода пакет Continue-As-New не делает. Шаг ложится в историю примерно
как вывод инструмента плюс ответ модели (вход модели — ссылками), то есть ход
из 300 шагов по 50 КБ вывода — около 40 МБ (кириллица — втрое быстрее), а
на 50 МБ сервер прерывает run. Держите `agent.max_steps` под задачу (по
умолчанию 1000 — это для локального режима), ограничивайте вывод
(`agent.tool_output_inline_max_chars`, `agent.exec_output_capture_max_chars`),
для очень длинных ходов — `thread_store.persist: step` (шаги не потеряются при
прерывании). Когда сервер посреди хода советует новый run, воркер пишет
предупреждение в лог с размером истории.

## Ограничения

- Один процесс воркера — один рабочий каталог; живые ресурсы (MCP, фоновые
  команды) не переживают процесс воркера и требуют своей очереди у процесса.
- `os_backend` проверен для `local` и `harbor`; `docker`, `ssh`, `exec` и
  `sandbox` под воркером не прогонялись (воркер предупреждает на старте).
  Параметры подключения (`ssh`, `exec`, `docker`) в историю не попадают.
- Запись хода частями не атомарна: если run прервать между частями, в треде
  останется начало хода (для Chat Completions следующий ход это переживёт,
  для Responses — нет).
- Кэши воркера (ссылки на сообщения, снимки тредов) ограничены числом
  сессий, а не байтами; обслуживание плагина — одно на процесс (воркеры в
  разных namespace с одним плагином не поддерживаются).
- Потоковых дельт ответа нет; вложения (картинки) в сообщениях через
  `AsyncAgent`/`AgentSession` не передаются, ответ человека — строка.
- Файлы картинок (`fs_view_image`) лежат на диске воркера: несколько
  воркеров на разных хостах требуют общего тома для каталога картинок.
- Журнал правок агента (дифф, откат в TUI) не ведётся.
- Одновременно активен один субагент.
- Субагент (дочерний workflow) получает режим разрешений копией общего состояния на
  старте: смену режима посреди его работы он не видит ни в проверках, ни в объявлении
  режима, они согласованы.
- Нет API «одобрить план» и «сменить режим» (см. «Ответ человека и подтверждения»).
- Агент — профиль настроек ядра: инструментов-функций на Python, как у
  Pydantic AI, нет.

## Как это устроено

Один `AgentExecutor`, один `ThreadSession`, один агентский цикл — тот же, что
локально. Цикл исполняется в коде workflow, а всё, что трогает внешний мир,
ядро делает через порты контекста (`ctx.operations.model`, `ctx.tool_effect`,
`ctx.guard_probe`, `ctx.thread_store`…). Локально порты исполняют работу на
месте, в Temporal каждый — activity на воркере:

| Activity | Что делает |
|---|---|
| `universal_agent.context.prepare` | собрать настоящий контекст профиля на воркере (MCP, инструменты, промпты); отдать workflow публичную часть |
| `universal_agent.model.request` | запрос к модели |
| `universal_agent.tool.execute` | эффект инструмента (одна попытка) |
| `universal_agent.guard.probe` | факты для guard'ов (файлы, пути) |
| `universal_agent.thread.get` / `read_stream` / `read_archive` / `resolve_alias` | чтение треда (запись, потоки, архивы, алиасы) |
| `universal_agent.thread.commit` / `append_side` / `claim` / `release` / `delete` | запись треда и аренда (контракт `ThreadStore` v2) |
| `universal_agent.state.prepare`, `universal_agent.prompts.prepare`, `universal_agent.prompt.read`, `universal_agent.skills.list` | подготовка хода |
| `universal_agent.memory.execute` / `hash` | память проекта |
| `universal_agent.subagent.prepare` | контекст субагента (дочерний workflow `UniversalSubagentWorkflow`) |
| `universal_agent.summary.trace`, `universal_agent.context.close` / `release` | трасса суммаризации, уборка хода |

Подробности по файлам — docstring модулей пакета
[`gc_core_temporalio`](../packages/temporalio/src/gc_core_temporalio).

**Защита от ввода-вывода мимо Temporal.** Код ядра без порта берёт его через
`gc_core.execution.ports.port(ctx, "<имя>")`: в durable-исполнении
отсутствие порта — отказ хода, а не локальная работа в коде workflow.
Песочница воркера запрещает создавать HTTP- и MCP-клиенты в коде workflow.
`test_port_contract.py` требует решения для Temporal по каждому
необязательному атрибуту контекста ядра; `test_parity_live.py` гоняет
эталонные сценарии ядра локально и под Temporal и сравнивает трассы. Живые
тесты требуют `temporal` CLI; `REQUIRE_TEMPORAL=1` делает его отсутствие
ошибкой (см. [тестирование](testing.md#temporal)).
