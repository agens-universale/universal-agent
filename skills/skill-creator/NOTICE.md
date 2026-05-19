# NOTICE

This skill is derived from `skill-creator`, originally published at
https://github.com/anthropics/skills under Apache License 2.0
(see `LICENSE.txt` in this directory).

## Modified files

The following files in this directory have been modified from the
upstream version:

- `SKILL.md` — rewritten to match this project's lean workflow (no
  full eval-loop; capture intent + interview + draft + lightweight
  self-check + optional package). Adapted for our subagent_worker tool,
  three-layer skills model, and headless environment.
- `scripts/quick_validate.py` — extended `ALLOWED_PROPERTIES` to
  recognise this project's frontmatter (`id`, `version`); relaxed
  kebab-case enforcement on `name`; moved kebab-case check to `id`;
  read SKILL.md with explicit `encoding='utf-8'` (fixes the cp1251
  default on Windows for UTF-8 files).
- `scripts/package_skill.py` — replaced the sibling-package import
  `from scripts.quick_validate import validate_skill` with a portable
  `sys.path.insert` of the script's own directory, so the script runs
  correctly from any working directory and via any invocation form
  (direct path, `python -m`, our `skills_run_script` API). Also
  extended `ROOT_EXCLUDE_DIRS` with `.eval_workspaces` so eval
  artefacts (if ever produced by experimental flows) are not bundled
  into the resulting `.skill` archive.

All other files (`scripts/utils.py`, `scripts/__init__.py`, `LICENSE.txt`)
are unmodified copies.

The following upstream files are NOT included in this distribution:
`scripts/aggregate_benchmark.py`, `scripts/generate_report.py`,
`scripts/run_loop.py`, `scripts/run_eval.py`,
`scripts/improve_description.py`, `references/schemas.md`,
`references/agents/{grader,analyzer,comparator}.md`,
`eval-viewer/{generate_review.py, viewer.html}`,
`assets/eval_review.html`. These files implement the full eval-loop
workflow (parallel subagent_worker runs, grading, benchmarking, HTML
viewer, blind comparison, description optimization) which is not part
of this lean version. They live in the experimental branch
`experiment/skill-creator-full-workflow` for further research and may
return in a future iteration.
