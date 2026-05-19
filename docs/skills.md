[Назад к README](../readme.md)

# Скиллы и пакеты скиллов

## Скиллы

gc-cli ищет скиллы в трёх местах (project > user > system, верхний слой перетирает нижний по `skill_id`):

- **Project:** `<your-repo>/.gc/skills/<skill-id>/SKILL.md` — командные скиллы для конкретного проекта.
- **User:** `~/.local/share/gc/skills/<skill-id>/SKILL.md` (Linux/macOS) или `~/.gc/skills/<skill-id>/SKILL.md` (Windows) — личные скиллы, доступные во всех проектах.
- **System:** скиллы, поставляющиеся с gc-cli — read-only.

Чтобы добавить project-скилл — положите `SKILL.md` в `<your-repo>/.gc/skills/<id>/`.

Чтобы зафиксировать единственный источник скиллов (например, в production), задайте `paths.skills_data_path` в `config.yml` — auto-discovery трёх слоёв при этом отключается.

## Пакеты скиллов

Пакет скиллов — это группировка связанных скиллов в подпапке `skills/<pack-id>/`,
помеченной файлом `pack.yaml`. Скиллы пакета лежат в подпапках, каждый со своим
`SKILL.md`. Один из internal-скиллов помечается в `pack.yaml` как `bootstrap_skill`
и играет роль точки входа в пакет.

### Геометрия

```
<repo>/skills/                         <gc_data>/skills/    <cwd>/.gc/skills/
├── complex-task-solver/SKILL.md       (то же)              (то же)
├── topic-research/SKILL.md
├── ...                                ← прочие flat-скиллы
├── supercoder/                        ← пакет (содержит pack.yaml)
│   ├── pack.yaml
│   ├── using-supercoder/SKILL.md      ← bootstrap
│   ├── brainstorming/SKILL.md
│   └── ...
├── codebase/                          ← пакет
│   ├── pack.yaml
│   ├── using-codebase/SKILL.md        ← bootstrap
│   ├── project-overview/SKILL.md
│   └── project-investigation/SKILL.md
└── articles/                          ← пакет
    ├── pack.yaml
    ├── using-articles/SKILL.md        ← bootstrap
    ├── article-creator/SKILL.md
    └── check-article/SKILL.md
```

Override-семантика та же что для обычных скиллов: `project > user > system`
по canonical ID.

### Манифест `pack.yaml`

```yaml
id: supercoder                        # default = имя папки
name: Supercoder Methodology
description: |
  Process methodology for design, planning, TDD, debugging.
version: 1.0.0
source: https://github.com/obra/superpowers
bootstrap_skill: using-supercoder     # обязательно для видимости в skills_list
```

### Canonical IDs

- **Flat-скилл**: `<skill-id>` (без префикса), например `topic-research`.
- **Internal-скилл пакета**: `<pack-id>:<inner-id>`, например `supercoder:brainstorming`.

### Видимость в `skills_list`

По умолчанию `skills_list` возвращает:
- Все flat-скиллы.
- По **одной** записи на пакет — bootstrap-скилл (помеченный полями `is_pack_bootstrap: true` и `pack_size: N`).

Internal-скиллы пакета **скрыты** в default-выдаче (чтобы не засорять системный
промпт). Но они **всегда адресуемы** через canonical ID — `skills_load("supercoder:brainstorming")`
работает в любом случае.

Чтобы получить полный список internal-скиллов пакета:

```
skills_list(pack="supercoder")
```

### Override

Чтобы переопределить конкретный internal-скилл пакета под свой проект — положить
свой SKILL.md в той же relative-path-структуре в `.gc/skills/`:

```
<your-repo>/.gc/skills/supercoder/brainstorming/SKILL.md
```

### Встроенные пакеты

В состав gc-cli встроены три пакета:

- [`supercoder`](../skills/supercoder/README.md) — методологический набор
  (brainstorming, writing-plans, TDD, systematic-debugging, code-review
  discipline). Точка входа — `supercoder:using-supercoder`.
- `codebase` — read-only-понимание кода: `project-overview` (карта
  репозитория) и `project-investigation` (трассировка поведения/потока).
  Точка входа — `codebase:using-codebase`.
- `articles` — авторинг русских Markdown-статей: `article-creator`
  (создать новую) и `check-article` (фактчек/правка существующей).
  Точка входа — `articles:using-articles`.
