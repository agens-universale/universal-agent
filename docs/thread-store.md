[Назад к README](../readme.md)

# Хранилище тредов: как подставить своё

История диалога по умолчанию лежит в `data_*.jsonl` рядом с `meta_*.json`. Это
реализация, а не устройство ядра: встраивающий может положить тред в базу и
передать своё хранилище агенту при создании.

Через конфиг оно **не настраивается** и настраиваться не может — хранилище
передаётся объектом. В YAML есть только `paths.threads_data_path`, и он нужен
единственному хранилищу, которое работает с файлами.

---

## Где что лежит

| файл | что |
|---|---|
| `gc_core/thread_store_protocol.py` | `ThreadStore` — контракт, 11 синхронных методов. Плюс типы: `ThreadState`, `SubagentTraceRecord`, `TraceKind`, `MessageKey` |
| `gc_core/thread_store_async.py` | `AsyncThreadStore` — те же 11 с приставкой `a`. Плюс `SyncThreadStoreAdapter` и фабрика `athread_store()` |
| `gc_core/thread_store_file.py` | `FileThreadStore` — реализация поверх `data_*.jsonl`. Образец для своей |
| `gc_core/thread_store.py` | файловый слой и `ThreadSession`. Внутренности; встраивающему сюда не нужно |
| `gc_testkit/memory_thread_store.py` | `MemoryThreadStore` — вторая реализация, целиком в памяти. Второй образец, 230 строк |
| `gc_testkit/thread_store_conformance.py` | `assert_conforms` / `assert_conforms_async` — проверка своей реализации |

Наследовать нечего: `ThreadStore` — `typing.Protocol`, структурная типизация.
Совпал по сигнатурам — подходит. Это не вкусовщина, а свой урок: `FsBackend`
сделан ABC с 31 абстрактным методом, его реализуют снаружи, и добавить туда
async-двойники стало невозможно — любой новый метод ломал бы каждую внешнюю
реализацию.

---

## Как прокинуть store

Пять точек, все проверены. Обычному встраивающему нужна первая.

### 1. SDK — `AsyncAgent.create`

```python
from gc_core.aio import AsyncAgent

agent = await AsyncAgent.create(cfg, thread_store=MyStore())
answer = await agent.run("привет", thread_id="t1")
```

Дальше ничего делать не нужно: хранилище доезжает до `RunContext`, оттуда до
`ThreadSession`, и весь ввод-вывод треда идёт через него.

### 2. Контекст напрямую

```python
from gc_core.run_context import create_run_context, acreate_run_context

ctx = create_run_context(cfg, thread_store=MyStore())          # синхронно
ctx = await acreate_run_context(cfg, thread_store=MyStore())   # не блокируя loop
```

### 3. Исполнитель

```python
from gc_core.agent_executor import AgentExecutor

executor = AgentExecutor(ctx)      # берёт хранилище из ctx, отдельно передавать не нужно
```

### 4. Сессия треда — если работаете с историей мимо агента

```python
from gc_core.thread_store import ThreadSession

session = ThreadSession.from_config(
    config_dict=cfg, system_id="cli", thread_id="t1", store=MyStore(),
)
session = await ThreadSession.afrom_config(...)   # асинхронный двойник
```

### 5. `system_id` — не забыть

Читать тред надо тем же `system_id`, каким его писали. У SDK он **`"sdk"`**, у
CLI — `"cli"`:

```python
await agent.run("привет", thread_id="t1")
store.load_messages("sdk", "t1")      # не "cli"
```

Частая ошибка при первом чтении из своего хранилища: тред «пустой», хотя всё
записано.

И `thread_id` — только латинские буквы, цифры, `_` и `-`. Кириллица падает
`ValueError` на валидации.

---

## Что реализовать: одиннадцать методов

```python
class MyThreadStore:
    # чтение
    def load_messages(self, system_id, thread_id) -> list[dict]: ...
    def load_thread_state(self, system_id, thread_id) -> ThreadState: ...
    def load_meta(self, system_id, thread_id) -> dict: ...

    # запись
    def append_messages(self, system_id, thread_id, messages, *,
                        known_keys=None, start_seq=0) -> int: ...
    def save_meta(self, system_id, thread_id, meta) -> None: ...
    def patch_runtime_flag(self, system_id, thread_id, flag, *,
                           active, session_meta=None) -> None: ...
    def update_runtime_meta(self, system_id, thread_id, patch) -> None: ...
    def discard_message(self, system_id, thread_id, message_id) -> bool: ...
    def reset(self, system_id, thread_id) -> list[str]: ...

    # трассы субагентов
    def append_subagent_trace(self, system_id, thread_id, *, parent_call_id,
                              child_run_id, kind, payload) -> SubagentTraceRecord: ...
    def read_subagent_trace(self, system_id, thread_id) -> list[SubagentTraceRecord]: ...
```

Образцы: `FileThreadStore` (185 строк) и `MemoryThreadStore` (233 строки).

---

## Проверить себя

```python
from gc_testkit.thread_store_conformance import assert_conforms

def test_my_store():
    assert_conforms(lambda: MyThreadStore(dsn=TEST_DSN))
```

Пятнадцать проверок: три структурных (методы на месте, синхронны, сигнатуры
совпали) и двенадцать поведенческих. Отказ называет, что именно разошлось.

**`isinstance(store, ThreadStore)` гейтом не является.** `runtime_checkable`
сверяет только наличие атрибутов: у класса, где все одиннадцать методов
объявлены `async def`, проверка проходит, а восемь свойств нарушены. Ошибка
симметрична — законное прокси-хранилище на `__getattr__` `isinstance` не
пройдёт, потому что в 3.12 проверка идёт через `getattr_static`.

---

## Восемь свойств, которые легко нарушить

Это не общие пожелания. Каждое — место, где разошлись **две наши собственные**
реализации, оставаясь зелёными на всём прежнем наборе тестов.

**Идемпотентность `append_messages`.** Батч персистится ДВАЖДЫ — сразу после
исполнения тулов и ещё раз на выходе цикла. Повтор не должен задваивать
историю.

**`known_keys` мутируется НА МЕСТЕ и только записанным.** Вызывающий держит тот
же объект и снимает снимок ДО вызова, чтобы по приросту понять, что записалось.
Защитная копия — совершенно естественное решение — молча задваивает историю на
следующем батче. Досыпать туда заодно всё содержимое треда тоже нельзя: это
ломает ту же разницу с другой стороны.

**Третий элемент `load_thread_state` — НОМЕР последнего `seq`, а не количество
записей.** Расходятся после `discard_message`: удалили среднее из трёх —
количество 2, номер по-прежнему 3. Оба `int`, и подмену не поймает ни одна
проверка типов. `seq` — это ordering key хвостового читателя TUI: на записи с
меньшим номером тот отдаёт `stream_reset` и перестраивает живой трейс.

**`id` проставляется на чтении.** Без него ключ идемпотентности уходит в ветку
`payload:<json>` — нестабильную к любому изменению формы сообщения. Готовая
функция: `gc_core.thread_store.ensure_message_id`.

**Meta штампуется при записи:** `state_schema_version`, `created_at`,
`updated_at`. Самое коварное из восьми: без версии схемы следующий прогон
ОТКАЖЕТСЯ читать тред (`StateSchemaError`), но ПЕРВЫЙ проходит нормально —
юнит-тесты первого прогона такое не ловят. Готовое значение даёт
`ThreadMetaMapper.ensure_v2_meta(meta)`.

**Снятие последнего флага убирает секцию `runtime` ЦЕЛИКОМ.** «Нет флагов» и
«пустой словарь» обязаны читаться одинаково: расхождение означало бы, что
переезд на другое хранилище меняет форму meta, а её читают и TUI, и сервер.

**Запись трассы субагента — шесть полей**, включая `ts` и `seq`, которые
проставляет хранилище, а не вызывающий.

**Форма meta ОТКРЫТАЯ.** Секцией `tui` владеет другой процесс, сервер отдаёт
meta наружу дословно — незнакомые ключи хранятся как есть, а не отбрасываются.

Отсутствующий тред читается пустотой, а не исключением.

---

## Асинхронный клиент

`ThreadStore` синхронный, и это правильный контракт по умолчанию: файлы и
локальная база отвечают за микросекунды. Но у `redis.asyncio` и `asyncpg`
синхронного клиента нет вовсе.

```python
class RedisThreadStore:
    async def aload_messages(self, system_id, thread_id): ...
    async def aload_thread_state(self, system_id, thread_id) -> ThreadState: ...
    # ...остальные девять с приставкой a

agent = await AsyncAgent.create(cfg, thread_store=RedisThreadStore(url))
```

Передаётся туда же. Ядро само разбирается — вся развилка в одной функции
(`gc_core.thread_store_async.athread_store`):

```python
if is_async_thread_store(store):
    return store                          # нативное — ждём напрямую
return SyncThreadStoreAdapter(store)      # синхронное — заворачиваем
```

Внутри ядро знает только асинхронный порт и всюду пишет
`await port.aload_meta(...)`. Так у async-пути не может быть «второго,
забытого» вызывающего — он единственный.

Проверить себя: `assert_conforms_async(factory)` — тот же набор свойств.
Свойства от корутинности не зависят, поэтому набор один на обе формы.

**Смешивать формы нельзя.** Либо все одиннадцать методов синхронные, либо все
одиннадцать корутинные. Половинчатая реализация отвергается при создании
агента:

```
Половинчатое: часть методов объявлена async def под СИНХРОННЫМИ именами: load_meta.
  Смешивать нельзя: синхронные вызывающие отбросят корутину без ошибки,
  и часть записей треда потеряется молча.
```

---

## Синхронное СЕТЕВОЕ хранилище: будет работать, но

Адаптер зовёт синхронные методы **инлайн**, не через поток — и это не
недосмотр. `asyncio.to_thread` здесь ошибка, проверенная запуском:
`save_thread_meta` пишет во фиксированный путь `{meta_path}.tmp`, и два потока
на одном треде дают `FileNotFoundError`; а дефолтный пул общий с синхронными
чужими тулами.

Значит адаптер даёт совместимость, но неблокирующим хранилище не делает: цикл
честно стоит на каждом обращении. Замеренный закон:

```
максимальный лаг цикла ≈ 11 × RTT + 29 мс
```

Одиннадцать — потому что 25 обращений за прогон укладываются в три блока подряд
(11, 11 и 3) без возврата в цикл.

| RTT на вызов | лаг цикла | критерий приёмки < 150 мс |
|---|---|---|
| 0 (файлы, память) | 15 мс | проходит |
| 5 мс (локальный Redis) | 79 мс | проходит |
| 10 мс | 148 мс | **на границе** |
| 20 мс (облако, другая зона) | 308 мс | **не проходит** |

Числа машинозависимы; граница проходит около 10 мс.

Отличить быстрое локальное хранилище от сетевого статически нельзя, поэтому
адаптер меряет первые восемь обращений и один раз пишет предупреждение:

```
thread_store_blocking_loop store=MyStore slowest_method=append_messages
    median_ms=6.2 threshold_ms=2 samples=8
    hint=implement_AsyncThreadStore_for_network_backed_storage
```

Судит по **медиане** окна, а не по отдельному вызову: у `FileThreadStore`
медиана 0,29 мс при отдельных всплесках до 2,8 мс (запись меты, конкуренция за
диск между агентами), и порог по одному вызову советовал владельцу обычных
файлов переписать хранилище на асинхронное.

---

## Необязательная способность: архив перед компакцией

Если у хранилища есть метод `archive_and_replace(system_id, thread_id,
messages)`, компакция зовёт его. Если нет — история замещается через `reset` +
`append_messages`, без архива.

Так и задумано: «отложить прежнюю историю рядом» осмысленно для файлов, а у
базы это либо отдельная таблица, либо не нужно вовсе. В основные одиннадцать
метод не входит — реализация без него полноценна.

---

## Единственный ключ конфига

```yaml
paths:
  threads_data_path: /абсолютный/путь
```

Нужен ТОЛЬКО файловому хранилищу и обязан быть абсолютным. Своей реализации он
не нужен: `ThreadSession` берёт каталог как `getattr(store, "threads_dir", "")`,
и у хранилища без файлов там пустая строка.

CLI и TUI делят этот путь намеренно — тред, начатый в `gc-cli -t …`,
открывается в TUI. В swarm он перекрывается последним слоем конфига
(см. [swarm.md](swarm.md)).

---

## Что проверяет автоматический набор

| | где |
|---|---|
| контракт, обе реализации, 15 свойств | `test_thread_store_protocol.py` |
| асинхронный путь, порт, адаптер | `test_thread_store_async.py` |
| SDK на чужом хранилище не пишет файлы | `test_acceptance_sdk_thread_store.py` |
| в ядре нет прямых вызовов файлового слоя | AST-гейт в `test_thread_store_protocol.py` |

Живая база (настоящий Redis) — в [ручных тестах](manual-tests.md), кейсы S-1…S-4.
