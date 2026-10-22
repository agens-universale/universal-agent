[Назад к SDK](README.md) · [Хранилище тредов: руководство](thread-store.md)

# Хранилище тредов: контракт для своей реализации

Ядро и фронтенды работают с тредом через фасады (`Threads`, сессия агента),
а хранилище видят только как объект контракта
`gc_core.threads.store.ThreadStore`. Всё производное — слияние состояния,
повторы при конфликте, продление аренды — живёт в фасадах, поэтому методов у
контракта немного и каждый — одна операция хранилища. Своё хранилище (база,
S3, Redis) передаётся агенту объектом: `AsyncAgent.create(settings,
thread_store=MyStore())`.

Методы асинхронные и **без приставки `a`**: это публичная поверхность для
авторов хранилищ, и она читается как обычная асинхронная библиотека
(исключение из правила [async.md](../async.md#как-называются-точки-входа)).

---

## Где что лежит

| файл | что |
|---|---|
| `gc_core/threads/store.py` | `ThreadStore` — контракт; докстринг модуля — его полный текст |
| `gc_core/threads/models.py` | модели: `ThreadKey`, `ThreadRecord`, `ThreadChange`, `SideChange`, `Receipt`, `Lease`… |
| `gc_core/threads/errors.py` | `ThreadConflict`, `OwnershipError`, `OperationMismatch` |
| `gc_core/threads/rules.py` | правила, общие для реализаций: выдача и проверка аренды, запись сбоку, порядок и курсор списка |
| `gc_testkit/memory_thread_store_v2.py` | `MemoryThreadStore` — образец целиком в памяти, без оптимизаций |
| `gc_stores/sqlite/`, `gc_stores/postgres/` | образцы на базах: транзакция на запись, блокировка строки треда |
| `gc_testkit/thread_store_conformance_v2.py` | `assert_conforms` — проверка своей реализации |

## Методы

```python
class ThreadStore(ABC):
    # чтение
    async def get_thread(self, key) -> ThreadRecord | None: ...
    async def list_threads(self, query: ThreadQuery) -> Page[ThreadRecord]: ...
    async def read_stream(self, key, stream, *, after_seq=0, limit=None) -> list[StreamEntry]: ...
    async def read_archive(self, key, archive_id) -> list[StreamEntry]: ...
    async def resolve_alias(self, alias) -> ThreadKey | None: ...
    # запись
    async def commit(self, key, change: ThreadChange) -> Receipt: ...
    async def append_side(self, key, change: SideChange) -> Receipt: ...
    # аренда и жизненный цикл
    async def claim(self, key, lease: LeaseRequest) -> ThreadRecord: ...
    async def release(self, key, lease: Lease) -> None: ...
    async def delete(self, key, *, op_id) -> None: ...
    # вложения и обслуживание
    async def put_blob(self, key, data: bytes, *, mime) -> BlobRef: ...
    async def get_blob(self, ref: BlobRef) -> bytes: ...
    async def prune_receipts(self, older_than: timedelta) -> int: ...
    # необязательное: push (по умолчанию — опрос read_stream)
    async def watch(self, key, stream, *, after_seq, generation=None,
                    history_version=None, poll_interval=0.5): ...
```

Наследуйте ABC: забытый метод — отказ при создании объекта, а не на
середине хода.

## `commit`: порядок проверок — в одной транзакции

1. Есть квитанция этого `op_id`: то же содержимое (`change_digest`) — вернуть
   её и ничего не писать; другое — `OperationMismatch`.
2. `lease` не совпадает с текущей арендой (владелец и эпоха) или истекла по
   часам хранилища — `OwnershipError`. `commit` без `claim` — тоже.
3. `expected_revision` не равна текущей ревизии — `ThreadConflict`.
4. Применить изменения, увеличить `revision`, сохранить квитанцию и вернуть
   её. Квитанция `commit` несёт аренду после записи (`reset_history` её
   снимает — `None`).

Проверки 2–3 — `rules.check_writer`. Две записи одновременно не должны
пройти обе: у базы это транзакция с блокировкой строки треда **до** проверок
(Postgres — `SELECT … FOR UPDATE` под READ COMMITTED, SQLite — `BEGIN
IMMEDIATE`). Блокировка до проверки квитанции нужна, чтобы повтор той же
операции, пришедший пока первая ещё пишет, получил квитанцию, а не конфликт.

**Применение — в порядке:** `reset_history`, `replace_history`,
`discard_message_ids`, `append_messages`, `set_*`, `add_aliases`.

- `set_state` / `set_usage` / `set_info` / `set_runtime` / `set_extra`
  **заменяют** значение целиком (слияние — забота фасада).
- `discard_message_ids` сверяется с `data["id"]` записей истории (строки).
- `replace_history` — новая история с `seq` 1, `history_version + 1`; при
  `archive=True` прежняя уходит в архив (`archives`, `read_archive`).
- `reset_history` очищает только поток `messages` (архивы, остальные потоки,
  состояние не трогает), увеличивает `generation` и `history_version` и
  **снимает аренду**.
- Алиас глобален: принадлежит другому треду — `ValueError`, и ничего не
  записано.

## `append_side`

Проверяет только `op_id` (повтор — та же квитанция) и ревизию не меняет:
статус хода, состояние фронтендов и журналы рядом с историей не конфликтуют с
ходом. Для нового ключа создаёт тред без аренды. В историю (`messages`) не
пишет — её пишет только `commit`. `set_frontend` заменяет состояние одного
фронтенда, `patch_frontend` сливает ключи (`dict.update`) — под блокировкой
хранилища (`rules.patched_frontend`).

**Тред durable-исполнителя** (бессрочная аренда — сессия Temporal): запись
хода сбоку (`set_runtime`, `append`, `add_aliases`) проходит только с его
арендой в `SideChange.lease`, иначе `OwnershipError`; состояние фронтенда —
любой процесс. Готовая проверка — `rules.check_side_writer`.

## Аренда и фенсинг

- `claim` проходит, если аренды нет, она того же владельца и жива (продление,
  эпоха та же) или истекла (эпоха + 1 — даже для того же владельца). Чужая
  живая — `OwnershipError`. Готовое правило — `rules.granted_lease`.
- `ttl=None` — бессрочно (владелец — workflow Temporal).
- Истечение считают **часы хранилища** (база — свои часы; в проверках —
  переданный `clock`), не часы инстансов.
- `release` чужой или уже сменённой аренды ничего не делает. Последняя
  выданная эпоха переживает `release`.
- Эпоха переживает и `delete`: тред, созданный заново под тем же ключом,
  выдаёт эпоху больше прежней, иначе запись по аренде удалённого треда прошла
  бы фенсинг. Надгробие (эпоху и поколение удалённого треда) хранилище вправе
  забыть через сутки: аренды со сроком, выданные до удаления, к тому времени
  истекли. Файловое так и делает — поверхность chat удаляет тред после каждого
  completions.

## Потоки

- `seq` строго растёт и **не переиспользуется**: удалённый хвост номер не
  освобождает, иначе читатель с курсором пропустил бы следующую запись.
  Исключение — `replace_history` и `reset_history`: история нумеруется заново
  с 1, а читатель узнаёт о замене по `history_version` (и о пересоздании
  треда — по `generation`).
- Имена потоков: `messages`, `subagent_events`, `edits`, `inbox` и потоки
  фронтендов `<фронтенд>.<имя>` (`models.check_stream_name`).
- `read_stream(limit=0)` — пусто, `limit=None` — всё.

## Время и список

- `created_at` ставится при создании треда, `updated_at` — при каждом
  `commit` (и не идёт назад); `append_side`, `claim`, `release` его не
  двигают — иначе список тредов прыгал бы от каждого тика статуса.
- Формат — ISO-8601 со смещением; границы `ThreadQuery` (`updated_before`,
  `updated_after`) сравниваются как моменты времени, а не как строки.
- Список — `updated_at` по убыванию, затем `system`, `thread` по кодам
  символов; курсор — непрозрачная строка. Образец — `rules.page_of`.

## Данные

- Хранилище отдаёт данные **ровно как записаны**: порядок ключей, `\u0000` в
  выводе инструментов, `NaN` / `Infinity`. (Поэтому `PostgresThreadStore`
  держит документы в `text`, а не в `jsonb`.) Изменение словаря после записи
  или чтения не меняет хранимое.
- Модели хранятся со **своими заданными полями** (`model_dump(exclude_unset=True)`):
  по `model_fields_set` фасад строит meta — «не задано» и «задано значением
  по умолчанию» после чтения разные.
- Ключи, которых модели не знают (`extra`, лишние поля `Usage` и
  `RuntimeStatus`), хранятся как есть.
- `put_blob` — вложение треда; ссылка для сообщения — `ref.location`
  (`thread-file://<system>/<thread>/<имя>` у хранилищ без диска). Нет —
  `FileNotFoundError`.

## Отказы

- Нет треда — `None` или `[]`, а не исключение.
- `ThreadConflict`, `OwnershipError`, `OperationMismatch` — отказы контракта
  (`ThreadStoreError`); фасад повторяет запись на конфликте сам.
- Недоступность самого хранилища (сеть, диск) — исключения драйвера как есть.
- Квитанции живут 7 дней: `prune_receipts(older_than)` удаляет старые и
  возвращает, сколько. Хранилище может чистить их и само (файловое — при
  записи в тред, раз в сутки).

## `watch`

Новые записи потока по мере появления; курсор читателя —
`(generation, history_version, after_seq)`: тред пересоздан или история
заменена — выдача начинается с начала новой истории. `None` — значение на
момент первого опроса; читатель, которому важно не пропустить замену между
своим чтением и стартом, передаёт их явно. По умолчанию — опрос
`read_stream`; хранилище с push (LISTEN/NOTIFY, Redis streams) переопределяет.

## Проверить себя

```python
from gc_testkit.thread_store_conformance_v2 import assert_conforms


def test_my_store():
    assert_conforms(lambda clock: MyStore(dsn, clock=clock))
```

Набор — около пятидесяти проверок, каждая на свежем хранилище со свежими
часами (`FakeClock`: время идёт только по `advance`, поэтому хранилище должно
уметь брать время у переданных часов). Хранилище с соединениями набор
закрывает после каждой проверки в том же loop (`await store.close()`, если он
есть). Отказ — `ConformanceError` с именем проверки и тем, что нарушено.

## Картинки и файлы треда

В историю картинка не пишется: у сообщения есть ключ `attachments` — ссылки на
нормализованные файлы (хеш, mime, размеры, путь; `source` — откуда картинка),
а `content` остаётся строкой с плашками `[Изображение #N]`. Номер `N`
сквозной по треду: счётчик — `runtime.image_seq`, только растёт.

Где лежат байты, решает хранилище:

- **файловое** — `<threads_dir>/images/<system>_<thread>_<sha16>.png|.jpg`;
  удаляются вместе с тредом, сжатие истории их не трогает;
- **без каталога тредов** (SQLite, Postgres, MySQL, Redis, S3, своё) —
  вложения треда контракта: `put_blob` (имя выбирает хранилище, ссылка —
  `ref.location`, `thread-file://<system>/<thread>/<имя>`) и `get_blob`.
  Снимки `fs_view_image`, картинки сообщений и медиа из ответов MCP ложатся
  туда, запрос к модели и `fs_view_image` читают байты через `get_blob`;
  удаляет их само хранилище вместе с тредом. На диске процесса — ничего:
  тред на другом инстансе или воркере видит те же картинки.

## Единственный ключ конфига

```yaml
paths:
  threads_data_path: /абсолютный/путь
```

Нужен только файловому хранилищу. CLI и TUI делят этот путь намеренно —
тред, начатый в `gc-cli -t …`, открывается в TUI.

---

## Прежний контракт удалён

Хранилище прежнего контракта (`ThreadStoreBase`, одиннадцать методов
`load_messages` … `read_subagent_trace`) ядро больше не принимает: проводка
отвергает его сразу (`TypeError` из `gc_core.threads.store.require_thread_store`).
Перенос — реализовать двенадцать методов v2; файлы треда формата develop
читает и пишет `FileThreadStore` v2.

## Что проверяет автоматический набор

| | где |
|---|---|
| конформанс v2: память, файлы, SQLite, Postgres (при `GC_TEST_POSTGRES_DSN`) | `packages/core/tests/threads/`, `packages/stores/tests/` |
| путь хода (сессия агента): чужие секции, сжатие, повторное открытие — на каждом хранилище | `gc_testkit.session_checks`, `threads/test_session_v2.py`, `packages/stores/tests/` |
| два инстанса на одном хранилище | `test_two_instances.py`, `test_postgres_thread_store.py` |
| SDK на хранилище v2 не пишет файлы и не морозит цикл | `test_acceptance_sdk_thread_store.py` |
| провод Temporal (разницы, куски, повторы) | `packages/temporalio/tests/test_thread_store.py` |
| запись данных агента — только через хранилища | `tests/test_agent_data_writes_go_through_stores.py` |
