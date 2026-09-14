#!/usr/bin/env bash
# Локальный аналог проверок Sonar для gc-core (см. [tool.ruff] в pyproject.toml).
#   scripts/lint.sh            — весь gc-core
#   scripts/lint.sh <paths...> — только указанные файлы/каталоги
# Нужна dev-группа lint: `uv sync --group lint`.
set -u
cd "$(dirname "$0")/.."
TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=(packages/core/src/gc_core packages/core/assets/skills/skill-creator/scripts)
fi
rc=0
echo "== ruff (мелкие правила Sonar)"
uv run --group lint ruff check "${TARGETS[@]}" || rc=1
echo "== complexipy (cognitive complexity > 15)"
# --failed печатает только нарушения, но всегда выходит с 0; код возврата даёт --quiet.
uv run --group lint complexipy "${TARGETS[@]}" --max-complexity-allowed 15 --failed --color no
uv run --group lint complexipy "${TARGETS[@]}" --max-complexity-allowed 15 --quiet || rc=1
echo "== sonar_like_check (дубли литералов, regex-подсказки)"
uv run python scripts/sonar_like_check.py "${TARGETS[@]}" || rc=1
exit $rc
