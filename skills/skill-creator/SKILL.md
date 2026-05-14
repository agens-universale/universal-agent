---
id: "skill-creator"
name: "skill-creator"
description: "Lean workflow for creating and improving skills: capture intent, single-question interview (max 5 questions), draft SKILL.md, two-pass self-check (text-review + run + hybrid review), package в skill-<id>.zip с demo-артефактами. Layer-aware (system/user/project) — for new skills always asks where to put them; for improving existing skills handles the source layer correctly (in-place for project/user, override for system). Use when the user wants to create, draft, improve, refactor, fix, or package a skill — even if they don't explicitly mention 'skill-creator'."
version: "0.2.0"
---

# Skill Creator

A skill for creating new skills and improving existing ones in a lean way.

At a high level, the workflow goes:

1. Identify the mode (new skill vs improve existing) — see `## Branching`.
2. Capture the user's intent at a high level (gather context, propose `skill-id`).
3. **For new skills:** pre-create check (id collision) → choose destination layer → focused single-question interview (max 3 soft / 5 hard) → draft SKILL.md.
4. **For improving existing:** handle the source layer (in-place for project/user, override for system) → focused interview → modify.
5. Self-check (two passes): text-review → опц. fix → run + hybrid review → опц. fix → final run.
6. Package в `skill-<id>.zip` (с README + skill folder + demo/).

This is the lean version. The full eval-loop workflow (parallel runs through
subagent_worker, grading, benchmarking, HTML viewer, iteration loop, blind
comparison, description optimization) is intentionally not included here.
It lives in the experimental branch `experiment/skill-creator-full-workflow`
and may return in a future iteration.

## Communicating with the user

Pay attention to context cues to understand how to phrase your communication.
Users range widely — from non-technical to expert developers. Default to
plain language; introduce jargon only when context shows the user knows it.

It's OK to briefly explain a term if you suspect the user won't recognise it.

## Branching: new vs improve existing

When the user asks for skill work, identify the mode first.

**Triggers for "improve existing" mode** (any of these in the request):
- "доработай / улучши / исправь / измени / дополни"
- "make ... better / fix / refactor / update"
- "в скилле X" / "в моём скилле"
- explicit skill id/name mention

**Otherwise** → "new skill" mode (default).

If ambiguous → ONE ask_human: "Это улучшение существующего скилла или новый?"

### Improve existing — find which one

If user didn't name the skill explicitly:

1. Call `skills_list`, read each skill's description AND `source` field.
2. Match descriptions semantically against the user's intent.
3. **Exactly 1 strong match** → ask_human:
   "Я думаю ты про `<id>` (source: **<system|user|project>**, description: «...»). Это он?"
4. **2+ candidates** → ask_human with all sources visible:
   "Есть несколько кандидатов:
    - `<id1>` (source: system) — description1
    - `<id2>` (source: project) — description2
   Который?"
5. **0 candidates** → ask_human:
   "Не нашёл подходящего скилла. Создаём новый, или ты имел в виду один из: [top-5 by relevance with sources]?"

`source` is ALWAYS shown in the formulations — it's relevant info for the user
(they see the layer and understand the override semantics).

After confirmation: `skills_load(skill_id=<confirmed>)` and proceed to
"Improve existing — handling source layer" below.

### Improve existing — handling source layer

The chosen skill has a `source` field. Behaviour depends on it:

**`source: project`** (skill at `<cwd>/.gc/skills/<id>/`)
- Edit in-place. The skill is in user-controlled project storage, no copy needed.

**`source: user`** (skill at user-data dir, e.g., `~/.local/share/gc/skills/<id>/`)
- Edit in-place. The skill is in user-controlled personal storage.

**`source: system`** (skill bundled with the agent at `<repo>/skills/<id>/`)
- DO NOT edit in-place. The system layer is bundled with the agent and may
  be read-only in production installs. Create an override on a higher layer
  instead:

  1. ask_human: "Скилл `<id>` — встроенный (bundled). Скопировать в:
     - `<cwd>/.gc/skills/<id>/` — для этого проекта (попадёт в его репо)
     - `<user-skills-path>/<id>/` — для всех твоих проектов (where
       `<user-skills-path>` = `~/.local/share/gc/skills` on Linux/Mac or
       `~/.gc/skills` on Windows; use the current platform)
     ?"
  2. Copy the **entire** skill folder (SKILL.md + scripts/ + references/ +
     assets/, including all sub-files) to the chosen target layer. A partial
     copy won't work — registry override is full-replacement, not merge.
  3. Apply improvements to the copy. The override automatically takes
     precedence (project > user > system).
  4. After self-check and finalization, in submit_solution mention to the user:
     - the absolute path where the override now lives;
     - how to revert (delete the override folder; bundled version comes back).

**Prior workspace context.** Workspace `_dev/` transient — после
прошлой упаковки удалён. В новой improve-сессии `_dev/` пустая,
prior context из прошлой разработки не доступен.

Видишь только текущий SKILL.md (через `skills_load`) — это уже
финальная версия прошлой итерации. Это нормально — работаем с чистого
листа: новое интервью по дельте запроса, новый self-check, новая
упаковка.

Если юзеру важно поднять старый контекст (что фиксили, на каких
промптах тестировали) — он может вручную распаковать предыдущий
`skill-<id>.zip` и показать тебе содержимое `demo/` в чате. Это
ad-hoc сценарий, не автоматизируем.

## Creating a skill

### Capture Intent

Start by understanding the user's intent. The current conversation might
already contain a workflow the user wants to capture (e.g., they say "turn
this into a skill"). If so, extract answers from the conversation history
first — the tools used, the sequence of steps, corrections the user made,
input/output formats observed.

For each piece of information not derivable from the conversation, plan to
ask in the interview (see below). Don't ask if the answer is obvious from
context.

### Pre-create check: id collision

Before drafting the new SKILL.md, call `skills_list` and check whether
the chosen `<skill-id>` already exists in any layer (system/user/project).

If yes — single ask_human:

  "Скилл `<id>` уже существует (source: <layer>, description: «...»).
   Что делаешь:
   1. Улучшить существующий — переключусь в режим improve existing
   2. Создать новый с другим id — какой id?
   3. Override-нуть существующей версии (новый SKILL.md полностью с нуля,
      не на основе старой)"

Don't silently override. The user must be aware of the collision and choose
explicitly.

Branch handling:
- Choice 1 → switch to "Improve existing — handling source layer" flow.
- Choice 2 → continue with new id (re-run collision check on the new id).
- Choice 3 → proceed with the original id; the new file becomes an override
  at the user-chosen target layer (next section).

### New skill — choose destination layer

Before creating any files for the new skill, ALWAYS ask the user where to
put it:

  ask_human:
  "Положить скилл `<id>` в:
   - `<cwd>/.gc/skills/<id>/` — для этого проекта (попадёт в его репо)
   - `<user-skills-path>/<id>/` — для всех твоих проектов
   ?"

  where `<user-skills-path>` = `~/.local/share/gc/skills` on Linux/Mac
                              or `~/.gc/skills` on Windows.

If the user picks "проект" → target = `<cwd>/.gc/skills/<id>/`.
If the user picks "все проекты" → target = user-skills-path/<id>/.

Create the directory tree at the target path, then proceed to draft SKILL.md
inside it.

**Note:** the System layer (`<repo>/skills/`) is NEVER a destination for
new skills created by skill-creator in the default flow. It's reserved for
skills bundled with the agent itself.

### Workspace for development artifacts

Все артефакты разработки скилла (брифинг, отчёты review, тестовые
промпты и outputs) пишутся в **подпапку `_dev/` прямо в папке скилла**.

**КРИТИЧЕСКИ ВАЖНО — используй АБСОЛЮТНЫЕ пути.**

Прямо сейчас вычисли и запомни абсолютный путь к workspace, например:

- Для project-layer: `D:\projects\foo\.gc\skills\weather-haiku\_dev\` (Windows)
  или `/home/user/projects/foo/.gc/skills/weather-haiku/_dev/` (Linux/Mac)
- Для user-layer: `C:\Users\<user>\.gc\skills\weather-haiku\_dev\` (Windows)
  или `/home/user/.local/share/gc/skills/weather-haiku/_dev/` (Linux/Mac)

Этот абсолютный путь = `<workspace>` далее по тексту. **Везде, где в
инструкциях встречается `<workspace>/...` или путь начинающийся с
`_dev/...`** — подставляй полный абсолютный путь, не относительный.

**Антипример:** `write_to_file(path="_dev/intent_brief.md", ...)` →
файл попадёт в CWD (project root), НЕ в папку скилла. Это баг.

**Правильно:** `write_to_file(path="<workspace>/intent_brief.md", ...)`
где `<workspace>` — полный абсолютный путь, который ты вычислил выше.

Папка создаётся неявно при первом `write` файла в неё (`make_dirs: True`).

**Lifecycle:** workspace transient. `package_skill.py` удаляет `_dev/`
после успешной упаковки сам. **Между сессиями не сохраняется** —
в следующий раз ты работаешь с чистого листа (текст SKILL.md
доступен через `skills_load`, но артефактов прошлой итерации нет).

Если сессия прервалась без упаковки — `_dev/` остаётся в папке скилла,
юзер видит его и может удалить вручную.

### Interview and Research

Before drafting the SKILL.md, identify what's unclear from the user's
request. Don't ask questions you can answer with reasonable defaults —
propose the default and ask the user to confirm or correct.

**Interview format rules:**

- **One question per `ask_human` call.** TUI doesn't render checkboxes or
  multi-option lists well. Don't bundle 3-4 questions into one prompt.
- **Prefer "I'm planning X — does that match what you want, or different?"**
  formulation when there's a sensible default. Helps the user with context
  and reduces decision fatigue. Plain open-ended phrasing only when defaults
  aren't possible.
- **No checkboxes, no enumerated options.** Examples in parentheses are OK
  ("e.g., JSON array, JSON object with `data` field, plain text — describe
  freely") but no "1./2./3." structures requiring the user to pick one.
- **Limits (только про content-questions):**
  - Target **2-3 questions** (soft).
  - Hard cap **5 questions**. Past 5 you're over-clarifying or asking the
    wrong questions.
  - Prioritize: most consequential ambiguities first (output format, scope,
    critical edge cases). Trivial details default or assume.
  - **Не считается в budget:** служебные вопросы про destination layer
    («куда положить — в проект или для всех проектов?») и про
    разрешение id-коллизии. Это setup-вопросы, не content. После них
    у тебя ещё есть полные 2-3 content-вопроса.
- **Read the user's tone:** if answers shrink to "ok / yes / whatever" —
  stop asking, finalize remaining gaps as `## Assumptions` in the new
  SKILL.md.

Whatever you didn't ask — make a reasonable default and document it in an
`## Assumptions` section of the new skill's SKILL.md. The user can correct
later if any assumption turns out to be wrong.

**После завершения интервью** — запиши `<workspace>/intent_brief.md`
(абсолютный путь, см. секцию `### Workspace for development artifacts`)
со следующим содержимым:

- **Исходный запрос:** 1-2 предложения юзера дословно.
- **Q→A интервью:** все вопросы которые задавал, и ответы юзера.
- **Assumptions:** что зафиксировал как умолчания, если юзер
  переключился в "сам реши" режим или ответы прерывались.

Этот файл попадёт в `demo/` архива при упаковке — даёт команде и
будущему тебе контекст создания скилла.

### Write the SKILL.md

Fill in these components in the new skill's SKILL.md frontmatter:

- **id**: kebab-case identifier (lowercase letters, digits, hyphens). If
  omitted, the registry takes the directory name.
- **name**: human-readable name. May contain UTF/spaces; up to 64 characters.
- **description**: when to trigger, what the skill does. This is the primary
  triggering mechanism — phrase it "pushy", explicitly naming triggers,
  contexts, and "use when..." patterns.
- **version**: optional version string.
- **compatibility**: optional; tooling/dependency requirements (rarely used).

**YAML escaping (важно для `description`):** если значение содержит
любой из символов `:`, `,`, `"`, `'`, `#`, `>`, `|`, `&`, `*`, `?`, `{`,
`}`, `[`, `]` (типично — список триггеров через запятую, или фраза
с двоеточием) — оборачивай **всё значение в двойные кавычки**.
Внутри двойных кавычек используй **одинарные** для inline-цитат
триггеров.

Пример (правильно):
```yaml
description: "Создаёт хайку про погоду. Триггеры: 'хайку про погоду',
  'стих про дождь'. Используй когда нужно poetically описать погоду."
```

Пример (неправильно — package_skill откажет с YAML parse error):
```yaml
description: Создаёт хайку про погоду. Триггеры: "хайку про погоду",
  "стих про дождь".
```

Поле `name` тоже оборачивай в кавычки, если содержит `:` или другие
спец-символы.

### Skill Writing Guide

#### Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (id required by convention; name, description; version optional)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - Executable code for deterministic/repetitive tasks
    ├── references/ - Docs loaded into context as needed
    └── assets/     - Files used in output (templates, icons, fonts)
```

#### Progressive Disclosure

Skills use a three-level loading system:

1. **Metadata** (name + description) — always in context (~100 words)
2. **SKILL.md body** — in context whenever the skill triggers (<500 lines ideal)
3. **Bundled resources** — as needed (unlimited; scripts can execute without loading)

These word counts are approximate; go longer if needed.

**Key patterns:**
- Keep SKILL.md under 500 lines; if approaching the limit, add another layer
  of hierarchy with clear pointers about where the model should go next.
- Reference files clearly from SKILL.md with guidance on when to read them.
- For large reference files (>300 lines), include a table of contents.

**Domain organization:** When a skill supports multiple domains/frameworks,
organize by variant:

```
cloud-deploy/
├── SKILL.md (workflow + selection)
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```

The model reads only the relevant reference file.

#### Principle of Lack of Surprise

Skills must not contain malware, exploit code, or any content that could
compromise system security. A skill's contents should not surprise the user
in their intent if described. Don't go along with requests to create
misleading skills or skills designed to facilitate unauthorized access,
data exfiltration, or other malicious activities. Roleplay skills (e.g.,
"act as an XYZ") are OK.

#### Writing Patterns

Prefer the imperative form in instructions.

**Defining output formats** — use a clear template:

```markdown
## Report structure
ALWAYS use this exact template:
# [Title]
## Executive summary
## Key findings
## Recommendations
```

**Examples pattern** — include input/output examples:

```markdown
## Commit message format
**Example 1:**
Input: Added user authentication with JWT tokens
Output: feat(auth): implement JWT-based authentication
```

### Writing Style

Explain **why** things matter instead of relying on heavy-handed MUSTs. Use
theory of mind — frame the instructions so the model understands the
reasoning. Make the skill general and not super-narrow to specific examples.
Start with a draft and then look at it with fresh eyes to improve.

### Self-check before finalizing

Self-check состоит из двух пасс — дешёвая статика (text-review) и
эмпирическая (run + hybrid review). Цель: поймать и structural-пробелы
в тексте SKILL.md, и output-quality issues при реальной генерации.

Все файлы артефактов пишутся в `<workspace>/...` (абсолютный путь, см.
`### Workspace for development artifacts`). Запомни этот путь сейчас —
он будет использоваться во ВСЕХ шагах ниже.

**Обязательный минимум артефактов в `_dev/`** (программно проверяется
`package_skill.py` — без этих файлов упаковка отказывает):

- `intent_brief.md`
- `completion_report.md` (Step 5)
- `examples/prompt_01.md` + `prompt_01_output.md`
- `examples/prompt_02.md` + `prompt_02_output.md`

**Best-effort (опционально)** — пиши если считаешь полезным для
будущих improve-сессий, но не обязательно:

- `text_review_report.md` (отчёт Step 1)
- `self_check_report.md` (отчёт Step 3)

Без этих best-effort файлов упаковка не падает. Содержимое
review-выводов всё равно отражается в `completion_report.md` (секция
«Эволюция версий» — что фиксили).

**Общие правила для всех subagent_worker вызовов в self-check:**

В каждый task, который ты передаёшь субагенту:
1. **Передавай абсолютный путь к SKILL.md** (например,
   `D:\projects\foo\.gc\skills\<id>\SKILL.md`), не относительный.
2. **Передавай абсолютные пути ко всем файлам**, которые субагент должен
   прочитать (output-ы и т.п.).
3. **Требуй явно: «Верни итоговый результат в поле `task_report`
   через `submit_solution`. НЕ создавай файлов на диске
   (`write_to_file`, `run_python_code` с file output и т.д.). НЕ
   создавай `exec_report_*.md`. В handoff `artifacts_created` оставь
   пустым массивом — файлов мы от тебя не ждём».** Без этой инструкции
   subagent_worker по умолчанию создаёт `exec_report_<step>.md` в
   project root и плодит другие side-файлы (включая promotional
   `*.py` скрипты для validation/utility — это тоже не нужно).
4. Если субагенту нужно что-то прочитать с диска — указывай абсолютный
   путь явно в task.

**Step 1 — Text-review (subagent_worker × 1).**

Подбери 2 реалистичных промпта (один — простая история с одним героем,
второй с дуэтом/расширенным контекстом, чтобы покрыть разные ветки).
Запиши их по **абсолютным** путям:
- `<workspace>/examples/prompt_01.md`
- `<workspace>/examples/prompt_02.md`

Вызови `subagent_worker × 1` с задачей примерно:

> «Прочитай SKILL.md по абсолютному пути `<абс-путь к SKILL.md>`.
> Мысленно примени его к двум промптам, которые я тебе сейчас передам
> в этом task (вот они ниже). Верни отчёт о structural-пробелах: что в
> инструкциях нечётко, какие edge cases не покрыты, есть ли противоречия.
>
> Промпт 1: <содержимое prompt_01.md>
> Промпт 2: <содержимое prompt_02.md>
>
> Верни отчёт через submit_solution в поле `task_report`. НЕ создавай файлов на
> диске. НЕ создавай exec_report — мы запишем твой отчёт сами.»

Получив ответ субагента через submit_solution — применяй его рекомендации
к SKILL.md. Запись `<workspace>/text_review_report.md` — **опциональна**
(best-effort): пиши если хочешь зафиксировать историю для будущих
improve-сессий; если решил не писать — содержимое отчёта всё равно
отразится в Step 5 (completion_report, секция «Эволюция версий»).

Если рецензент нашёл проблемы → правь SKILL.md (можешь сразу несколько
рекомендаций применить). Если он сказал «ok» — переходи к Step 2 без
правок.

**Step 2 — Run на 2 промптах (subagent_worker × 2).**

Для каждого из 2 промптов вызови отдельный `subagent_worker`. В task
каждому передавай:

- **Полный текст текущего SKILL.md** (читай SKILL.md и вставляй inline
  в task; не давай ссылку — нужен реальный текст).
- **Один промпт пользователя** из `<workspace>/examples/prompt_NN.md`.
- Инструкцию примерно:

> «Применяй этот SKILL.md как свои инструкции для этого запроса. Не
> задавай уточняющих вопросов — делай reasonable defaults. Верни
> итоговый артефакт скилла (то что юзер увидел бы) через submit_solution
> в поле `task_report`. **НЕ создавай файлов на диске** (никаких `*.md`,
> `*.py` или других side-файлов). НЕ создавай exec_report. Только
> submit_solution с финальным артефактом в task_report.»

Получив ответы от обоих субагентов через submit_solution, **сам запиши их** по
абсолютным путям:
- `<workspace>/examples/prompt_01_output.md`
- `<workspace>/examples/prompt_02_output.md`

Если субагент упал или вернул мусор — сохрани результат с пометкой в
начале файла: `[degraded output: <причина>]`. Не блокируй процесс.

**Step 3 — Hybrid review (subagent_worker × 1).**

Вызови `subagent_worker × 1`. В task передавай:

- **Абсолютный путь к SKILL.md.**
- **Абсолютные пути к обоим output-файлам** (`<workspace>/examples/prompt_01_output.md`
  и `<workspace>/examples/prompt_02_output.md`).
- Инструкцию примерно:

> «Прочитай SKILL.md по пути `<абс-путь>` и два output-файла по путям
> `<абс-путь-1>`, `<абс-путь-2>`. Критикуй и текст инструкций, и качество
> реальных выходов. Привязывай находки к конкретным местам в outputs.
> Возвращай: что фиксить в SKILL.md vs что является ограничением модели
> или промпта.
>
> Верни отчёт через submit_solution в поле `task_report`. НЕ создавай файлов на
> диске. НЕ создавай exec_report.»

Получив ответ через submit_solution — применяй рекомендации к SKILL.md.
Запись `<workspace>/self_check_report.md` — **опциональна** (best-effort,
как и Step 1 report). Если хочешь сохранить — пиши; если нет — содержимое
отразится в Step 5 completion_report.

Если есть проблемы для фикса в SKILL.md:
1. **Применяй фиксы глобально**, не точечно. Если меняешь термин или
   формулировку — пройди весь SKILL.md и проверь, что термин использован
   consistent во всех секциях (включая блоки примеров `\`\`\`markdown ...
   \`\`\``). После замен — обязательно re-read SKILL.md целиком и убедись,
   что нет старого варианта термина / противоречий.
2. После применения фиксов — **переходи к Step 4** (final run).
   **Не упаковывай скилл без Step 4** — иначе outputs в demo/ будут
   соответствовать version-before-fix, а не shipped SKILL.md
   (десинхронизация).

Если hybrid review сказал «ok» (фиксов нет) — outputs из Step 2 уже
финальные, переходи к Step 5 (completion report).

**Step 4 — Final run (subagent_worker × 2).**

**Обязательный шаг, если в Step 3 были применены фиксы.** Пропустить
Step 4 = ship outputs от устаревшей версии SKILL.md.

Повторно для каждого из 2 промптов вызови `subagent_worker` по тем же
правилам, что и Step 2 (полный SKILL.md inline в task — **из обновлённого
файла**, не из памяти; инструкция «верни через submit_solution, не
создавай файлов»). Перезапиши **сам** по абсолютным путям:
- `<workspace>/examples/prompt_01_output.md`
- `<workspace>/examples/prompt_02_output.md`

**Step 5 — Completion report (обязательный шаг).**

Сразу после Step 4 (или после Step 3 если фиксов не было) — запиши
`<workspace>/completion_report.md`. Это финальное «слово агента» о
проделанной работе: команда, открывающая архив, должна понять что
сделали, что работает, что осталось ограничением и что можно
улучшить в будущей итерации.

**Структура файла (строго следуй):**

```markdown
# Completion Report — <skill-id>

**Финальная версия:** v0.1.X
**Дата завершения:** YYYY-MM-DD
**Итераций self-check:** N (text-review × 1 + hybrid review × 1
[+ final run × 2 если был fix])

## Что сделано

<1-2 предложения: что скилл делает, ключевые фичи.>

## Эволюция версий

- **v0.1.0** (draft): начальная версия с базовым алгоритмом.
- **v0.1.1** (после text-review): конкретные правки структурного
  плана (1-2 строки на фикс). Например:
  - добавил syllable validation шаг;
  - убрал штампы из примеров;
  - переписал триггеры явно.
- **v0.1.2** (после hybrid review + final run): конкретные правки
  по output-quality. Если фиксов после hybrid не потребовалось —
  пиши «фиксов не потребовалось, outputs Step 2 финальные».

## Accepted limitations

<Вещи которые НЕ идеальны, но сознательно оставлены — это известные
ограничения модели или scope-decision. Например:
"Точный подсчёт русских слогов — модель часто ошибается на 1; не
фиксим в SKILL.md, полагаемся на алгоритм пересчёта в выходе."
Если ничего такого нет — пиши «Нет.».>

## Открытые вопросы для будущих итераций

<Что можно улучшить, но не блокер сейчас. TODO-лист для будущих
сессий. Например:
- Добавить поддержку английского как опции.
- Расширить anti-cliché правило примерами before/after.
Если ничего — пиши «Нет.».>

## Подтверждение готовности

- [x] Все артефакты в `_dev/` на месте (см. Step 6 checklist ниже).
- [x] Final run выполнен на финальной версии (если был fix в Step 3),
      ИЛИ outputs из Step 2 финальные (если фиксов после hybrid не было).
- [x] Outputs соответствуют shipped SKILL.md.
```

**Правила заполнения (важно):**

1. **Эволюция версий** — НЕ ленись общими формулировками. Возьми
   конкретный список фиксов из контекста сессии (review-отчёты
   субагентов через task_report, или твои own files если ты их сохранил
   в `text_review_report.md` / `self_check_report.md`). Перечисляй
   конкретно («добавил syllable validation шаг», «убрал штамп
   `капли стучат` из примеров»), не «исправил пробелы».

2. **Accepted limitations** — критичная секция для **improve-existing**
   в будущем. Если модель плохо что-то делает и ты решил это принять
   как known limitation, а не фиксить в SKILL.md — обязательно запиши.
   Без этого следующая сессия будет тратить итерацию на ту же проблему.

3. **Открытые вопросы** — пиши кратко, без объяснений. TODO-лист для
   следующих итераций. Если по содержанию всё ок — честно пиши «Нет.».

4. **НЕ пиши пустых отчётов** типа «всё хорошо, готов к распространению».
   Если в секции реально нечего сказать — пиши «Нет.» и переходи дальше.
   Полстраницы template-prose без конкретики бесполезны.

**Step 6 — Package (финальный шаг сессии, обязательный).**

После Step 5 **сам вызови `package_skill.py`** (через `skills_run_script`
или `execute_command` — см. секцию `### Package` ниже). Это финальный
шаг — **не оставляй упаковку юзеру**, делай сам.

`package_skill.py` при вызове проверяет наличие required-артефактов в
`_dev/`. Если чего-то нет — упаковка отказывает с понятным сообщением,
перечисляя missing files. В этом случае вернись, доделай недостающее, и
вызови упаковку снова.

После успешной упаковки → `submit_solution` с абсолютным путём к
получившемуся `skill-<id>.zip`.

Финальные `<workspace>/examples/prompt_NN_output.md` соответствуют
финальной версии SKILL.md (либо Step 2 если фиксов не было, либо
Step 4 если были).

**Жёсткий cap:** каждый тип review — ровно 1 раз за сессию. НЕ
запускай:
- Второй text-review после фикса по первому. Hybrid review это
  обнаружит при необходимости.
- Второй hybrid review после Step 4. Если решил фиксить — доверяй
  final run.

Превышение лимита = overfitting: модель начинает подстраиваться под рецензента, а не улучшать скилл.

**Fallback (нет `subagent_worker`).** Каждый шаг сам отыграй mental
walk-through. Опциональные отчёты (text_review, self_check) если пишешь
— с пометкой `[fallback: no subagent]` в начале файла. Step 2/4 (runs)
скипаются — но `examples/prompt_NN.md` + `prompt_NN_output.md`
**обязательны для упаковки**, поэтому запиши промпты и в output-файлы
пиши `[skipped: no subagent for run — outputs not generated]`. Step 5
(completion_report.md) делаешь как обычно — обязателен; в секциях
"Эволюция версий" опиши свои mental walk-through итерации. Все пути —
**абсолютные**.

### Package

Когда self-check завершён и SKILL.md финальный, упакуй скилл.

**Предпочитаемый способ — через `skills_run_script`** (работает кросс-
платформенно, без проблем с кодировкой Windows):

```
skills_run_script(
    skill_id='skill-creator',
    script_name='package_skill.py',
    args={
        'skill_path': '<абсолютный путь к папке скилла>',
        'output_dir': '<абсолютный путь куда положить архив>',
        'source_layer': 'project'  # или 'user', в зависимости от выбора
    }
)
```

Все три параметра — обязательны для предсказуемости. `source_layer` —
тот слой, который юзер выбрал в начале сессии.

**Fallback через `execute_command`** (если `skills_run_script` недоступен
или нужно вручную):

```bash
python -m scripts.package_skill <abs-path-to-skill-folder> --source-layer project --output-dir <abs-path>
```

С `cwd: <абсолютный путь к корню репо>/skills/skill-creator`. На Windows
если видишь UnicodeEncodeError на emoji в выводе — добавь
`set PYTHONIOENCODING=utf-8 && ` перед командой, либо просто используй
основной `skills_run_script` способ выше.

**НЕ используй** `python <abs>/skills/skill-creator/scripts/package_skill.py
<args>` — у пакета `skill-creator` дефис в имени, `-m` его не разрешит,
а прямой запуск файла создаёт sys.path проблемы.

Скрипт:
1. Валидирует skill-folder (`quick_validate`).
2. Создаёт `skill-<id>.zip` в `--output-dir` (по умолчанию CWD).
3. Внутри архива: `README.md` (генерируется автоматически), `<id>/...`
   (содержимое skill-folder без `_dev/`), `demo/...` (содержимое
   `_dev/`).
4. **Удаляет `_dev/`** из skill-folder после успешной упаковки.

В `submit_solution` юзеру: сообщи **абсолютный путь к архиву**
(`skill-<id>.zip`). Workspace уже удалена — упоминать её не нужно.

**Тонкость re-pack.** Если вызвать `package_skill.py` второй раз
на том же скилле — `_dev/` уже нет, в архиве не будет `demo/`.
Это сознательное поведение: для полной пересборки с demo нужна
новая сессия скилл-креатора. Для переименования архива — обычное
`mv` без повторного скрипта.

## Our agent environment (gc-cli)

This skill runs inside the `gc-cli` agent. Notable differences from upstream:

- **Subagents:** only `subagent_worker(reason, task)` is available (see
  `src/tools/subagents/worker.py`). Use it whenever you need an independent
  LLM session (e.g., the self-check above). Do NOT simulate subagents by
  writing a single Python script that runs both with-skill and baseline in
  the same LLM session — that's the same model evaluating its own output,
  which defeats the purpose.
- **Headless:** no browser, no GUI. Any visual review by the user is via
  files on disk (the user opens them in their own tools).
- **No CLI-based description-optimization tooling.** The upstream
  description-tuning flow isn't bundled here; it lives in
  `experiment/skill-creator-full-workflow`.
- **Three-layer skills model:** the registry merges system / user / project
  layers with priority `project > user > system`. The skill-creator is
  layer-aware — see `## Branching` and `### New skill — choose destination
  layer` above.
- **Skills tools:** prefer `skills_list`, `skills_load`, `skills_run_script`,
  `skills_load_reference`, `skills_load_asset` for any skill operation.
  Don't shell out to scripts directly when `skills_run_script` does the same.
- **package_skill is portable:** invoke it directly from any CWD; the script
  inserts its own directory into `sys.path` for sibling-import resolution.
  Eval artefacts under `.eval_workspaces/` are excluded from `skill-<id>.zip`
  archives automatically.
