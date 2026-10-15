[Назад к SDK](README.md)

# Хранилище тредов

Тред — это вся история разговора с агентом: сообщения (вопросы, ответы модели,
вызовы инструментов и их результаты), meta треда (счётчики токенов, журнал
намерений, память сессии), трассы субагентов и картинки. Всё это лежит в
**хранилище тредов**. По умолчанию — файлы на диске; своё хранилище (база, S3)
передаётся агенту объектом. Через конфиг оно не выбирается.

Хранилище тредов отвечает за то, что относится к одному треду. Промпты,
AGENTS.md, скиллы и субагенты — общее для всех тредов агента — даёт другое
хранилище: [хранилище агента](agent-store.md).

## Какие есть

| Хранилище | Где | Когда |
|---|---|---|
| `FileThreadStore` | `gc_core.thread_store_file` | по умолчанию в SDK, CLI, TUI, сервере: файлы `data_*.jsonl` и `meta_*.json` в `paths.threads_data_path` |
| `MemoryThreadStore` | `gc_testkit.memory_thread_store` | тесты; образец своего хранилища |
| `DurableFileThreadStore` | `gc_core.thread_store_file_durable` | Temporal: те же файлы плюс служебный каталог `.store/` |
| `SQLiteThreadStore` | `gc_core_temporalio.storage.sqlite` | Temporal: один файл базы |
| `PostgresThreadStore` | `gc_stores.postgres` (дистрибутив `gc-stores[postgres]`) | несколько инстансов на одной базе: контракт v2 (`gc_core.threads.store.ThreadStore`), см. «Postgres» ниже |
| своё | ваш код | база, S3, Redis — по контракту ниже |

## Как передать агенту

SDK — параметром `thread_store`:

```python
from gc_core.aio import AsyncAgent

agent = await AsyncAgent.create(settings, thread_store=MyStore())
answer = await agent.run("привет", thread_id="t1")              # system="sdk"
answer = await agent.run("привет", thread_id="t1", system="crm")
```

Адрес треда — пара `(system, thread_id)`: `system` разделяет треды разных
приложений в одном хранилище (у SDK по умолчанию `"sdk"`, у CLI — `"cli"`).
Без `thread_id` история не сохраняется: каждый ход — с чистого листа.

Temporal — в плагин воркера, одно хранилище или несколько по именам:

```python
from gc_core_temporalio import AgentPlugin, AgentSession

plugin = AgentPlugin({"support": settings}, thread_store=store)
plugin = AgentPlugin({"support": settings}, thread_stores={"s3": s3_store})

session = AgentSession(client, agent="support", thread_id="T-1",
                       task_queue="agents", store_id="s3")
```

Под Temporal хранилище обязано выполнять durable-контракт (ниже): запись в
тред идёт из activity, а activity после падения воркера повторяется.

## Своё хранилище для SDK

Наследуйте `ThreadStoreBase` (`gc_core.thread_store_base`): одиннадцать
абстрактных методов — чтение истории и meta, запись, трассы субагентов, сброс.
Забытый метод — отказ при создании объекта, а общие правила meta (штампы
версии схемы, флаги рантайма) достаются от базы готовыми.

```python
from gc_core.thread_store_base import ThreadStoreBase


class PostgresThreadStore(ThreadStoreBase):
    def __init__(self, dsn):
        self.db = connect(dsn)

    def load_messages(self, system_id, thread_id): ...
    def load_thread_state(self, system_id, thread_id): ...
    def load_meta(self, system_id, thread_id): ...
    def append_messages(self, system_id, thread_id, messages, *,
                        known_keys=None, start_seq=0): ...
    def save_meta(self, system_id, thread_id, meta): ...
    # …и ещё шесть: patch_runtime_flag, update_runtime_meta,
    # discard_message, reset, append_subagent_trace, read_subagent_trace
```

Клиент базы асинхронный (`asyncpg`, `redis.asyncio`) — `AsyncThreadStoreBase`:
те же методы с приставкой `a`, ядро ждёт их нативно.

Проверка — конформанс-набор, одной строкой в тестах:

```python
from gc_testkit.thread_store_conformance import assert_conforms


def test_my_store():
    assert_conforms(lambda: PostgresThreadStore(TEST_DSN))
```

Что именно делает каждый метод, восемь свойств, которые легко нарушить, и
поведение синхронного сетевого клиента — в
[контракте хранилища тредов](thread-store-contract.md). Образцы:
`FileThreadStore` и `MemoryThreadStore`.

## Своё хранилище для Temporal

К обычному контракту добавляются два метода:

- `read_snapshot(system_id, thread_id, *, include_state=True)` — снимок треда:
  meta, сообщения, ревизия, поколение, владелец;
- `commit_operation(system_id, thread_id, operation)` — атомарно применить
  операцию записи и вернуть квитанцию.

У каждой операции есть ID. Хранилище запоминает её результат (**квитанцию**):
если воркер упал после записи, но до ответа Temporal, activity повторится с
тем же ID, и хранилище вернёт исходную квитанцию, а не запишет сообщения второй
раз. Ещё хранилище проверяет ревизию (чужая запись между чтением и записью —
`ThreadStoreConflict`, ядро перечитает тред и повторит) и владельца треда (одна
сессия пишет тред, другие получают отказ).

Обязательно объявите `protected_paths()` — пути данных хранилища на диске
воркера, к которым агенту нельзя прикасаться, или `[]`, если на диске ничего
нет. Без него воркер не стартует.

Квитанции нужны только на время повторов activity. Метод
`prune_receipts(*, max_age_seconds)` даёт обслуживанию воркера удалять старые
(старше 7 дней); без него квитанции копятся.

Проверка:

```python
from gc_testkit.durable_thread_store_conformance import assert_durable_conforms


def test_my_durable_store():
    assert_durable_conforms(lambda: S3ThreadStore(bucket, prefix))
```

## Postgres: несколько инстансов на одной базе

`PostgresThreadStore` из дистрибутива `gc-stores` — хранилище контракта v2
(`gc_core.threads.store.ThreadStore`) для нескольких инстансов агента на одной
базе. Драйвер — extra `postgres` (asyncpg):

```bash
uv pip install "gc-stores[postgres] @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/stores"
```

```python
from gc_core.aio import AsyncAgent
from gc_stores.postgres import PostgresThreadStore

store = PostgresThreadStore("postgresql://agent@db/agents")
await store.ensure_schema()      # таблицы, если их нет; повторно — безвредно
agent = await AsyncAgent.create(settings, thread_store=store)
...
await store.close()              # или `async with PostgresThreadStore(...)`
```

- **Таблицы** создаёт только `ensure_schema()` (SQL — `gc_stores/postgres/schema.sql`);
  два инстанса, стартующие одновременно, не мешают друг другу. Уже созданные
  таблицы пользователю без права `CREATE` не мешают: DDL не выполняется.
- **Схема базы** — параметр `schema` (по умолчанию `public`): несколько
  независимых наборов тредов в одной базе.
- **Пул** — свой (создаётся при первом вызове, параметры — `pool_options` →
  `asyncpg.create_pool`) или ваш: `PostgresThreadStore(pool=pool)`; чужой пул
  `close()` не закрывает. Пул asyncpg привязан к event loop: после закрытого
  loop (`asyncio.run(store.ensure_schema())` на старте) хранилище открывает
  новый пул в следующем, из другого работающего loop — понятный отказ.
  Свой пул ставит сессиям `idle_in_transaction_session_timeout = 60s`
  (`pool_options={"server_settings": …}` переопределяет): замёрзший посреди
  транзакции инстанс не держит строку треда бессрочно.
- **Конкурентность.** Каждая запись — одна транзакция READ COMMITTED (явно,
  независимо от `default_transaction_isolation` сервера): строка треда
  блокируется (`SELECT … FOR UPDATE`), затем проверки контракта (квитанция →
  аренда → ревизия). Две записи по одной ревизии с разных инстансов — одна
  проходит, вторая получает `ThreadConflict`; повтор той же операции (тот же
  `op_id`), пока первая ещё пишет, — та же квитанция без дубля.
- **Время** — часы базы (`clock_timestamp()` после блокировки строки):
  истечение аренды не зависит от часов инстансов, `updated_at` треда не идёт
  назад.
- **Данные** — текст JSON в колонках `text` (не `jsonb` и не `json`):
  история читается ровно той, что записана — порядок ключей, `\u0000` в
  выводе тулов, `NaN`/`Infinity`, как у файлового хранилища. JSON-операторов
  в запросах нет; для разбора в SQL — `data::json`.
- **Удалённые треды** оставляют строку в `gc_tombstones` (поколение и эпоха
  аренды): тред, созданный заново под тем же ключом, не пропустит запись по
  аренде удалённого.
- **`watch`** — опрос (`poll_interval`); LISTEN/NOTIFY — позже.

## Необязательные возможности

Хранилище объявляет их методами; нет метода — ядро работает по-старому.

- **Файлы треда** — `write_thread_file(system_id, thread_id, name, data)` и
  `read_thread_file(system_id, thread_id, name)`. Картинки (снимки
  `fs_view_image`, вложения сообщений) и медиа из ответов MCP ложатся в
  хранилище, а не на диск процесса; модель видит их ссылкой
  `thread-file://<system>/<thread>/<имя>`. Удаляет их само хранилище вместе с
  тредом (`reset`). Под Temporal хранилище без файлов треда и без каталога на
  диске выключает `fs_view_image`: картинкам негде жить, кроме диска воркера.
- **Архив перед компакцией** — `archive_and_replace(system_id, thread_id,
  messages)`: старая история уходит в архив одной операцией, когда сжатие
  контекста заменяет её сводкой.
- **Замена истории одной операцией** — `replace_messages`: компакция без пары
  `reset` + `append_messages`, трасса субагентов не трогается.

## Где что лежит

| Файл | Что |
|---|---|
| `gc_core/thread_store_base.py` | `ThreadStoreBase`, `AsyncThreadStoreBase` — контракт |
| `gc_core/thread_store_durable.py` | снимок, операция, квитанция, ошибки durable-контракта |
| `gc_core/thread_store_file.py` | `FileThreadStore` |
| `gc_core/thread_store_file_durable.py` | `DurableFileThreadStore` |
| `gc_core/thread_files.py` | файлы треда, ссылки `thread-file://` |
| `gc_core_temporalio/storage/sqlite.py` | `SQLiteThreadStore` |
| `gc_stores/postgres/store.py`, `schema.sql` | `PostgresThreadStore` и его таблицы |
| `gc_testkit/memory_thread_store.py` | `MemoryThreadStore` |
| `gc_testkit/thread_store_conformance.py`, `durable_thread_store_conformance.py` | проверки своей реализации |
