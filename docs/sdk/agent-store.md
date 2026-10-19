[Назад к SDK](README.md)

# Хранилище агента: промпты, AGENTS.md, скиллы, субагенты, память проекта

Хранилище тредов (`ThreadStore`, см. [thread-store.md](thread-store.md)) держит
то, что относится к одному треду: историю, состояние, картинки. **Хранилище
агента** (`AgentStore`) — то, что одинаково для всех тредов агента:

| вид | что | имена |
|---|---|---|
| `prompt` | промпты | `Prompt`: `system`, `special_instructions`, `subagent`, `custom_subagent`, `summary`, `memory_patch`, `limit_summary`, `intent_tool`, `intent_final`, `finish_gate`, `routing_pair_judge`, `project_memory_compaction`, `command_judge_system`, `command_judge_user`, `init_agents_md` |
| `instructions` | AGENTS.md | `L0` — роль запуска (`paths.agent_md_path`), `L1` — глобальные |
| `subagent` | определения кастомных субагентов | `<файл>.md`; каталог — `list("subagent")` |
| `skill` | скиллы | `<слой>/<скилл>/…`, слои `system`, `user`, `project` |

и **память проекта** — текст с версией, которую агент копит между сессиями.

`L2` (`.gc/AGENTS.md`) и `L3` (`AGENTS.md` проекта) лежат в репозитории
пользователя и читаются из workspace, не отсюда.

По умолчанию — `FileAgentStore`: файлы по путям конфига и пакета, ровно как
ядро читало их всегда (пути хранилище вычисляет само по настройкам прогона).
Ничего передавать не нужно.

Своё хранилище (база, S3, конфиг-сервис) передаётся объектом, как хранилище
тредов:

```python
from gc_core.aio import AsyncAgent

agent = await AsyncAgent.create(settings, agent_store=MyAgentStore())
```

Под Temporal — в плагин воркера, одно на всех агентов или по агенту:

```python
AgentPlugin({"coder": settings}, thread_store=ts, agent_store=MyAgentStore())
AgentPlugin(agents, thread_store=ts, agent_store={"coder": MyAgentStore()})
```

Читается хранилище на воркере, в activity; в коде workflow ресурсов агента нет
(обращение туда — ошибка, а не тихое чтение диска). Тексты промптов идут в
историю Temporal с подготовкой каждого хода: промпт или AGENTS.md больше записи
истории (около 1 МБ кириллицы) — отказ хода (`TurnFailed`), сессия жива.

Когда читается: там же и тогда же, что до хранилища — системный промпт,
AGENTS.md и статус памяти на каждый прогон, служебные промпты в момент
использования, индекс скиллов — живой (скилл, созданный посреди хода, виден
сразу).

---

## Контракт

```python
from gc_core.agent_store import AgentStore, ProjectKey, ProjectMemory, ResourceInfo


class AgentStore(ABC):
    async def read(self, kind, name) -> bytes | None: ...
    async def list(self, kind, prefix="") -> list[ResourceInfo]: ...
    async def read_memory(self, project: ProjectKey) -> ProjectMemory | None: ...
    async def write_memory(
        self, project: ProjectKey, text: str, *, expected_version: str | None
    ) -> ProjectMemory: ...
    async def restore_memory(self, project: ProjectKey) -> None: ...  # необяз.
    def label(self, kind, name) -> str: ...  # как назвать ресурс в отказе
    def memory_label(self, project) -> str: ...  # как назвать память в отказе
```

Методы асинхронные и без приставки `a`: контракт — публичная поверхность для
авторов хранилищ (исключение из правила `docs/async.md`).

- `read`: `None` — ресурса нет (например, путь к нему не настроен); есть, но не
  читается — `OSError`. Байты, потому что assets скиллов бывают бинарными;
  тексты декодирует ядро (UTF-8, переводы строк — как `Path.read_text`).
- `list`: содержимое каталога ресурсов (`ResourceInfo(name, "file" | "dir")`)
  по алфавиту; нет каталога — `FileNotFoundError` / `NotADirectoryError`.
- `read_memory` / `write_memory`: память проекта `ProjectKey(id, root)`
  (`id` — slug каталога проекта, `root` — сам каталог). Версия —
  `memory_version(text)`: sha256 текста, `None` для пустого. Запись проходит,
  только если на месте версия `expected_version` (`None` — памяти нет или она
  пуста); иначе `MemoryConflict(current)`.
- `restore_memory` (по умолчанию — ничего): вернуть удалённую память из
  резервной копии. Зовёт только тул `project_memory`; `read_memory` копию не
  трогает — статус и отпечаток памяти читают её на каждом прогоне и шаге, и
  удалённое пользователем не должно возвращаться само.

Ядро отвечает на отказ так же, как раньше на файл: обязательный промпт роняет
запуск или ход с понятным текстом, необязательный (`special_instructions`, `L1`)
пропускается, судья команд выключается, битое определение субагента —
предупреждение, остальные загружаются. Обработка текста (проверка на пустоту,
подстановки в шаблоны, тексты отказов) — в ядре и одинакова для любого
хранилища: хранилище только отдаёт байты.

Конформанс своего хранилища — `gc_testkit.agent_store_conformance.assert_conforms`.

## Своё хранилище: что переопределить

Удобнее всего наследовать `FileAgentStore` и переопределить своё, остальное
отдать `super()` — пакетные промпты сжатия, finish gate и судьи берутся как
раньше:

```python
from gc_core.agent_store import FileAgentStore


class DbAgentStore(FileAgentStore):
    def __init__(self, settings, db):
        super().__init__(settings)
        self.db = db

    async def read(self, kind, name):
        row = await self.db.get(kind, name)
        if row is not None:
            return row.data
        return await super().read(kind, name)
```

Своё хранилище может отдать системный промпт, `special_instructions`, роль
запуска `L0` и субагентов, даже если пути в конфиге не заданы — ядро
спрашивает их и тогда.

Образец целиком в памяти — `gc_testkit.memory_agent_store.MemoryAgentStore`:
словари по видам, память проектов и `fallback` (по умолчанию файловое).

## Память проекта

Тул `project_memory` читает и пишет память через хранилище. Две правки
одновременно (два инстанса сервера, два окна TUI) не теряют друг друга:
`append` при конфликте перечитывает память и дописывает свою дельту к свежему
тексту, повторный конфликт — ошибка тула `conflict` («перечитайте»).
Файловое хранилище держит память в `<проект>/.gc/project_memory.md` с атомарной
записью и бэкапом вне дерева проекта; между процессами запись не блокируется —
для нескольких инстансов на одном проекте нужно хранилище с базой.

## Скиллы

Реестр скиллов строится по дереву `skill` хранилища: `list` по слоям, `read` по
файлам (`SKILL.md`, `pack.yaml`, `references/…`, `assets/…`). Описание,
инструкции, references и assets работают как у скиллов на диске.

Скрипты скиллов своего хранилища запускаются: перед запуском файлы скилла
выкладываются через `os_backend` в
`<launch_root>/.gc/skills_runtime/<слой>/<скилл>/<версия>` с манифестом
`.manifest.sha256`:

- версия — отпечаток набора файлов: файл, удалённый из хранилища, в новой
  версии не появится;
- неизменённый скилл повторно не копируется, но каждый файл копии перед
  запуском сверяется с хранилищем по sha256: правку модели (каталог открыт на
  запись) следующий запуск откатывает;
- имя файла из хранилища, уводящее за каталог скилла (`..`, абсолютный путь),
  — отказ запуска, а не запись мимо каталога;
- корень — `launch_root` рана после сверки с бэкендом (docker/ssh — корень в
  контейнере, а не на хосте).

Файловое хранилище собирает реестр по-прежнему (`get_skills_registry` по
бэкенду рана). Наследник `FileAgentStore`, у которого скиллы свои, берёт
реестр базового класса:

```python
class DbAgentStore(FileAgentStore):
    skills_registry = AgentStore.skills_registry
    aprovision_skills = AgentStore.aprovision_skills
```

## Где что лежит

| файл | что |
|---|---|
| `gc_core/agent_store/store.py` | `AgentStore` — контракт |
| `gc_core/agent_store/models.py` | `Prompt`, `ResourceInfo`, `ProjectKey`, `ProjectMemory`, `memory_version` |
| `gc_core/agent_store/file_store.py`, `file_paths.py` | `FileAgentStore` и правила путей |
| `gc_core/agent_store/reading.py` | чтение ядром: `aread_text`, `agent_store_of`, `project_key_of` |
| `gc_core/tools/skills/store_files.py` | реестр скиллов поверх дерева `skill` своего хранилища |
| `gc_testkit/memory_agent_store.py`, `agent_store_conformance.py` | образец в памяти и конформанс |
