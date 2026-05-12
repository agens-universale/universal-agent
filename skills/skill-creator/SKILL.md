---
id: "skill-creator"
name: "skill-creator"
description: "Lean workflow for creating and improving skills: capture intent, single-question interview (max 5 questions), draft SKILL.md, lightweight self-check via subagent_worker (max 2 rounds), optional package. Layer-aware (system/user/project) — for new skills always asks where to put them; for improving existing skills handles the source layer correctly (in-place for project/user, override for system). Use when the user wants to create, draft, improve, refactor, fix, or package a skill — even if they don't explicitly mention 'skill-creator'."
version: "0.2.0"
---

# Skill Creator

A skill for creating new skills and improving existing ones in a lean way.

At a high level, the workflow goes:

1. Identify the mode (new skill vs improve existing) — see `## Branching`.
2. Capture the user's intent at a high level (gather context, propose `skill-id`).
3. **For new skills:** pre-create check (id collision) → choose destination layer → focused single-question interview (max 3 soft / 5 hard) → draft SKILL.md.
4. **For improving existing:** handle the source layer (in-place for project/user, override for system) → focused interview → modify.
5. Self-check via ONE subagent_worker (max 2 rounds of review+fix).
6. Optional: package into `.skill` archive for distribution.

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
- **Limits:**
  - Target **2-3 questions** (soft).
  - Hard cap **5 questions**. Past 5 you're over-clarifying or asking the
    wrong questions.
  - Prioritize: most consequential ambiguities first (output format, scope,
    critical edge cases). Trivial details default or assume.
- **Read the user's tone:** if answers shrink to "ok / yes / whatever" —
  stop asking, finalize remaining gaps as `## Assumptions` in the new
  SKILL.md.

Whatever you didn't ask — make a reasonable default and document it in an
`## Assumptions` section of the new skill's SKILL.md. The user can correct
later if any assumption turns out to be wrong.

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

Before declaring the skill done, run a sanity-check against 2-3 realistic
prompts a real user might send.

**Preferred (if `subagent_worker` is available):** spawn ONE subagent_worker
to review the new SKILL.md. The subagent reads the skill, mentally applies
it to the prompts, returns a report listing gaps, ambiguities, missing edge
cases. Address findings in SKILL.md.

If your fixes were SUBSTANTIAL (you changed core instructions or added new
sections, not just typos), spawn a SECOND subagent to verify. Hard limit:
**max 2 subagent rounds total**. Past 2 rounds you're either overfitting to
the reviewer or the skill needs a different approach (consider rewriting
from scratch instead of patching).

**Fallback (no `subagent_worker` available):** mentally walk through 2-3
realistic prompts yourself, applying the skill step-by-step in your head.
Look for the same kinds of issues. The independent-eyes property is weaker,
but better than no check at all.

### Package

When the skill is in good shape, package it. The script is portable — invoke
it directly:

```bash
python <project-root>/skills/skill-creator/scripts/package_skill.py <abs-path-to-skill-folder> [output-directory]
```

Or via our skills API:

```
skills_run_script(skill_id='skill-creator', script_name='package_skill.py', args={...})
```

The script first validates the skill (via `quick_validate`) and then creates
`<skill-name>.skill` (zip archive) — by default in the current working
directory, or in `output-directory` if provided. Tell the user the absolute
path of the produced `.skill` so they can install or share it. We do NOT
have a `present_files` tool — just print the path.

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
  Eval artefacts under `.eval_workspaces/` are excluded from `.skill`
  archives automatically.
