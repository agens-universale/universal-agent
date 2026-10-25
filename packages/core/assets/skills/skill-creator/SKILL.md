---
id: "skill-creator"
name: "skill-creator"
description: "Lean workflow for creating and improving skills: capture intent, single-question interview (max 5 questions), draft SKILL.md, two-pass self-check (text-review + run + hybrid review), package as skill-<id>.zip with demo artifacts. Layer-aware (system/user/project) — for new skills always asks where to put them; for improving existing skills handles the source layer correctly (in-place for project/user, override for system). Use when the user wants to create, draft, improve, refactor, fix, or package a skill — even if they don't explicitly mention 'skill-creator'."
version: "0.2.0"
---

# Skill Creator

A skill for creating new skills and improving existing ones in a lean way.

**Tool-agnostic wording:** describe actions by role (filesystem write, human
clarification, finish handoff, delegated subagent, shell invocation). Name
only `skills_*` tools explicitly — see `supercoder:writing-skills`.

At a high level, the workflow goes:

1. Identify the mode (new skill vs improve existing) — see `## Branching`.
2. Capture the user's intent at a high level (gather context, propose `skill-id`).
3. **For new skills:** pre-create check (id collision) → choose destination layer → focused single-question interview (max 3 soft / 5 hard) → draft SKILL.md.
4. **For improving existing:** handle the source layer (in-place for project/user, override for system) → focused interview → modify.
5. Self-check (two passes): text-review → optional fix → run + hybrid review → optional fix → final run.
6. Package as `skill-<id>.zip` (with README + skill folder + demo/).

This is the lean version. The full eval-loop workflow (parallel runs through
delegated subagent runs, grading, benchmarking, HTML viewer, iteration loop, blind
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

If ambiguous → ONE human clarification: "Это улучшение существующего скилла или новый?"

### Improve existing — find which one

If user didn't name the skill explicitly:

1. Call `skills_list`, read each skill's description AND `source` field.
2. Match descriptions semantically against the user's intent.
3. **Exactly 1 strong match** → human clarification:
   "Я думаю ты про `<id>` (source: **<system|user|project>**, description: «...»). Это он?"
4. **2+ candidates** → human clarification with all sources visible:
   "Есть несколько кандидатов:
    - `<id1>` (source: system) — description1
    - `<id2>` (source: project) — description2
   Который?"
5. **0 candidates** → human clarification:
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

**`source: system`** (skill bundled with the agent, in the distribution's own
`skills/` root: `packages/core/assets/skills/<id>/` in the working copy,
`gc_core/assets/skills/<id>/` in an installed wheel)
- DO NOT edit in-place. The system layer is bundled with the agent and may
  be read-only in production installs. Create an override on a higher layer
  instead:

  1. human clarification: "Скилл `<id>` — встроенный (bundled). Скопировать в:
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
  4. After self-check and finalization, in finish handoff mention to the user:
     - the absolute path where the override now lives;
     - how to revert (delete the override folder; bundled version comes back).

**Prior workspace context.** The `_dev/` workspace is transient — it
was deleted by the previous packaging. In a new improve session the
`_dev/` folder is empty; prior context from earlier development is not
available.

You only see the current SKILL.md (via `skills_load`) — already the
final version of the previous iteration. That's fine — work from a
clean slate: a fresh delta-interview, a fresh self-check, a fresh
packaging.

If the user wants to bring up older context (what was fixed, which
prompts were tested) — they can unpack the previous `skill-<id>.zip`
manually and show you the `demo/` contents in chat. That's an ad-hoc
scenario, not something we automate.

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

If yes — single human clarification:

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

  human clarification:
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

**Note:** the System layer (the bundled `skills/` root above) is NEVER a
destination for new skills created by skill-creator in the default flow. It's reserved for
skills bundled with the agent itself.

### Workspace for development artifacts

All development artifacts of the skill (intent brief, review reports,
test prompts, and outputs) are written to a **`_dev/` subfolder inside
the skill folder itself**.

**CRITICAL — use ABSOLUTE paths.**

Right now compute and remember the absolute workspace path. For example:

- For project-layer: `D:\projects\foo\.gc\skills\weather-haiku\_dev\` (Windows)
  or `/home/user/projects/foo/.gc/skills/weather-haiku/_dev/` (Linux/Mac)
- For user-layer: `C:\Users\<user>\.gc\skills\weather-haiku\_dev\` (Windows)
  or `/home/user/.local/share/gc/skills/weather-haiku/_dev/` (Linux/Mac)

This absolute path = `<workspace>` throughout the rest of this document.
**Wherever you see `<workspace>/...` or a path starting with `_dev/...`**,
substitute the full absolute path, not a relative one.

**Anti-example:** writing to a relative `_dev/intent_brief.md` path from
project CWD → the file lands in the project root, NOT in the skill folder.

**Correct:** filesystem write to the full absolute path
`<workspace>/intent_brief.md`, where `<workspace>` is the path you computed
above.

The folder is created implicitly on the first `write` into it
(`make_dirs: True`).

**Lifecycle:** the workspace is transient. `package_skill.py` deletes
`_dev/` after a successful packaging itself. **It is not preserved
between sessions** — on the next run you start from a clean slate
(the SKILL.md text is still available via `skills_load`, but no
artifacts from prior iterations).

If the session is interrupted before packaging — `_dev/` remains in
the skill folder; the user can see it and remove it manually.

### Interview and Research

Before drafting the SKILL.md, identify what's unclear from the user's
request. Don't ask questions you can answer with reasonable defaults —
propose the default and ask the user to confirm or correct.

**Interview format rules:**

- **One question per human-clarification turn.** TUI doesn't render checkboxes or
  multi-option lists well. Don't bundle 3-4 questions into one prompt.
- **Prefer "I'm planning X — does that match what you want, or different?"**
  formulation when there's a sensible default. Helps the user with context
  and reduces decision fatigue. Plain open-ended phrasing only when defaults
  aren't possible.
- **No checkboxes, no enumerated options.** Examples in parentheses are OK
  ("e.g., JSON array, JSON object with `data` field, plain text — describe
  freely") but no "1./2./3." structures requiring the user to pick one.
- **Limits (content questions only):**
  - Target **2-3 questions** (soft).
  - Hard cap **5 questions**. Past 5 you're over-clarifying or asking the
    wrong questions.
  - Prioritize: most consequential ambiguities first (output format, scope,
    critical edge cases). Trivial details default or assume.
  - **Not counted toward the budget:** setup questions about the destination layer
    (“куда положить — в проект или для всех проектов?”) and resolving
    ID collisions. These are setup questions, not content questions. After them,
    you still have the full 2-3 content questions available.
- **Read the user's tone:** if answers shrink to "ok / yes / whatever" —
  stop asking, finalize remaining gaps as `## Assumptions` in the new
  SKILL.md.

Whatever you didn't ask — make a reasonable default and document it in an
`## Assumptions` section of the new skill's SKILL.md. The user can correct
later if any assumption turns out to be wrong.

**Once the interview is finished** — write `<workspace>/intent_brief.md`
(absolute path, see `### Workspace for development artifacts`) with the
following content:

- **Original request:** 1-2 sentences quoting the user verbatim.
- **Q→A interview:** every question you asked and the user's answer.
- **Assumptions:** the defaults you fixed if the user switched into
  "you decide" mode or stopped engaging.

This file lands in `demo/` of the final archive — it gives the team
and future-you the context behind why the skill exists.

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

**YAML escaping (matters for `description`):** if the value contains
any of `:`, `,`, `"`, `'`, `#`, `>`, `|`, `&`, `*`, `?`, `{`, `}`,
`[`, `]` (typically — a comma-separated list of triggers, or a phrase
with a colon) — wrap the **whole value in double quotes**. Inside
double quotes use **single quotes** for inline trigger citations.

Example (correct):
```yaml
description: "Создаёт хайку про погоду. Триггеры: 'хайку про погоду',
  'стих про дождь'. Используй когда нужно poetically описать погоду."
```

Example (incorrect — `package_skill` will refuse with a YAML parse error):
```yaml
description: Создаёт хайку про погоду. Триггеры: "хайку про погоду",
  "стих про дождь".
```

Wrap the `name` field in quotes too if it contains `:` or other
special characters.

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

Self-check has two passes — a cheap static one (text-review) and an
empirical one (run + hybrid review). Goal: catch both structural gaps
in SKILL.md text and output-quality issues at real generation time.

All artifact files are written to `<workspace>/...` (absolute path,
see `### Workspace for development artifacts`). Remember this path now —
it is used in ALL the steps below.

**Mandatory minimum of artifacts in `_dev/`** (programmatically enforced
by `package_skill.py` — without these files packaging is refused):

- `intent_brief.md`
- `completion_report.md` (Step 5)
- `examples/prompt_01.md` + `prompt_01_output.md`
- `examples/prompt_02.md` + `prompt_02_output.md`

**Best-effort (optional)** — write if you find it useful for future
improve sessions, but not required:

- `text_review_report.md` (Step 1 report)
- `self_check_report.md` (Step 3 report)

Packaging does not refuse on missing best-effort files. The substance
of review findings is reflected in `completion_report.md` anyway
(section "Эволюция версий" — what was fixed).

**Common rules for every delegated subagent run in self-check:**

In every task you pass to a subagent:

1. **Pass an absolute path to SKILL.md** (for example,
   `D:\projects\foo\.gc\skills\<id>\SKILL.md`), not a relative one.
2. **Pass absolute paths to every file** the subagent has to read
   (outputs and so on).
3. **Require explicitly: "Return the final result in the `task_report`
   field via finish handoff (do not write deliverables to disk yourself).
   Do NOT create files on disk (filesystem writes, Python execution with
   file output, etc.). Do NOT create exec sidecar reports (`exec_report_*.md`).
   Leave `artifacts` in the handoff as an empty array — we don't
   expect files from you."** Without this line, a delegated subagent often
   defaults to creating `exec_report_<step>.md`
   in the project root and emits other side files (including auxiliary
   `*.py` scripts for validation/utility — also unwanted).
4. If the subagent has to read something from disk — give the absolute
   path explicitly in the task.

**Step 1 — Text-review (one delegated subagent).**

Pick 2 realistic prompts (one — a simple single-character case, the
second — with a duet or extended context, to cover different branches).
Save them to **absolute** paths:
- `<workspace>/examples/prompt_01.md`
- `<workspace>/examples/prompt_02.md`

Invoke one delegated subagent with a task roughly:

> "Read SKILL.md at the absolute path `<abs-path-to-SKILL.md>`. Mentally
> apply it to two prompts that I'm passing inline in this task (they
> are below). Return a report on structural gaps: where the instructions
> are unclear, which edge cases are not covered, whether there are
> contradictions.
>
> Prompt 1: <contents of prompt_01.md>
> Prompt 2: <contents of prompt_02.md>
>
> Return the report via finish handoff in the `task_report` field. Do
> NOT create files on disk. Do NOT create exec_report — we will save
> your report ourselves."

Once the subagent returns its task_report — apply its recommendations
to SKILL.md. Saving `<workspace>/text_review_report.md` is **optional**
(best-effort): write it if you want to preserve history for future
improve sessions; if you don't, the substance still ends up in Step 5
(completion_report, "Эволюция версий" section).

If the reviewer found problems → fix SKILL.md (you can apply several
recommendations in one pass). If it said "ok" — proceed to Step 2
without edits.

**Step 2 — Run on 2 prompts (two delegated subagents).**

For each of the 2 prompts invoke a separate delegated subagent. In each
task pass:

- **The full text of the current SKILL.md** (read SKILL.md and inline
  it into the task — don't give a reference, the real text is needed).
- **One user prompt** from `<workspace>/examples/prompt_NN.md`.
- An instruction roughly:

> "Apply this SKILL.md as your instructions for this request. Do not
> ask clarifying questions — make reasonable defaults. Return the
> final artifact of the skill (what the user would see) via
> finish handoff in the `task_report` field. **Do NOT create files on
> disk** (no `*.md`, `*.py`, or other side files). Do NOT create
> exec sidecar reports. Only finish handoff with the final artifact in
> task_report."

Once both subagents return their task_reports — save the contents
yourself to absolute paths:
- `<workspace>/examples/prompt_01_output.md`
- `<workspace>/examples/prompt_02_output.md`

If a subagent failed or returned garbage — save the result with a tag
at the top of the file: `[degraded output: <reason>]`. Don't block the
process.

**Step 3 — Hybrid review (one delegated subagent).**

Invoke one delegated subagent. In the task pass:

- **The absolute path to SKILL.md.**
- **Absolute paths to both output files** (`<workspace>/examples/prompt_01_output.md`
  and `<workspace>/examples/prompt_02_output.md`).
- An instruction roughly:

> "Read SKILL.md at `<abs-path>` and the two output files at
> `<abs-path-1>`, `<abs-path-2>`. Criticise both the instruction text
> and the quality of the actual outputs. Tie findings to specific
> locations in the outputs. Return: what to fix in SKILL.md vs what is
> a model or prompt limitation.
>
> Return the report via finish handoff in the `task_report` field. Do
> NOT create files on disk. Do NOT create exec_report."

Once you get the response — apply the recommendations to SKILL.md.
Saving `<workspace>/self_check_report.md` is **optional** (best-effort,
like the Step 1 report). If you want to preserve it — save; if not —
the substance ends up in Step 5 completion_report.

If there are problems to fix in SKILL.md:
1. **Apply fixes globally**, not pointwise. If you're changing a term
   or wording — walk through the entire SKILL.md and confirm the term
   is used consistently in every section (including code-block
   examples inside ` ```markdown ... ``` `). After the replacements —
   re-read SKILL.md cover to cover and confirm there's no old variant
   of the term, no contradictions.
2. After applying fixes — **proceed to Step 4** (final run).
   **Do not package the skill without Step 4** — otherwise outputs in
   demo/ will correspond to version-before-fix, not the shipped
   SKILL.md (desync).

If hybrid review said "ok" (no fixes) — Step 2 outputs are already
final, proceed to Step 5 (completion report).

**Step 4 — Final run (two delegated subagents).**

**Mandatory step if Step 3 applied any fixes.** Skipping Step 4 = ship
outputs from an outdated version of SKILL.md.

Repeat the calls for each of the 2 prompts following the same rules as
Step 2 (full SKILL.md inline in the task — **from the updated file**,
not from memory; instruction "return via finish handoff, do not
create files"). Overwrite **yourself** at the absolute paths:
- `<workspace>/examples/prompt_01_output.md`
- `<workspace>/examples/prompt_02_output.md`

**Step 5 — Completion report (mandatory).**

Load the template via
`skills_read_asset(skill_id='skill-creator', slug='completion_report_template')`.
The asset contains the full structure (5 sections) and detailed
filling rules — read them and fill in every section. If a section has
nothing meaningful — write "Нет." rather than skipping. Save the result
to `<workspace>/completion_report.md`.

**MUST call `skills_read_asset` first; do not write the template from
memory** — the asset is the single source of truth for the template
structure and Russian wording.

**Step 6 — Package (final session step, mandatory).**

After Step 5 **invoke `package_skill.py` yourself** (via
`skills_run_script` or a shell invocation — see the `### Package`
section below). This is the final step — **don't leave packaging to
the user**, do it yourself.

When invoked, `package_skill.py` checks the required artifacts in
`_dev/`. If anything is missing — packaging is refused with a clear
message listing the missing files. In that case go back, finish the
missing pieces, and invoke packaging again.

After a successful packaging → finish handoff to the user with the absolute
path to the produced `skill-<id>.zip`.

Final `<workspace>/examples/prompt_NN_output.md` files correspond to
the final version of SKILL.md (either Step 2 if no fixes were applied,
or Step 4 if there were any).

**Hard cap:** each review type — exactly once per session. Do NOT
invoke:
- A second text-review after the first fix. Hybrid review will catch
  what's left if anything.
- A second hybrid review after Step 4. If you decided to fix —
  trust the final run.

Going past the limit = overfitting: the model starts adapting to the
reviewer rather than improving the skill.

**Fallback (no delegated subagent available).** Walk through every
step in your head yourself (mental walk-through). Optional reports
(text_review, self_check) — if you do write them, prefix with
`[fallback: no subagent]` at the top. Step 2/4 (runs) are skipped —
but `examples/prompt_NN.md` + `prompt_NN_output.md` are **required for
packaging**, so save the prompts and in each output file write
`[skipped: no subagent for run — outputs not generated]`. Step 5
(completion_report.md) is done as usual — required regardless of
subagent availability; in "Эволюция версий" describe your mental
walk-through iterations. All paths — **absolute**.

### Package

Once self-check is finished and SKILL.md is final, package the skill.

**Preferred — via `skills_run_script`** (cross-platform, no Windows
encoding issues):

```
skills_run_script(
    skill_id='skill-creator',
    script_name='package_skill.py',
    args={
        'skill_path': '<absolute path to the skill folder>',
        'output_dir': '<absolute path where to put the archive>',
        'source_layer': 'project'  # or 'user', depending on the choice
    }
)
```

All three parameters are required for predictability. `source_layer`
is the layer the user picked at the start of the session.

**Fallback via shell invocation** (if `skills_run_script` is not
available or you want to invoke manually):

```bash
python -m scripts.package_skill <abs-path-to-skill-folder> --source-layer project --output-dir <abs-path>
```

with `cwd` = this skill's own folder — the `root_path` that `skills_load`
reports for `skill-creator` (the directory holding this SKILL.md). On
Windows, if you see UnicodeEncodeError on emoji in the output — prefix
the command with `set PYTHONIOENCODING=utf-8 && `, or just use the
`skills_run_script` route above.

**Do NOT use** `python <abs-path-to-this-skill>/scripts/package_skill.py
<args>` — the `skill-creator` package name contains a dash, `-m` won't
resolve it as a Python identifier, and direct file invocation creates
sys.path problems.

The script:
1. Validates the skill folder (`quick_validate`).
2. Creates `skill-<id>.zip` in `--output-dir` (CWD by default).
3. Inside the archive: `README.md` (auto-generated), `<id>/...` (skill
   folder contents excluding `_dev/`), `demo/...` (contents of `_dev/`).
4. **Deletes `_dev/`** from the skill folder after successful packaging.

In the finish handoff to the user: report the **absolute path to the
archive** (`skill-<id>.zip`). The workspace is already removed, no need
to mention it.

**Re-pack caveat.** If you call `package_skill.py` a second time on
the same skill — `_dev/` is already gone, so the archive will not
contain `demo/`. This is intentional: for a full re-package with demo
you need a new skill-creator session. For renaming an archive — a
plain `mv` without re-running the script.

## Our agent environment (gc-cli)

This skill runs inside the `gc-cli` agent. Notable differences from upstream:

- **Subagents:** only the delegated subagent tool is available (reason +
  task parameters; see
  `packages/core/src/gc_core/tools/subagents/worker.py`). Use it whenever you
  need an independent LLM session (e.g., the self-check above). Do NOT simulate subagents by
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
  `skills_read_reference`, `skills_read_asset` for any skill operation.
  Don't shell out to scripts directly when `skills_run_script` does the same.
- **package_skill is portable:** invoke it directly from any CWD; the script
  inserts its own directory into `sys.path` for sibling-import resolution.
  Eval artefacts under `.eval_workspaces/` are excluded from `skill-<id>.zip`
  archives automatically.
