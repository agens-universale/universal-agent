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

Одна сессия агента — один тред: workflow `AgentWorkflow` живёт между
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
from gc_core.thread_store_file_durable import DurableFileThreadStore
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
        thread_store=DurableFileThreadStore("/var/ua/threads"),
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
или к `Worker`, добавляет workflow `AgentWorkflow` и `SubagentWorkflow`,
activities агента и песочницу кода workflow. Всё, что относится к Temporal,
задаётся нативными объектами `temporalio`.

| Параметр | Что это |
|---|---|
| `agents` | Имя агента → его настройки: `LoadedSettings` (из `load_settings`, вместе с рантаймом), `AppConfig` или `AgentProfile`. Имя — то, что приложение передаёт как `agent=`; у живых сессий его не меняют |
| `thread_store` | Хранилище тредов **объектом**: `DurableFileThreadStore`, `SQLiteThreadStore` (`gc_core_temporalio.storage.sqlite`) или своё (см. [хранилища](#хранилища-тредов)) |
| `thread_stores` | Несколько хранилищ по именам (`store_id` у приложения) |
| `workspace` | Рабочий каталог агентов; по умолчанию cwd. Воркер должен быть запущен в нём |
| `activity`, `model_activity`, `resource_activity`, `thread_store_activity` | `temporalio.workflow.ActivityConfig` по уровням (см. [таймауты и повторы](#таймауты-и-повторы)) |
| `tool_activities` | `{имя инструмента: ActivityConfig}` |
| `worker_id` | Имя процесса для живых ресурсов; по умолчанию `хост-pid` |

Остальное — параметры самого `Worker` из `temporalio`: `task_queue`,
`identity`, `max_concurrent_activities`, `deployment_config` (версии, см.
[деплой](#деплой-новых-версий)).

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

### Размещение

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
| `send(message)` | Сообщение в идущий ход (или в следующий) |
| `stop()` | Кооперативная остановка идущего хода |
| `aclose()` | Освобождает сторону приложения; сессия в Temporal живёт дальше |

Вложения (`attachments`) и параметры контекста (`auth_connector`,
`on_stream_delta`, `tools_cache`…) в этом режиме не принимаются: контекст
собирает воркер.

**Настройки агента читает воркер.** `settings`, переданные в
`AsyncAgent.create`, в durable-режиме не применяются — ход идёт по профилю
воркера. Держите их одинаковыми (один и тот же `shop.yaml`); смена настроек или
токена — перезапуск воркера, живые сессии продолжат работу со следующего хода.

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

- ID workflow сессии вычисляется из хранилища, `system_id` и треда: один тред —
  одна сессия у любого клиента; упавшая или закрытая сессия продолжается новым
  сообщением с тем же ID (Update-With-Start).
- Повтор сообщения с тем же `message_id` — не второй ход (последние 1000 ID
  сессии). Сообщение, отброшенное остановкой хода, при повторе принимается
  заново. Текст — до 1 МБ в UTF-8.
- `wait` переживает Continue-As-New: ход, обработавший сообщение, помнится в
  состоянии сессии.

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
у модели ещё `task_queue`. В JSON — те же поля без суффикса `_timeout`
(`start_to_close`, `schedule_to_close`, `heartbeat_timeout`, `retry`),
длительности — ISO 8601 (`"PT10M"`) или секунды:

```json
"activities": {
  "default": {"retry": {"maximum_interval": "PT30S"}},
  "model": {"schedule_to_close": "PT10M"},
  "tools": {"exec_command": {"start_to_close": "PT2H", "retry": {"maximum_attempts": 3}}}
}
```

Нативный `RetryPolicy` без `maximum_attempts` означает «без ограничения», как в
Temporal.

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
`gc_core_temporalio` (`0.4.0+a1b2c3d4e5f6`): правка кода без подъёма версии —
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
(удаляется по retention namespace). Своей базы у воркера нет.

- `DurableFileThreadStore` — файлы треда плюс служебный каталог `.store/`
  (квитанции операций, журнал прерванной записи, владелец).
- `SQLiteThreadStore` (`gc_core_temporalio.storage.sqlite`) — один файл базы,
  права 0600.
- **Своё** (S3, Postgres…) — реализация durable-контракта ядра: чтение снимка и
  идемпотентная запись операции по ID. Проверка — набор
  `gc_testkit.durable_thread_store_conformance`. Пути для защиты от агента
  хранилище объявляет методом `protected_paths()`.

## Размер истории

Temporal ограничивает запись истории 2 МБ, историю одного run — 50 МБ. Пакет
укладывается так:

- Continue-As-New после каждого хода: run — один ход.
- Тред читается страницами по 400 КБ.
- Activity модели получает целиком только новые сообщения шага; уже
  переданные — ссылкой на содержимое (воркер держит их в памяти, на промахе
  ищет в треде, иначе workflow повторяет запрос целиком).
- При `persist: turn` шаги хода пишутся в тред частями по 400 КБ.
- Вывод инструмента через границу activity — до 1 МБ.
- Кэш страниц `fetch_url` — в памяти воркера (до 64 МБ), не в истории.
- Текст сообщения — до 1 МБ.

Ход из 300 шагов по 50 КБ вывода даёт run около 41 МБ: для очень длинных ходов с
мегабайтами вывода нужен свой `DataConverter` с external storage Temporal SDK.

## Ограничения

- Один процесс воркера — один рабочий каталог; живые ресурсы (MCP, фоновые
  команды) не переживают процесс воркера и требуют своей очереди у процесса.
- `os_backend` проверен для `local` и `harbor`; `docker`, `ssh`, `exec` и
  `sandbox` под воркером не прогонялись.
- Потоковых дельт ответа нет; вложения (картинки) в сообщениях через
  `AsyncAgent`/`AgentSession` не передаются, ответ человека — строка.
- Файлы картинок (`fs_view_image`) лежат на диске воркера: несколько
  воркеров на разных хостах требуют общего тома для каталога картинок.
- Журнал правок агента (дифф, откат в TUI) не ведётся.
- Одновременно активен один субагент.
- Агент — профиль настроек ядра: инструментов-функций на Python, как у
  Pydantic AI, нет; свой workflow с агентом шагом — через дочерний
  `AgentWorkflow`.

## Как это устроено

Один `AgentExecutor`, один `ThreadSession`, один агентский цикл — тот же, что
локально. Цикл исполняется в коде workflow, а всё, что трогает внешний мир,
ядро делает через порты контекста (`ctx.operations.model`, `ctx.tool_effect`,
`ctx.guard_probe`, `ctx.thread_store`…). Локально порты исполняют работу на
месте, в Temporal каждый — activity на воркере:

| Activity | Что делает |
|---|---|
| `agent.context.prepare` | собрать настоящий контекст профиля на воркере (MCP, инструменты, промпты); отдать workflow публичную часть |
| `agent.model.request` | запрос к модели |
| `agent.tool.execute` | эффект инструмента (одна попытка) |
| `agent.guard.probe` | факты для guard'ов (файлы, пути) |
| `agent.thread.load` / `commit` / `read_trace` | чтение и запись треда |
| `agent.state.prepare`, `agent.prompts.prepare`, `agent.prompt.read`, `agent.skills.list` | подготовка хода |
| `agent.memory.execute` / `hash` | память проекта |
| `agent.subagent.prepare` | контекст субагента (дочерний workflow `SubagentWorkflow`) |
| `agent.summary.trace`, `agent.context.close` / `release` | трасса суммаризации, уборка хода |

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
