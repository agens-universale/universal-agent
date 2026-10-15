[Назад к SDK](README.md) · [Контракт для авторов хранилищ](thread-store-contract.md)

# Хранилище тредов

Тред — вся история разговора с агентом: сообщения (вопросы, ответы модели,
вызовы инструментов и их результаты), состояние сессии (рабочая память, журнал
намерений, расход токенов), трассы субагентов, журнал правок и вложения
(картинки). Всё это лежит в **хранилище тредов**. По умолчанию — файлы на
диске; своё хранилище (база, S3) передаётся агенту объектом. Через конфиг оно
не выбирается — в YAML есть только `paths.threads_data_path` для файлового.

Хранилище тредов отвечает за то, что относится к одному треду. Промпты,
AGENTS.md, скиллы, субагенты и память проекта — общее для всех тредов агента —
даёт другое хранилище: [хранилище агента](agent-store.md).

## Какие есть

Все реализуют контракт `gc_core.threads.store.ThreadStore` («v2»).

| Хранилище | Где | Когда |
|---|---|---|
| `FileThreadStore` | `gc_core.threads.file_store` | по умолчанию в SDK, CLI, TUI, сервере, gc-chat, gc-acp: файлы `data_*.jsonl` и `meta_*.json` в `paths.threads_data_path` (формат прежний) плюс служебный каталог `.store/`; под Temporal — `durability="fsync"` |
| `SQLiteThreadStore` | `gc_stores.sqlite` (дистрибутив `gc-stores`) | один файл базы; файлы треда (картинки) — в ней же |
| `PostgresThreadStore` | `gc_stores.postgres` (`gc-stores[postgres]`) | несколько инстансов на одной базе — [ниже](#postgres-несколько-инстансов-на-одной-базе) |
| `MemoryThreadStore` | `gc_testkit.memory_thread_store_v2` | тесты; самый короткий образец своего хранилища |
| своё | ваш код | база, S3, Redis — по [контракту](thread-store-contract.md) |

Хранилище прежнего контракта (`ThreadStoreBase` — одиннадцать методов
`load_messages`, `save_meta`…) по-прежнему принимается: ядро заворачивает его
в адаптер. Новое пишите на v2 — см. [контракт](thread-store-contract.md#прежний-контракт-threadstorebase).

## Как передать агенту

SDK — параметром `thread_store`:

```python
from gc_core.aio import AsyncAgent

agent = await AsyncAgent.create(settings, thread_store=MyStore())
answer = await agent.run("привет", thread_id="t1")              # system="sdk"
answer = await agent.run("привет", thread_id="t1", system="crm")
```

Адрес треда — `ThreadKey(system, thread)`: `system` разделяет треды разных
приложений в одном хранилище (у SDK по умолчанию `"sdk"`, у CLI — `"cli"`),
`thread` — только латинские буквы, цифры, `_` и `-`. Читайте тред тем же
`system`, каким писали. Без `thread_id` история не сохраняется: каждый ход —
с чистого листа.

Temporal — в плагин воркера, одно хранилище или несколько по именам:

```python
from gc_core_temporalio import AgentPlugin, AgentSession

plugin = AgentPlugin({"support": settings}, thread_store=store)
plugin = AgentPlugin({"support": settings}, thread_stores={"pg": pg_store})

session = AgentSession(client, agent="support", thread_id="T-1",
                       task_queue="agents", store_id="pg")
```

## Как устроен тред

Хранилище отдаёт тред одной записью (`ThreadRecord`) и потоками:

| Часть | Что внутри |
|---|---|
| `info` | заголовок, workspace, режим API, `created_at` / `updated_at` / `messages_count` (их ставит хранилище) |
| `state` | состояние сессии: рабочая память, заметки, todo, журнал намерений, счётчики, кэш `fetch_url` |
| `usage` | расход токенов по категориям вызовов |
| `runtime` | живой статус хода: `turn`, флаги (`summarizing`, `finalizing`…), объём живой истории, снимок MCP |
| `frontend` | состояние фронтендов по имени (`"tui"`, `"acp"`) — его знает только фронтенд |
| `archives` | архивы истории, ушедшей при сжатии контекста |
| `extra` | ключи meta, которых модели не знают (данные старых версий, сторонние секции) — хранятся как есть |
| потоки | `messages` (история), `subagent_events`, `edits`, `inbox`, потоки фронтендов `<фронтенд>.<имя>` (`chat.responses`) |

Запись в историю — `commit`: атомарно, по ревизии треда и по аренде
пишущего, повтор той же операции (тот же `op_id`) возвращает прежнюю
квитанцию. Статус хода, состояние фронтендов и журналы рядом с историей —
`append_side`, с ходом они не конфликтуют. Аренда (`claim`) — кто сейчас
ведёт тред: ход SDK/CLI/сервера — со сроком (по умолчанию 60 с, продлевается),
сессия Temporal — бессрочно. Алиасы — внешние идентификаторы треда
(`response_id` gc-chat), по ним тред находится с любого инстанса.

Файловое хранилище раскладывает всё это в прежние ключи `meta_*.json`:
старые треды открываются, формат файлов не изменился.

## Читать треды из приложения: фасад `Threads`

Фронтенды и приложения работают с тредами через фасад, а не через сырые
методы хранилища:

```python
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey

threads = Threads(store)                     # или Threads.from_settings(settings)
key = ThreadKey(system="sdk", thread="t1")

async for record in threads.iter_threads(system="sdk"):
    print(record.key.thread, record.info.title, record.info.updated_at)
history = await threads.history(key)         # записи потока messages
parts = await threads.parts(key)             # архивы по порядку, затем живая история
await threads.update_frontend(key, "myapp", {"pinned": True})
found = await threads.resolve("resp_123")    # тред по алиасу
await threads.delete(key)
```

`Threads.legacy_meta(record)` отдаёт запись в прежней форме meta (ключи
`shared`, `runtime`, `tui`…) — для кода, который читал `meta_*.json`.

## Своё хранилище

Наследуйте `gc_core.threads.store.ThreadStore` и реализуйте его методы — все
асинхронные: чтение (`get_thread`, `list_threads`, `read_stream`,
`read_archive`, `resolve_alias`), запись (`commit`, `append_side`), аренда и
жизненный цикл (`claim`, `release`, `delete`), вложения и обслуживание
(`put_blob`, `get_blob`, `prune_receipts`). Порядок проверок, правила аренды,
нумерации потоков и времени — в [контракте](thread-store-contract.md);
правила, общие для всех реализаций, — готовыми функциями в
`gc_core.threads.rules`.

Образцы: `MemoryThreadStore` (`gc_testkit.memory_thread_store_v2`, ~400 строк
— всё явно и без оптимизаций), `SQLiteThreadStore` и `PostgresThreadStore`
(`gc_stores`).

Проверка — конформанс-набор, одной строкой в тестах:

```python
from gc_testkit.thread_store_conformance_v2 import assert_conforms


def test_my_store():
    assert_conforms(lambda clock: MyStore(dsn, clock=clock))
```

## Своё хранилище для Temporal

Под Temporal работает любое хранилище контракта v2: код workflow говорит с
ним через activities `universal_agent.thread.*`, ядро пишет тред так же, как
локально. Идемпотентность записи — часть контракта: повтор `commit` после
записи (воркер упал до ответа) хранилище узнаёт по `op_id`. Владелец треда —
цепочка run сессии (бессрочная аренда).

Обязательно объявите `protected_paths()` — пути данных хранилища на диске
воркера, к которым агенту нельзя прикасаться, или `[]`, если на диске ничего
нет. Без него воркер не стартует. Квитанции старше 7 дней обслуживание воркера
удаляет `prune_receipts(older_than)`. Подробно — [Temporal](../temporal.md#хранилища-тредов).

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

## Картинки и файлы треда

Картинки сообщений и снимки `fs_view_image` у файлового хранилища лежат в
`<threads_dir>/images/`, у SQLite — в самой базе (файлы треда,
`write_thread_file` / `read_thread_file`), модель видит их ссылкой
`thread-file://<system>/<thread>/<имя>`. У хранилища без каталога и без
файлов треда (Postgres) картинки ложатся во временный каталог процесса и
после перезапуска недоступны; под Temporal такое хранилище выключает
`fs_view_image`. Подробно — в [контракте](thread-store-contract.md#картинки-и-файлы-треда).

## Где что лежит

| Файл | Что |
|---|---|
| `gc_core/threads/store.py`, `models.py`, `errors.py` | `ThreadStore` — контракт v2, модели, отказы |
| `gc_core/threads/rules.py` | правила, общие для реализаций (аренда, список, запись сбоку) |
| `gc_core/threads/facade.py` | `Threads` — фасад для фронтендов и приложений |
| `gc_core/threads/session_port.py` | `SessionPort` — сессия агента поверх хранилища v2 |
| `gc_core/threads/file_store.py` | `FileThreadStore` |
| `gc_stores/sqlite/`, `gc_stores/postgres/` | `SQLiteThreadStore`, `PostgresThreadStore` и их схемы |
| `gc_testkit/memory_thread_store_v2.py` | `MemoryThreadStore` |
| `gc_testkit/thread_store_conformance_v2.py` | проверка своей реализации |
| `gc_core/thread_store_base.py`, `thread_store_async.py` | прежний контракт и адаптер к нему |
