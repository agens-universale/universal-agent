---
id: writing-skills
name: Writing Skills
description: Best practices for authoring new SKILL.md files (flat or in a pack). Frontmatter spec, hard-gate patterns, next-step chaining, local tool vocabulary. Use when creating or revising a project skill.
version: 1.0.0
---

# Writing Skills

A SKILL.md is a behavioral contract for an agent. Bad skills get ignored;
good skills get followed and produce consistent results.

## Structure of a SKILL.md

### Frontmatter (mandatory)

```yaml
---
id: <kebab-case-id>            # must match directory name
name: <Human Name>             # for skills_list display
description: <one-paragraph>   # what this skill is for + when to load it
version: 1.0.0                 # semver, optional but recommended
---
```

The `description` is the most important field — it's what `skills_list` shows
to the agent at discovery time. It MUST tell the agent both:
- **What** the skill does (one sentence).
- **When** to load it (specific intent triggers).

### Body sections (recommended)

| Section | Purpose |
|---|---|
| Brief intro | One paragraph: what this skill solves. |
| Hard-gate / Hard-rule | If the skill has invariants (e.g. "don't write code until X"), call them out at the top in bold. |
| Process | Numbered steps or phases. Each step concrete and actionable. |
| Anti-patterns | Common ways the skill gets violated. Specific examples > generic warnings. |
| When NOT to use | Bound the skill — prevents over-application. |
| Output | What artifact / state the skill leaves behind. |
| Next step | Concrete link to the skill that runs after, or "STOP and return control". |

Not every skill needs every section. A trivial skill might just have a brief
intro and Process.

## Hard-gates

A hard-gate is an imperative "do NOT X until Y". It works because:
- Bold capitalized prose is harder to skim past than a soft suggestion.
- It gives the agent a clear stop signal even when momentum says "just keep going".

Use hard-gates for:
- Approval-before-action gates (design before code, plan before execute).
- Verification-before-claim (no "done" without running tests).
- Scope discipline (no unrelated changes in this commit).

Don't overuse — three hard-gates per skill is the upper limit. More than that
and they lose their force.

## Next-step chaining

End every process-style skill with an explicit next step, in the form:

```markdown
## Next step

After <observable state>:
- `skills_load("<pack>:<skill-id>")`.
```

This creates a text-based workflow chain across skills. No engine needed —
the agent's reading is the engine.

## Tool-agnostic body

Describe actions in plain language, not by tool name: "read the file",
"run the tests", "save progress notes", "finalize the result". Do not
hardcode runtime tool names — skills must stay portable across runtimes.

The only tools you may name explicitly are the skill-discovery tools
`skills_load` and `skills_list`, because they are intrinsic to the skills
mechanism itself (e.g. the next-step chaining above).

## Length budget

- Bootstrap / entry skills: ≤ 50 lines.
- Methodology / process skills: 60-120 lines.
- Domain orchestrators (a single skill driving a whole multi-step
  delivery/workflow): up to 250 lines if genuinely needed. Anything longer
  is probably two skills.

Long skills won't get followed. The agent's attention budget is finite.

## References (optional)

For content too detailed to embed (lookup tables, anti-pattern catalogs):

- Put it in `<skill-dir>/references/<slug>.md` (no frontmatter).
- Reference it from the SKILL.md body:
  ```
  See `skills_load_reference("<skill-id>", "<slug>")` for details.
  ```

Don't put more than 3-4 references per skill — beyond that, refactor.

## Testing a skill

Manual smoke test before declaring it ready:

1. Load it via `skills_load(...)`. Did it parse correctly? Body readable?
2. Walk through the Process steps mentally on a real task in your domain. Do
   the steps actually map to what you'd do?
3. For methodology skills: deliberately violate a hard-gate. Does the skill
   actually catch your attention, or does it read as a suggestion?
4. Get a second pair of eyes on the description — does it convey when to load?

## Next step

After writing a skill, validate via `skills_load("<your-pack>:<your-skill>")`,
then load it on a real task and observe whether you actually follow it. If
you don't follow your own skill, neither will the agent.

For packaging multiple new skills as a coherent pack, refer to the spec at
`docs/supercoder/specs/2026-05-12-skills-packs-and-supercoder-pack.md`.
