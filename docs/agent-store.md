[Назад к README](../readme.md)

# Хранилище агента: промпты, AGENTS.md, скиллы, субагенты

Хранилище тредов (`ThreadStore`, см. [thread-store.md](thread-store.md)) держит
то, что относится к одному треду: историю, meta, картинки. **Хранилище агента**
(`AgentStore`) — то, что одинаково для всех тредов агента и что ядро только
читает:

| вид | что | имена |
|---|---|---|
| `prompt` | промпты | `system`, `special_instructions`, `subagent`, `custom_subagent`, `summary`, `memory_patch`, `limit_summary`, `intent_tool`, `intent_final`, `finish_gate`, `project_memory_compaction`, `command_judge_system`, `command_judge_user` |
| `instructions` | AGENTS.md | `L0` — роль запуска (`paths.agent_md_path`), `L1` — глобальные, `L2` — `.gc/AGENTS.md`, `L3` — `AGENTS.md` проекта |
| `subagents` | определения кастомных субагентов | `""` — каталог, `<файл>.md` — определение |
| `skills` | скиллы | `<слой>/<скилл>/…`, слои `system`, `user`, `project` |

По умолчанию — `FileAgentStore`: файлы по путям конфига и пакета, ровно как
ядро читало их всегда. Ничего передавать не нужно.

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
(обращение туда — ошибка, а не тихое чтение диска).

---

## Контракт

```python
from gc_core.agent_store import AgentResource, AgentStore


class AgentStore(ABC):
    def read_text(self, resource: AgentResource) -> str: ...
    def list_dir(self, resource: AgentResource) -> list[tuple[str, "file" | "dir"]]: ...
```

`AgentResource(kind, name, path)` — вид, логическое имя из таблицы выше и путь,
который ядро вычислило по конфигу (или `None`, если путь не задан). Файловое
хранилище читает по пути, своё отвечает по виду и имени — путь для него
подсказка.

Отказ — как у `Path.read_text`: нет ресурса — `FileNotFoundError`, есть, но не
читается — `OSError` / `UnicodeDecodeError`. Ядро отвечает на него так же, как
раньше на файл: обязательный промпт роняет запуск с понятным текстом,
необязательный (`special_instructions`, слои AGENTS.md) пропускается, битое
определение субагента — предупреждение, остальные загружаются.

Обработка текста (проверка на пустоту, подстановки в шаблоны, тексты отказов)
остаётся в ядре и одинакова для любого хранилища: хранилище только отдаёт текст.

## Своё хранилище: что переопределить

Удобнее всего наследовать `FileAgentStore` и переопределить своё, остальное
отдать `super()` — пакетные промпты сжатия, finish gate и судьи берутся как
раньше:

```python
from gc_core.agent_store import AgentResource, FileAgentStore


class DbAgentStore(FileAgentStore):
    def __init__(self, db):
        self.db = db

    def read_text(self, resource: AgentResource) -> str:
        row = self.db.get(resource.kind, resource.name)
        if row is not None:
            return row.text
        return super().read_text(resource)
```

Ресурсы без пути: своё хранилище может отдать системный промпт,
`special_instructions`, роль запуска `L0` и субагентов, даже если пути в
конфиге не заданы — ядро спрашивает их и тогда.

Образец целиком в памяти — `gc_testkit.memory_agent_store.MemoryAgentStore`:
словари по видам и `fallback` (по умолчанию файловое).

## Скиллы

Реестр скиллов строится по дереву `skills` хранилища: `list_dir` по слоям,
`read_text` по файлам (`SKILL.md`, `pack.yaml`, `references/…`, `assets/…`).
Описание, инструкции, references и assets работают как у скиллов на диске.

**Скрипты скиллов из своего хранилища не запускаются**: скрипту нужен файл в
среде исполнения, а хранилище отдаёт текст. `skills_run_script` отвечает
понятным отказом. Скрипты работают у файлового хранилища — как раньше, в том
числе с копированием в удалённую среду (docker / ssh).

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
| `gc_core/agent_store.py` | `AgentStore`, `FileAgentStore`, `AgentResource`, `agent_store_of` |
| `gc_core/tools/skills/store_files.py` | реестр скиллов поверх дерева `skills` своего хранилища |
| `gc_testkit/memory_agent_store.py` | `MemoryAgentStore` — образец в памяти |
