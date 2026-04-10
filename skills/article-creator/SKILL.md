---
id: "article-creator"
name: "Article Creator"
description: "Instruction for agent how to create article on given subject"
version: "0.1.0"
---

# Skill: Paper Creation - Research, Writing & Fact-Checked Article Generation

## Overview
A structured multi-agent workflow for producing publication-ready, rigorously fact-checked articles in Markdown. The skill orchestrates `Planner`, `Worker`, `Critic`, and `SpecialAgent` sub-agents through iterative drafting, auditing, compilation, and final verification loops to guarantee accuracy, logical coherence, and proper sourcing.

## Language Strategy (Critical)
- **Target Language**: All final user-facing content MUST be in **Russian**.
- **Intermediate Artifacts**: All section drafts, editor compilations, and critic reports MUST also be generated in **Russian** to ensure linguistic consistency and proper stylistic evaluation.
- **Research Phase**: Agents ARE encouraged to search and analyze sources in English or other languages. However, when synthesizing information into an article artifact, they must write directly in Russian.
- **Terminology**: Use established Russian technical terms where they exist. For neologisms or widely used English terms (e.g., "prompt engineering", "fine-tuning"), keep the English term with a brief Russian explanation on first use if necessary.

## Agent Roles & Responsibilities

| Role | Language Constraint |
|------|---------------------|
| `Planner` | Outline and section descriptions in **Russian**. Research notes can be multilingual. |
| `Worker` | **Write all draft content directly in Russian**. Use English sources as reference, but do not "write in English then translate". |
| `Critic` | Evaluate drafts **in Russian**: check grammar, style, flow, and cultural appropriateness for a Russian-speaking audience, in addition to factual accuracy. |
| `SpecialAgent` | When instantiated (e.g., as `Editor`), generate role-specific prompts that instruct the model to operate **in Russian**. |
| `Orchestrator` *(implicit coordinator)* | Routes tasks, manages artifact versions, enforces workflow gates, and triggers role transitions. |

## Workflow Pipeline

### Phase 1: Planning & Outline
1. `Planner` performs initial research (web search, knowledge retrieval, analytical reasoning).
2. `Planner` outputs a structured outline containing:
   - Section titles & logical order
   - Core theses/arguments per section
   - Required tone, depth, and audience alignment
   - Flags for complex sections requiring `SpecialAgent`

### Phase 2: Section Drafting & Refinement (Iterative Loop)
*Executed per outline item:*
**Hard gating rule for `Orchestrator`**:
- Process sections strictly one-by-one (`section_n` fully approved before `section_n+1` starts).
- Do NOT start drafting the next section until the current section has `Critic` status `Pass` for all claims.
- Do NOT batch-run all `Worker` drafts first and postpone `Critic` to the end.
1. **Assignment**: `Orchestrator` routes the section to:
   - `Worker` (standard complexity), or
   - `SpecialAgent` (complex/niche topics; auto-generates system prompt from role description)
2. **Drafting**: The assigned agent produces a Markdown artifact for the section using search, reasoning, and domain knowledge.
3. **Audit**: `Critic` reviews the draft and outputs a structured report covering:
   - Factual accuracy & logical gaps
   - Unsupported claims or overstatements
   - Structural/stylistic recommendations
   - `Pass / Minor / Critical` tags per claim
4. **Revision**: `Worker` updates the section, explicitly addressing every Minor and Critical item.
5. **Verification**: `Critic` cross-checks the revised version against the previous report. Loop repeats until all items are `Pass`.
6. **Gate check by `Orchestrator`**: Only after `Pass` on the current section, enqueue and start the next outline item.

### Phase 3: Editorial Compilation
1. `SpecialAgent` is instantiated with role `Editor` with competence in necessary domain.
2. It ingests all approved section artifacts, unifies terminology, ensures smooth transitions, removes redundancy, and outputs a cohesive draft article in Markdown.

### Phase 4: Final Fact-Checking & Polish
1. `Critic` scans the compiled article and extracts **every factual claim...**
2. `Worker` performs comprehensive fact-checking:
   - Uses live search/knowledge to verify each extracted item
   - Corrects inaccuracies, updates outdated data, clarifies ambiguities
   - Inserts inline Markdown citations for every non-common-knowledge claim
3. `Worker` outputs the **clean final version** in Russian, fully formatted and ready for publication.

## Output Specifications
- **Format**: Valid Markdown
- **Citation Style**: Inline footnotes or direct links for every factual claim in style: `[Source Title](URL)`
- **Reference List**: `## References` section at the end, listing all sources with:
  - Title, publisher/author, publication/access date, and direct URL
  - Numbered or alphabetically ordered
- **Quality Gates**:
  - Zero unsourced factual statements
  - All critic feedback explicitly resolved and traceable
  - Consistent tone, terminology, and heading hierarchy
  - Publication-ready without further editorial intervention

## Execution Constraints
1. **Strict Role Separation**: Agents must not bypass orchestration or merge responsibilities outside defined phases.
2. **Artifact Versioning**: All outputs must be traceable (e.g., `section_01_draft_v1.md`, `section_01_revised_v2.md`, `article_final_clean.md`).
3. **Critic Reports**: Must follow structured format: `[Claim/Thesis] → [Issue] → [Action Required] → [Status]`.
4. **SpecialAgent Prompting**: Role description must fit the article topic.
5. **Final Consistency Check**: `Worker` must run a self-review pass to verify citation integrity, markdown validity, and claim-to-source mapping before delivery.
6. Translate article into Russian language. Final article should ve always in Russian language. Don't omit any information in translation
7. **No deferred section QA**: Running `Critic` only once after all section drafts are produced is prohibited. Section-level critic loop is mandatory during Phase 2.


## Expected Article format


```
# [Article Title]

[Introduction...]

## [Section 1]
Content with inline citations: ...[Source Title](https://example.com/source1) ...[Source Title](https://example.com/source2)

## [Section 2]
...

## References
- [Source Title](https://example.com/source1) - Author, Year
- [Source Title](https://example.com/source2) - Organization, Date
```

