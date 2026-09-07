---
id: receiving-code-review
name: Receiving Code Review
description: Discipline for responding to review feedback — verify each point technically before agreeing or pushing back. Prevents both performative agreement and stubborn defense. Use when a reviewer has left comments.
version: 1.0.0
---

# Receiving Code Review

Reviews come in three kinds: clearly correct, clearly wrong, and "depends on
context". Your job is to respond to each correctly, not to perform agreement.

## Hard rules

- **No performative agreement.** "You're right, I'll fix that!" without
  understanding WHY they're right is worse than disagreement — you accept a
  change you don't believe in, and the codebase ends up with mixed intent.
- **No blind defense.** "It's fine as-is" without re-reading their argument
  is dismissive. Re-read first.
- **Verify before responding.** If they say "this breaks X" — actually run X
  and observe whether it breaks. Don't argue from theory.

## Process

For each review comment:

### 1. Read carefully

- What specific behavior or concern is the reviewer raising?
- Is it factual ("this throws NPE on empty list") or stylistic ("I'd name this
  differently")?
- Is there a concrete suggestion or just a question?

### 2. Verify technically

For factual claims:

- If they claim a bug: write a test that would catch it. Does the test fail
  on your current code? If yes — they're right. If no — gather evidence.
- If they claim a contract violation: read the spec/docs and check.
- If they claim a missed edge case: try it by running the code.

For stylistic claims:

- Match the surrounding codebase. If their suggestion aligns with project
  conventions → accept. If yours aligns and theirs doesn't → push back
  with the convention as evidence.

### 3. Respond

Use one of:

- **Accept + fix:** "Right, that misses the empty case. Added test in
  `test_x.py:42`, fixed in `module/y.py:18`. New commit: <sha>."
- **Push back with evidence:** "I think this is intentional — see
  `module/z.py:55` where we already handle that path. Happy to add a
  comment if it's confusing."
- **Ask for clarification:** "I'm not sure I follow — could you give an
  example input that would trigger this?"

### 4. Don't accept what you don't believe

If you disagree but the reviewer insists, take it offline (DM / sync chat /
voice). Don't push a change you think is wrong just to close the review —
either get to a real consensus, or escalate to a third opinion.

## Anti-patterns

### Performative agreement

> Reviewer: "I think we should rename `foo` to `bar`."
> You: "Great catch, I'll fix it!" *commits rename without thinking*

Better:
> You: "I'll rename it to `bar`. Quick sanity check — is the reason
> [X reason I inferred], or were you thinking of something else?"

### Blind defense

> Reviewer: "This might throw on empty input."
> You: "It's been working in prod for months, I'm not going to touch it."

Better:
> You: "Let me check — `pytest -k 'test_empty'` → 0 tests
> exist for empty input. I'll add one to confirm. *test added, passes* —
> looks like it's actually handled because of the early `if not items:` at
> line 12. Adding the test for regression coverage."

### Scope explosion via "while you're at it"

> Reviewer: "Could you also rename these other 5 things and refactor
> module Q while you're at it?"
> You: "Sure, will do."

Better:
> You: "Those are good ideas but they're separate concerns from this PR's
> scope (which is X). I'll open follow-up issues for the rename and the
> refactor — happy to assign to myself."

## Output

Either:
- Updated commits in response to feedback, with a brief note per resolved
  comment explaining what changed and why.
- A respectful but specific pushback with evidence.

## Next step

If the review surfaced bugs or missing tests:
- `skills_load("supercoder:test-driven-development")` or
  `skills_load("supercoder:systematic-debugging")`.

If everything is resolved:
- `skills_load("supercoder:verification-before-completion")` before the
  final "ready to merge" signal.
