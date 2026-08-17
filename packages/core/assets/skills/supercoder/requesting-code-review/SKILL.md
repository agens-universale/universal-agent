---
id: requesting-code-review
name: Requesting Code Review
description: Self-checklist before asking another person or agent to review your work. Catches scope creep, missing tests, and unclear context. Use BEFORE pushing for review.
version: 1.0.0
---

# Requesting Code Review

A code review is expensive (someone else's time). Don't ask for it on broken
or incomplete work — self-check first.

## Pre-review checklist

Run all of these before requesting review. Skipping any item = reviewer wastes
time spotting things you could have spotted.

### 1. Tests pass

Run the relevant test suite. Observe the output. If a single test fails — don't
request review. Fix or explain in the PR description (with rationale).

```
pytest tests/<area>/ -v
```

### 2. Diff is in scope

Run:
```
git diff <base-branch>..HEAD --stat
git diff <base-branch>..HEAD --name-only
```

For every file in the diff, ask:
- Does this change directly serve the task's stated goal?
- Is there an "improvement" I made along the way that's actually a separate concern?

If yes to the second — separate it into its own commit/PR. Scope creep makes
reviews longer and risks rejection on otherwise-fine work.

### 3. No leftover debug artifacts

Search for:
- `print(` calls added during debugging
- `console.log`, `dbg!`, `TODO`, `FIXME`, `XXX`
- Commented-out code (delete it; git remembers)
- Timing logs / metric counters added "for testing"

### 4. Commit messages are useful

```
git log <base-branch>..HEAD --oneline
```

Each line should be readable a year from now. "fix bug" / "wip" / "more changes"
are not. Re-word or squash before push.

### 5. Description ready

For the PR / message to reviewer, prepare:

- **What** changed (1-3 bullet points).
- **Why** (which problem this solves; link to spec/issue if applicable).
- **How verified** (tests run, manual smoke, screenshots if UI).
- **What I'm unsure about** — explicit list of things you want the reviewer
  to focus on.

The "unsure about" list is the highest-value item. It tells the reviewer where
your attention has been, and where it hasn't.

### 6. No silent dependency changes

If you bumped a library version, added a new dep, or removed one — call it out
explicitly in the description. Don't bury it in `package.json` / `pyproject.toml`.

## Output

A short message / PR description following the structure in step 5.

## Hard rules

- **Never push for review on failing tests** unless the failure is the point
  of the PR (and you've said so).
- **Never request review without running the verification commands** above.
  "I think it's clean" is not self-review.
- **Never ask "is this good?" — ask specific questions**. "Did I miss any
  edge cases?" / "Is the abstraction in module Foo right?" lead to useful
  reviews; "is this good?" leads to either rubber-stamps or scope explosions.

## Next step

After the reviewer responds:
- `skills_load("supercoder:receiving-code-review")`.
