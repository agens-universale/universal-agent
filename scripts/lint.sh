#!/usr/bin/env bash
# Единая точка проверок качества — для человека и для CI.
#
#   scripts/lint.sh              — весь репозиторий (как в CI)
#   scripts/lint.sh <пути...>    — только указанные файлы/каталоги
#
# Внутри — pre-commit со ВСЕМИ хуками из .pre-commit-config.yaml, включая
# стадию manual: гигиена, ruff (линт + формат), complexipy (S3776),
# scripts/sonar_like_check.py, мёртвый код (scripts/dead_code_check.py) и
# дублирование кода (pylint duplicate-code — при коммите не гоняется, только
# pre-push и здесь).
# Раньше этот скрипт звал инструменты сам и проверял ТОЛЬКО gc-core; теперь
# область — весь репозиторий, а список проверок один и тот же у всех.
#
# Нужна группа lint: `uv sync --group lint`.
# Если в системе есть prek (та же конфигурация, быстрее и без Python), можно
# взять его: GC_LINT_RUNNER=prek scripts/lint.sh
set -u
cd "$(dirname "$0")/.."

RUNNER="${GC_LINT_RUNNER:-pre-commit}"
if [ "$RUNNER" = "prek" ]; then
  RUN=(prek run)
else
  RUN=(uv run --group lint pre-commit run)
fi

if [ $# -eq 0 ]; then
  exec "${RUN[@]}" --all-files --hook-stage manual --show-diff-on-failure
fi
exec "${RUN[@]}" --hook-stage manual --files "$@"
