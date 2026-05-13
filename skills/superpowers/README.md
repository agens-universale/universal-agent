# Superpowers Methodology Pack

Адаптация [obra/superpowers](https://github.com/obra/superpowers) для gc-cli:
набор process-скиллов, формирующих workflow для нетривиальной разработки —
brainstorming, writing-plans, TDD, debugging, code-review discipline.

## Когда использовать

- Новая фича, рефакторинг, behavior-changing изменение.
- Нетривиальный баг или test failure.
- Дизайн / план перед началом реализации.
- Code review (как requesting, так и receiving).

**Когда НЕ использовать:**
- Read-only вопросы («что делает функция X»).
- Trivial one-step команды («переименуй foo в bar»).
- Не-code задачи (конфиг, документация).

## Как вызвать скиллы пакета

### 1. Прямой canonical ID

```
gc-cli -q "use skill superpowers:brainstorming for designing feature X"
```

Агент видит canonical ID в реплике → вызывает `skills_load("superpowers:brainstorming")`.

### 2. Short alias (если уникальный)

```
gc-cli -q "use skill brainstorming for designing feature X"
```

Резолвер найдёт `superpowers:brainstorming` если такой skill-сегмент уникален
в реестре. При коллизии (два пакета с одинаковым inner-id) — структурная
ошибка с кандидатами.

### 3. Через намерение (агент дойдёт сам)

```
gc-cli -q "help me design a new feature"
```

Агент видит entry-скилл `superpowers:using-superpowers` в `skills_list`,
читает описание, понимает что нужен design workflow → загружает его, по
таблице триггеров переходит к `superpowers:brainstorming`.

## Скиллы внутри пакета

| Canonical ID | Назначение |
|---|---|
| `superpowers:using-superpowers` | **Entry-скилл** (bootstrap). Карта пакета: триггер → нужный скилл. |
| `superpowers:brainstorming` | Сократический рефайнмент идеи в дизайн ДО любого кода. Hard-gate. |
| `superpowers:writing-plans` | План реализации с задачами 2-5 минут, конкретным кодом, exact-путями. |
| `superpowers:executing-plans` | Исполнение плана task-by-task с checkpoint'ами. |
| `superpowers:test-driven-development` | RED-GREEN-REFACTOR цикл. С reference `testing-anti-patterns`. |
| `superpowers:systematic-debugging` | 4-фазный root-cause процесс. 3 references. |
| `superpowers:verification-before-completion` | Запрет claim'а «готово» без верифицирующего артефакта. |
| `superpowers:requesting-code-review` | Pre-review self-check. |
| `superpowers:receiving-code-review` | Дисциплина ответа на фидбек. |
| `superpowers:writing-skills` | Best-practices написания SKILL.md (для skill-scaffolder и для авторов). |

## Просмотр списка из CLI

```
gc-cli -q "list skills in the superpowers pack"
```

Агент вызовет `skills_list(pack="superpowers")` и покажет все internal.

## Override под свой проект

Чтобы переопределить конкретный скилл пакета — положить SKILL.md в той же
relative-path-структуре в свой layer. Например, чтобы переопределить
`superpowers:brainstorming` для текущего проекта:

```
<your-repo>/.gc/skills/superpowers/brainstorming/SKILL.md
```

При наличии файла в project-слое он выигрывает у system-слоя.

Чтобы переопределить **манифест** пакета (например, изменить `bootstrap_skill`):

```
<your-repo>/.gc/skills/superpowers/pack.yaml
```

## Что не входит в пакет

Скиллы upstream `obra/superpowers`, у которых нет аналогов тулов в gc-cli
(пока):

- `using-git-worktrees` — нет worktree-инструментов.
- `dispatching-parallel-agents` — нет встроенного параллельного диспатча.
- `subagent-driven-development` — то же.
- `finishing-a-development-branch` — зависит от `gh` CLI integration.

Будут добавлены, когда появятся соответствующие тулы в gc-cli.
