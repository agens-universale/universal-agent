---
id: research-artifact-compiler
name: Research Artifact Compiler
description: "Compiles free-form research input (notes, conversation excerpts, experiment logs, PDF excerpts, source lists) into a STRUCTURED research artifact with four layers: Logic (claims with falsifiability criteria, formal concepts, related work), Physical (configs, env stubs), Exploration (research DAG in YAML), Evidence (numerical results with provenance). Every claim is bound to one or more experiment IDs (E01, E02, …); evidence layer holds the raw numbers — never duplicated inside experiment plans. Citations resolved through science-mcp work_lookup. Intended as an intermediate format consumed by `scientific-article-creator` and `scientific-article-checker`. Use when the user has raw scientific material to organize before writing or reviewing. Do NOT use for casual note-taking — only for material with claims and evidence."
version: 0.1.0
---

# Skill: Research Artifact Compiler

## Language Strategy

- This SKILL.md is in English (project convention).
- The deliverable — the artifact directory and the chat summary — is produced in **Russian Markdown** unless the user explicitly asks for another language.
- Quoted source titles, DOIs, and verbatim numerical results stay in their original form.

## Role boundary

Compiler **structures**, it does not verify. Verifying that citations exist and that claims match cited sources is the job of `scientific-article-checker`. If the user wants verification of the compiled artifact, run the checker as a follow-up step.

## When to use

- Triggers: "compile a research artifact from these notes", "structure my results", "turn this PDF into claims+evidence", "prepare material for an article".
- Do NOT use for casual notes without scientific content. Do NOT use on already-structured artifacts (just consume them).

## What it does

Converts scattered material into a canonical 4-layer directory where every claim is explicitly bound to an experiment or external source.

## Inputs

- Raw text / file path / set of files (notes, logs, paper excerpts).
- Optional — the topic or hypothesis the material relates to.

## Algorithm

### Step 1 — extraction

Scan the input and tag:
- Claims — sentences with factual content.
- Experiments / observations / measurements — descriptions of methodology and what was measured.
- Numerical results with source attribution.
- External references (DOI / PMID / arXiv) — collect separately.

### Step 2 — citation normalization (optional)

For each external reference, decide whether normalization is needed:
- If the input already provides authors / title / year / journal — copy the entry as-is. Do not call `work_lookup`.
- If the input has only a bare identifier (just a DOI or PMID, no other metadata), call `work_lookup(identifier)` once to enrich the record.
- An identifier that fails to resolve is tagged `unresolved` in `sources.json` and surfaced in the chat summary. Verification beyond that is the checker's job, not the compiler's.

### Step 3 — build the 4 layers

Create `<output_dir>/artifact/`:

```
artifact/
├── PAPER.md              # one-page summary: what is being studied and why
├── logic/
│   ├── claims.md         # claims, each with criteria and evidence-refs
│   ├── concepts.md       # formal definitions / variables
│   └── related_work.md   # from normalized WorkRecord entries
├── physical/
│   ├── hyperparams.yaml  # if applicable
│   └── environment.md    # environment / setup
├── exploration_tree.yaml # research DAG (≥8 nodes)
└── evidence/
    ├── experiments.md    # E01, E02, ... — design + methodology, NO exact numbers
    ├── results.md        # exact numerical results bound to E01, E02, ...
    └── sources.json      # array of WorkRecords (with DOI / PMID / arXiv)
```

### Step 4 — claims-evidence binding

Every claim in `logic/claims.md` has:
- ID (C01, C02, …).
- Claim text.
- `evidence_refs: [E01, E03]` — experiments it relies on.
- `criteria` — the observation that would refute the claim.
- If there are no evidence-refs — status `unsupported` (highlighted, red tag).

Rule: claim wording is bounded by what the evidence shows. A validation metric supports a claim about a validation metric. Do not write "method improves X in production" when the evidence is a test set only.

### Step 5 — exploration_tree.yaml

At least 8 nodes of the form:

```yaml
nodes:
  - id: N01
    type: question | hypothesis | experiment | dead_end | pivot | decision | observation | conclusion
    title: "..."
    parent: null | <id>
    notes: "..."
edges:
  - from: N01
    to: N02
    relation: "leads_to | rejects | refines | branches"
```

Record dead-ends and pivots honestly — not only the successful path.

### Step 6 — self-validation

Run the following self-check:
- Every claim either has ≥1 `evidence_refs` or is marked `unsupported`.
- Every experiment ID referenced from a claim exists in `experiments.md`.
- `experiments.md` contains no exact numbers (design only); exact numbers live in `results.md`.
- exploration_tree has ≥8 nodes.
- Every `sources.json` entry has a canonical identifier.

Surface the validation report to the user.

## MCP tools

- `work_lookup(identifier)` — **only** when a reference is missing metadata. Not called for already-complete entries.
- `citations_by_doi(doi, count_only=true)` — optional, for citation-weight notes in `related_work.md`.

This skill does not run full verification. If the user asks "are these citations valid?" — hand off to `scientific-article-checker` after compilation.

## Output format

The directory structure above plus a short chat summary:

```
artifact compiled at <path>
- claims: <N> (supported: <M>, unsupported: <K>)
- experiments: <N>
- exploration_tree nodes: <N>
- sources: <N> total, <K> normalized via work_lookup, <U> unresolved
```

## What NOT to do

- Do not invent experiments / results that are not in the input. If data is missing — flag a gap and ask the user to fill it in.
- Do not copy exact numbers into experiment plans — they belong only in `evidence/results.md`.
- Do not write a finished article — the artifact is an input for `scientific-article-creator`.
- Do not duplicate `scientific-article-checker`: forcing `work_lookup` on every already-complete reference is verification, not structuring.

## Examples

**Request:** "Compile these notes (3 KB) into an artifact."
→ Steps 1–6. If the text yields 5 claims and only 2 experiments, the exploration_tree still has ≥8 nodes (questions and pivots count, not only experiments).

**Request:** "I only have a list of 10 papers, no experiments of my own."
→ Review-mode artifact: `claims.md` reflects claims from external papers, `evidence_refs` point to entries in `sources.json` as `E01, E02 ...` external evidence; the exploration_tree describes the structure of the field.

## Limits

- If the input requires full text of papers that exists only in PubMed, `ncbi_fetch` truncates at 50 KB. Use summarization or `open_access_search` instead.
- For very large inputs (>50 KB), compile in multiple passes and merge.
