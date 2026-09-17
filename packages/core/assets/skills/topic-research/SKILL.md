---
id: "topic-research"
name: "topic-research"
description: "Conducts deep research on a topic using multiple independent sources, primary evidence, competing viewpoints, reliability assessment, and confidence-rated recommendations. Use when the user asks to investigate, research, compare, benchmark vendors/products/markets/models, find sources, or prepare an evidence-backed overview. Best for external or domain questions, market, technology, regulatory, vendor research, and uncertain claims. Do not use for automated benchmark/eval task instances requiring no-HITL execution and machine-checkable artifacts; use autonomous-benchmark-agent. Do not use for writing full articles, software architecture design, or code implementation."
version: "0.1.1"
---

# Skill: Topic Research

## Purpose
Use this skill to investigate a topic and produce a decision-ready research brief.
Focus on depth, independent evidence, competing viewpoints, and practical recommendations.

Default outcome: deep research artifact (file + chat) with findings, confidence, risks, and next actions.

## Use When
- The user asks to research an unfamiliar topic or compare approaches.
- The task requires evidence synthesis, not immediate implementation.
- The user needs recommendations backed by sources and explicit uncertainty.
- There are conflicting claims and a reasoned position is needed.

Do not use this skill for pure coding tasks, ADR-level architecture design, full long-form article production, or automated benchmark/eval task instances that require no-HITL execution and machine-checkable artifacts.

## Language Strategy
- Default to Russian for final user-facing output unless the user requests another language.
- Sources can be in any language.
- Preserve standard Russian technical terms; keep common English terms when useful.

## Input Contract
Before research, define:
- Research question and decision context.
- Scope boundaries (in-scope / out-of-scope).
- Constraints: time horizon, geography, platform, regulatory limits, budget assumptions.
- Freshness requirements for sources, if the topic is time-sensitive.
- Success criteria: what would make the result actionable.

If key inputs are missing, ask focused clarifying questions. Otherwise proceed with explicit assumptions.

## Research Depth
- Treat this as deep research by default. Do not optimize for minimal token use or short-form answers unless the user explicitly asks for a brief response.
- Do not rely on a single source for important conclusions. Seek multiple independent sources and compare them.
- For non-trivial topics, use enough sources to test the conclusion, not just to support it.
- Follow references, citations, changelogs, issue threads, or primary documents when they materially improve confidence.
- Prefer a smaller number of well-explained, high-quality findings over a broad shallow survey.

## Research Saturation
- Do not stop research just because the first plausible answer appears. Continue until the main conclusion has been challenged against independent source families, primary evidence, counterclaims, and edge cases.
- Treat additional collection as saturated only when new sources mostly repeat already captured claims and do not add material evidence, counterexamples, changed definitions, fresher data, or different applicability conditions.
- If time or tool limits prevent saturation, say so explicitly and identify the highest-value next sources to check.

## Execution Model
- The agent applying this skill becomes the research owner for its assigned scope.
- The research owner is responsible for question framing, research decomposition, final synthesis, and the answer it returns to its caller or user.
- Additional agents are optional evidence gatherers, not final decision makers.
- Use additional agents when the topic has independent research tracks, competing options, or enough scope to benefit from parallel investigation.
- Give each additional agent a narrow evidence task with explicit scope, source expectations, freshness expectations, and required output format.
- Ask additional agents for evidence packs: claims, sources, dates, reliability notes, conflicts, and confidence caveats.
- The research owner must cross-check delegated findings, remove duplicate source chains, resolve conflicts, and produce the final research brief or evidence pack required by its caller.
- If delegation is unavailable or unnecessary, execute the same workflow directly within the applying agent.

## Workflow

### 1) Question Framing
- Convert user request into 1 primary question and up to 5 sub-questions.
- Define comparison dimensions (cost, performance, risk, adoption complexity, etc.).
- State assumptions and information gaps.

Exit criteria:
- Research question is testable and bounded.
- Comparison dimensions are explicit.

### 2) Source Plan
- Plan source mix before collecting evidence:
  - primary sources (official docs, specs, vendor docs, standards),
  - secondary sources (analyses, blogs, reports),
  - contextual sources (forums/issues/changelogs when relevant).
- Prefer recent and original sources for factual claims.
- For current technology, market, regulatory, security, pricing, or vendor claims, prefer sources updated close to the current date visible in the system context.
- Record when source dates are unavailable, stale, or unclear.

Exit criteria:
- At least 5 independent sources planned for non-trivial topics when available.
- Primary sources are included for core claims.
- No major recommendation depends on a single source unless explicitly marked as low confidence.

### 3) Evidence Collection
- Collect evidence per sub-question with source metadata:
  - claim,
  - source URL/title,
  - publication/update date (if available),
  - source type (primary/secondary/contextual).
- Separate observed facts from interpretation.
- Capture source independence: note whether sources are truly independent or repeat the same original claim.
- Cite evidence close to the finding it supports, not only in a final source list.

Exit criteria:
- Each major claim has at least one cited source.
- High-impact claims have 2+ corroborating sources when feasible.
- Recommendations are supported by multiple independent sources or marked as uncertain.

### 4) Reliability Assessment
Assess each source:
- Authority (who published it).
- Recency.
- Method transparency.
- Potential bias/conflict of interest.
- Reproducibility/verifiability.

Assign reliability tier: high / medium / low with short rationale.

Assess conclusion confidence separately from source reliability:
- high: multiple independent reliable sources agree, and definitions/context match.
- medium: reliable evidence exists, but context, recency, or applicability is limited.
- low: evidence is sparse, indirect, stale, biased, or materially contested.

### 5) Conflict Resolution
When sources disagree:
- Identify exactly what conflicts (metric, timeframe, context, definition).
- Prefer higher-reliability and newer primary evidence.
- Keep both views if conflict is unresolved.
- Explicitly mark unresolved uncertainty.

Exit criteria:
- Conflicts are either resolved with rationale or recorded as open uncertainty.

### 6) Synthesis and Recommendations
- Summarize findings by sub-question.
- Provide recommendations with:
  - expected benefit,
  - key risk,
  - confidence level (high/medium/low),
  - conditions where recommendation may fail.
- Distinguish:
  - confirmed findings,
  - probable findings,
  - hypotheses requiring validation.

### 7) Decision-Ready Output

**Delivery (MANDATORY — both channels):**

1. **File** — write the full research brief (template below) to a Markdown artifact under the writable project dir, name like `*_research_brief.md`. Create the file early (skeleton/outline is fine), then **append verified findings after each meaningful research block** — do not keep the accumulating brief only in the dialog/trace until the end (dialog may be summarized and drop detail). Verify writes (tool `ok` / readback). The skill is **not complete** until this file exists with the full brief.
2. **Chat** — include a compact decision-ready summary (executive summary, key findings, recommendations, uncertainty) **and** the absolute path to the file. Do not reply with only a path, and do not leave the full brief only in chat/trace without a file.

Brief contents:
- Executive summary.
- Key findings.
- Option comparison (if applicable).
- Recommendations and next experiments.
- Source list and evidence notes.

Keep the chat summary concise enough to be useful, but do not omit important uncertainty, conflicting evidence, or source rationale for brevity. The file holds the full structured brief.

## Output Template
Use this structure in the **file** (and mirror the same sections compactly in chat) unless the user requests another format:

```markdown
# Research Brief: <Topic>

## 1. Research Question and Scope
- Primary question:
- Sub-questions:
- Scope boundaries:
- Assumptions:

## 2. Method
- Research depth: deep by default
- Source strategy:
- Evidence limitations:
- Freshness limitations:

## 3. Key Findings
- Finding 1:
  - Evidence:
  - Confidence: high | medium | low
- Finding 2:
  - Evidence:
  - Confidence: high | medium | low

## 4. Option Comparison (optional)
- Criteria:
- Option A:
- Option B:
- Trade-offs:

## 5. Conflicts and Uncertainty
- Resolved conflicts:
- Unresolved conflicts:
- What would reduce uncertainty:

## 6. Recommendations
- Recommendation 1:
  - Why:
  - Expected benefit:
  - Main risk:
  - Failure conditions:
- Recommendation 2:
  - Why:
  - Expected benefit:
  - Main risk:
  - Failure conditions:

## 7. Next Steps
- Immediate actions:
- Validation experiments:
- Revisit trigger:

## 8. Sources
- [Source title](URL) — type, date, reliability tier
```

## Quality Gates
Before finalizing, verify:
- Scope is explicit and bounded.
- Major claims are sourced.
- Important conclusions are checked against multiple independent sources.
- Conflicting evidence is addressed, not ignored.
- Confidence levels are stated for key findings.
- Recommendations include risks and failure conditions.
- Sources are traceable, relevant, and reasonably fresh for the topic.
- File artifact exists and was updated incrementally; chat includes path + compact summary.

## Anti-Patterns
- Presenting unsourced claims as facts.
- Relying on one source for an important conclusion.
- Treating repeated copies of the same original claim as independent evidence.
- Delegating final recommendations without research-owner synthesis.
- Mixing facts and opinions without labeling.
- Cherry-picking only sources that support one conclusion.
- Hiding uncertainty when evidence is weak.
- Delivering generic recommendations with no actionable next step.
- Chat-only or file-only delivery (both channels required).
- Keeping the full brief only in the dialog until the end — risk of loss when context is summarized.
