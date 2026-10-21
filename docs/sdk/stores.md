[Назад к SDK](README.md) · [Хранилище тредов](thread-store.md) · [Хранилище агента](agent-store.md)

# Хранилища на базах: Postgres, MySQL, SQLite, Redis, S3

У агента два хранилища, и они независимы:

- **хранилище тредов** (`ThreadStore`) — история разговоров, состояние сессий,
  трассы субагентов, вложения;
- **хранилище агента** (`AgentStore`) — промпты, AGENTS.md, скиллы, субагенты,
  память проекта.

По умолчанию оба — файлы. Дистрибутив `gc-stores` даёт оба контракта на пяти
базах; сочетания любые: треды в Postgres, а промпты в файлах образа, или всё в
одной базе.

| Хранилище | Треды | Агент | Extra | Когда брать |
|---|---|---|---|---|
| Postgres | `PostgresThreadStore` | `PostgresAgentStore` | `[postgres]` (asyncpg) | несколько инстансов на одной базе — основной вариант |
| MySQL 8 | `MySQLThreadStore` | `MySQLAgentStore` | `[mysql]` (asyncmy) | то же, если у вас MySQL |
| SQLite | `SQLiteThreadStore` | `SQLiteAgentStore` | — (stdlib) | один процесс или процессы одной машины (воркер Temporal) |
| Redis | `RedisThreadStore` | `RedisAgentStore` | `[redis]` | быстрое хранилище для простых задач — см. [ограничения](#redis) |
| S3 | `S3ThreadStore` | `S3AgentStore` | `[s3]` (aiobotocore) | только S3 с условной записью — см. [ограничения](#s3) |

```bash
uv pip install "gc-stores[postgres] @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/stores"
# все драйверы — gc-stores[all]
```

Из конфига — секция `storage` в config.yml
([configuration.md](../configuration.md#хранилище-storage)); её открывают
`gc-serve` и `gc-cli`, а в своём коде — `aopen_configured`:

```python
from gc_stores.configured import aopen_configured

stores = await aopen_configured(settings)   # по storage: настроек
agent = await AsyncAgent.create(settings, stores=stores)
...
await stores.aclose()
```

Схема при этом сверяется сразу (`ensure_schema: true` — накатывается), а
неизвестный параметр, нет `agent_id` или драйвера — `StorageConfigError` с
подсказкой. Утилита берёт подключение из того же файла:
`gc-stores migrate --config config.yml`, `gc-stores sync ./my-agent --config config.yml`.

## Подключить

Оба хранилища на одном подключении — `open_stores` нужной базы:

```python
from gc_core.aio import AsyncAgent
from gc_stores import postgres

stores = postgres.open_stores("postgresql://agent@db/agents", agent_id="coder")
await stores.threads.ensure_schema()   # миграции схемы (одна на оба)
agent = await AsyncAgent.create(settings, stores=stores)
...
await stores.aclose()
```

Сочетание — связкой `gc_core.stores.Stores`:

```python
from gc_core.agent_store import FileAgentStore
from gc_core.stores import Stores
from gc_stores.postgres import PostgresThreadStore

stores = Stores(
    threads=PostgresThreadStore("postgresql://agent@db/agents"),
    agent=FileAgentStore(settings),       # промпты и скиллы — с диска
)
agent = await AsyncAgent.create(settings, stores=stores)
```

Под Temporal — `AgentPlugin(agents, stores=stores)`. Одно хранилище агента на
всех агентов воркера — один `agent_id`; разным агентам — словарь
`agent_store={"coder": …, "reviewer": …}` и `thread_store=`.

`stores=` нельзя совмещать с `thread_store=`/`agent_store=` — `ValueError`.

## Схема и миграции

Схема SQL-баз — пронумерованные миграции (`gc_stores/<база>/migrations/`),
версия — в таблице `gc_schema_version`. Перед первой операцией хранилище
сверяет её с кодом:

- схемы нет — «выполните `await store.ensure_schema()` или `gc-stores migrate`»;
- ниже кода (gc-stores обновлён) — «выполните `gc-stores migrate`»;
- выше кода — «база от более новой gc-stores».

`ensure_schema()` применяет недостающие миграции под блокировкой (два
инстанса не применяют одну дважды); всё на месте — DDL не выполняется, так что
пользователю приложения без права `CREATE` хватает того, что накатил
администратор:

```bash
gc-stores migrate --store postgres --dsn postgresql://admin@db/agents [--schema s]
gc-stores migrate --check --store mysql --dsn mysql://admin:pw@db:3306/agents
```

SQLite применяет миграции сам при открытии файла. Redis и S3 хранят номер
формата ключей (`_gc/schema`): пишут его сами при первом обращении,
`gc-stores migrate --store redis|s3` — то же явно.

Адрес базы — флагом или переменной окружения (секрет не в истории
оболочки): `GC_STORES_DSN`, `GC_STORES_SQLITE_PATH`, `GC_STORES_REDIS_URL`,
`GC_STORES_S3_BUCKET`, `GC_STORES_S3_ENDPOINT`; ключи S3 — стандартные
переменные AWS.

## Ресурсы агента в базе: `gc-stores sync`

Хранилище агента в базе ядро только читает. Ресурсы туда кладёт администратор
из каталога в привычной раскладке:

```
my-agent/
  prompts/<имя>.md          # system.md, finish_gate.md, … — имена Prompt
  instructions/L0.md…L3.md  # L2, L3 — только с --project
  subagents/<имя>.md
  skills/<слой>/<скилл>/…   # system, user; project — только с --project
  roles/<роль>/L0.md        # роль поверхности сервера: L0@<роль>
  roles/<роль>/special_instructions.md   # special_instructions@<роль>
```

```bash
gc-stores sync ./my-agent --store postgres --dsn … --agent-id coder --with-defaults
gc-stores sync ./repo-agent --store postgres --dsn … --agent-id coder --project billing
```

- В хранилище в базе **всё** берётся из него: промпты, AGENTS.md всех слоёв,
  скиллы всех слоёв, субагенты, память. Промптов по умолчанию там нет, пока
  их не залить, и фронтенд (`gc-serve`, `gc-cli`) такое хранилище не
  открывает — отказ с подсказкой и списком недостающих промптов (без них ход
  упал бы посреди работы). Залить: `--with-defaults` кладёт промпты, роли поверхностей сервера
  по умолчанию (`webhook`, `chat`, `chat.completions`) и системные скиллы
  пакета gc-core с меткой `bundled@<версия>`. Файлы каталога их перекрывают.
- **Роль** — именованный вариант L0 и специнструкций внутри одного агента:
  `L0@<роль>`, `special_instructions@<роль>`. Её выбирает настройка
  `agent.role` (специнструкции — `agent.special_instructions_role`, по
  умолчанию та же роль); сервер ставит роль каждой поверхности. Варианта роли
  нет — ресурса нет: общий `L0` роль не подменяет. Файловое хранилище роль не
  использует — у него роль задаёт `paths.agent_md_path`.
- **После обновления gc-ua** промпты пакета меняются вместе с тулами —
  выполните `sync --with-defaults` снова. Хранилище, прочитав умолчание другой
  версии, один раз предупреждает об этом в лог.
- Неизменённое (sha256) не переписывается; `--prune` убирает загруженное
  человеком, чего в каталоге нет, — только в своей области (ресурсы агента
  или ресурсы проекта `--project`), умолчаний пакета не трогает; `--dry-run`
  — только отчёт.
- Ресурсы ключуются `agent_id` (ресурсы агента) и `agent_id` + id проекта
  (L2, L3, скиллы `project`, память). id проекта — настройка
  `agent.project_id`, без неё — slug пути каталога (у того же репозитория в
  контейнере или на другом хосте он другой). В общей базе `project_id`
  задаёт оператор (SDK, сервер), а не `.gc/config.yml` репозитория: иначе
  репозиторий может назваться чужим проектом и читать его память.
- Память проекта в базе — своя у каждого агента (у файлового хранилища она
  одна на проект: `.gc/project_memory.md`).
- **Что агент создаёт сам, в базу не попадает:** skill-creator и `/init`
  пишут файлы (`.gc/skills/…`, `AGENTS.md`), а читает агент из базы. Готовое
  загрузите `sync`.
- `<location>` скилла в системном промпте — путь в хранилище
  (`system/<скилл>/…`), а не путь на диске; скрипты скилла перед запуском
  выкладываются в `<launch_root>/.gc/skills_runtime/`.

## Postgres

Описание — в [хранилище тредов](thread-store.md#postgres-несколько-инстансов-на-одной-базе):
транзакция READ COMMITTED с блокировкой строки треда, часы базы, JSON текстом.

## MySQL

MySQL ≥ 8.0.19. Алгоритм — тот же, что у Postgres (общий SQL-движок).

- Транзакция — явно READ COMMITTED (по умолчанию у MySQL REPEATABLE READ);
  дедлок и ожидание блокировки — повтор транзакции.
- Ключи (id треда и системы, алиас, `op_id`) — до 255 символов, длиннее —
  `ValueError`; ключ ресурса агента — `agent_id` и id проекта до 128, путь до
  400 символов.
- Коллация `utf8mb4_0900_bin`: порядок по кодам символов и «a» ≠ «a ».
- Пакет клиента — 64 МБ; сервер режет по своему `max_allowed_packet` (по
  умолчанию у 8.x — 64 МБ): вывод тулов больше — поднимите его.
- DSN — `mysql://user:password@host:3306/database`.

## Redis

**Быстрое хранилище для простых задач.** Атомарность записи в тред
гарантирована: условная запись заголовка треда (`WATCH`/`MULTI`/`EXEC`).
Долговечность — нет: без AOF (`appendonly yes`, `appendfsync always`)
перезапуск Redis теряет последние ходы, а переключение реплики —
подтверждённые записи. Проверок при старте нет — выбирая Redis, вы
соглашаетесь с этим.

- Redis ≥ 6.2, одиночный или Sentinel; Cluster не поддерживается.
- `prefix` (по умолчанию `gc:`) — пространство ключей хранилища.
- Аренда считается часами машины: часы инстансов должны быть синхронны (NTP).

## S3

Только S3 с условной записью (`If-None-Match: *`, `If-Match`): AWS S3, MinIO.
Хранилище пробует её при первом обращении и без неё отказывается работать
(`ConditionalWritesUnsupported`) — гарантий тредов без неё нет.

- Цена: запись хода — несколько PUT (куски потока, заголовок, ключ индекса);
  список тредов — GET заголовка на каждую строку страницы.
- Прерванная запись оставляет невидимые куски; `await store.sweep()` убирает
  такие сироты (старше часа).
- Аренда считается часами машины: часы инстансов должны быть синхронны (NTP).
- `bucket`, `prefix`, `endpoint_url` (MinIO), `region`; ключи — стандартная
  цепочка botocore.

## Проверить своё

Своё хранилище проходит те же наборы:
`gc_testkit.thread_store_conformance_v2.assert_conforms` (включая
конкурентную запись) и `gc_testkit.agent_store_conformance.assert_conforms`
(ресурсы агента и проекта, память).

## Где что лежит

| Файл | Что |
|---|---|
| `gc_stores/sql/` | SQL-движок: `SqlThreadStore`, `SqlAgentStore`, диалект, подключение |
| `gc_stores/kv/` | KV-движок: `KvThreadStore`, `KvAgentStore`, `MemoryKV` |
| `gc_stores/postgres/`, `mysql/`, `sqlite/`, `redis/`, `s3/` | хранилища баз, миграции, `open_stores` |
| `gc_stores/migrations.py` | миграции схемы и версия |
| `gc_stores/sync.py`, `cli.py`, `connect.py` | `gc-stores sync` и `migrate` |
| `gc_core/stores.py` | `Stores` — связка двух хранилищ |
