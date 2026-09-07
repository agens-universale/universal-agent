# Default Coworker Skill Pack — спецификация

Статус: v0.1.0 draft, готов к подключению в registry.
Цель: слой навыков «цифрового коллеги» для широких нетривиальных задач с низким зацеплением между inner-скиллами.

## Epistemic stance (канон)

Коллега беспристрастен: truth over ego, критика предпосылок важнее приятного тона, факты отделены от гипотез, эзотерика/маркетинг не подаются как знание. Закреплено в bootstrap и cross-cutting skills.

## Архитектура

| Модуль | Ответственность |
|---|---|
| `prompt3` (system) | Короткое ядро: safety, honesty, tools, memory, workspace, finalize |
| `using-default-coworker` | Единственный router: классификация, autonomy/evidence, маршрутизация |
| `research` / `personal-assistant` / `background-business` | Режимы работы; не ссылаются друг на друга |
| `deliverables-evidence` / `handoff-autonomy` | Cross-cutting методы; автономны от режимов |
| Внешние packs | Специализация по main goal; ссылки только из bootstrap |

**Низкое зацепление:** inner-скилы не знают имён соседей. Смена состава pack = правка bootstrap (+ pack.yaml), не N файлов.

**Tool-agnostic:** действия описаны ролями; явно именуются только `skills_*` (см. `docs_md/principles.md` §5, `test_skills_tool_agnostic.py`).

## Layout

```text
skills/default-coworker/
  pack.yaml
  README.md
  using-default-coworker/SKILL.md   # bootstrap, виден в default skills_list
  research/SKILL.md
  personal-assistant/SKILL.md
  background-business/SKILL.md
  deliverables-evidence/SKILL.md
  handoff-autonomy/SKILL.md
```

## Public discovery

Default `skills_list` показывает только `default-coworker:using-default-coworker`.
Internal — через `skills_list(pack="default-coworker")`.

## System prompt (интеграция, to-be)

В раздел `### Skills` добавить одну строку:

```markdown
For broad non-trivial work, first consider `default-coworker:using-default-coworker` as the default method/router layer. Use it to classify the task, choose autonomy/evidence/deliverable level, and then load narrower specialized skills only when they fit the main goal. Do not load it for simple direct answers.
```

## Autonomy (L0–L5)

L0 answer → L1 inspect → L2 artifact → L3 local exec → L4 orchestrate → L5 external/irreversible (approval). По умолчанию — минимальный достаточный уровень.

## Evidence labels

observed · derived · sourced · assumption · unknown — явно при high-stakes.

## Routing matrix (канон — только bootstrap)

| Запрос | Primary | External pack (если main goal) |
|---|---|---|
| простой ответ | без pack | — |
| широкая задача | bootstrap → mode/method | по таблице в SKILL.md |
| domain/market deep research | `topic-research` | — |
| DOI/PMID/arXiv literature | `science-article-creator` | — |
| ambiguous / lightweight research triage | `research` | specialist if scope clarifies |
| coding | — | `supercoder` |
| codebase read-only | — | `codebase` |
| русская статья | — | `articles` |
| GUI action | — | `computer-use` |
| high-impact orchestration | `complex-task-solver` | `handoff-autonomy` lazy (L5/continuity) |
| личная организация | `personal-assistant` | — |
| фоновый бизнес-процесс | `background-business` | — |

## Приёмка

1. Bootstrap ≤ ~100 строк; inner ≤ 120 строк.
2. `pack.yaml` + registry: bootstrap в default list, 6 inner по `pack=`.
3. Inner-скилы не содержат `default-coworker:<other-inner-id>`.
4. Approval gates для L5 / external.
5. `uv run pytest tests/test_pack_default_coworker_smoke.py` зелёный.
6. `test_skills_tool_agnostic.py` зелёный на дереве skills.

## Smoke scenarios

1. «Какой инструмент для X?» → bootstrap → research (triage) или topic-research если deep brief.
2. «Статьи по теме Y» → bootstrap → science-article-creator (specialist-first).
3. «Организуй день» → bootstrap → personal-assistant.
4. «Следи за процессом» → bootstrap → background-business (+ cadence question).
5. «Почини баг» → supercoder (bootstrap может пропустить coworker modes).
6. «Открой Chrome» → computer-use до GUI tools.
7. «Что значит термин?» → без pack.
