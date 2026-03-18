#!/usr/bin/env bash
set -euo pipefail

# Простейшая обёртка для запуска gc-cli внутри bubblewrap.
# Ожидает, что окружение заполнил Python-слой (src/swarm/runner.py):
#   SWARM_ID, SWARM_SYSTEM, SWARM_TASK_ID,
#   SWARM_THREADS_DIR, SWARM_SANDBOX_DIR, SWARM_SANDBOX_PATH,
#   SWARM_CLI_COMMAND, SWARM_SANDBOX_TOOL,
#   SWARM_LIMIT_MEMORY_MB, SWARM_LIMIT_DISK_MB,
#   SWARM_LIMIT_CPU_PERCENT, SWARM_LIMIT_TIMEOUT_SECONDS,
#   SWARM_PAYLOAD_JSON (по желанию).

SWARM_ID="${SWARM_ID:-swarm_unknown}"
SWARM_SYSTEM="${SWARM_SYSTEM:-generic}"
SWARM_TASK_ID="${SWARM_TASK_ID:-unknown}"
SWARM_THREADS_DIR="${SWARM_THREADS_DIR:-./swarm_threads}"
SWARM_SANDBOX_PATH="${SWARM_SANDBOX_PATH:?SWARM_SANDBOX_PATH is required}"
SWARM_CLI_COMMAND="${SWARM_CLI_COMMAND:-gc-cli}"
SWARM_SANDBOX_TOOL="${SWARM_SANDBOX_TOOL:-bubblewrap}"
SWARM_LIMIT_TIMEOUT_SECONDS="${SWARM_LIMIT_TIMEOUT_SECONDS:-1800}"

# Директория для логов сворма на хосте.
SWARM_LOG_DIR="${SWARM_THREADS_DIR}/${SWARM_ID}"
mkdir -p "${SWARM_LOG_DIR}"

STDOUT_LOG="${SWARM_LOG_DIR}/stdout.log"
STDERR_LOG="${SWARM_LOG_DIR}/stderr.log"

# Создаём payload.json внутри песочницы, если передан SWARM_PAYLOAD_JSON.
if [[ -n "${SWARM_PAYLOAD_JSON:-}" ]]; then
  mkdir -p "${SWARM_SANDBOX_PATH}"
  printf '%s\n' "${SWARM_PAYLOAD_JSON}" > "${SWARM_SANDBOX_PATH}/payload.json"
fi

# Формируем команду запуска внутри bubblewrap.
if [[ "${SWARM_SANDBOX_TOOL}" == "bubblewrap" ]]; then
  # Минимально необходимый набор биндов; при необходимости можно расширить.
  BWRAP_CMD=(
    bwrap
    --unshare-all
    --share-net
    --dev /dev
    --proc /proc
    --ro-bind /usr /usr
    --ro-bind /lib /lib
    --ro-bind /lib64 /lib64
    --bind "${SWARM_SANDBOX_PATH}" /work
    --chdir /work
  )
else
  # Без песочницы — запускаем CLI напрямую в рабочей директории.
  BWRAP_CMD=(bash -lc "cd \"${SWARM_SANDBOX_PATH}\" && exec \"${SWARM_CLI_COMMAND}\"")
fi

# Основная команда: gc-cli, по умолчанию без дополнительных аргументов.
CLI_CMD=("${SWARM_CLI_COMMAND}")

# Если есть payload.json, можно при желании добавить сюда специальные аргументы для CLI.
# Например: CLI_CMD+=("--payload-file" "payload.json")

# Таймаут ограничиваем через стандартную утилиту timeout, если она есть.
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_PREFIX=(timeout "${SWARM_LIMIT_TIMEOUT_SECONDS}")
else
  TIMEOUT_PREFIX=()
fi

if [[ "${SWARM_SANDBOX_TOOL}" == "bubblewrap" ]]; then
  # Запускаем gc-cli внутри bubblewrap, stdout/stderr пишем в отдельные логи на хосте.
  "${TIMEOUT_PREFIX[@]}" "${BWRAP_CMD[@]}" -- "${CLI_CMD[@]}" \
    >>"${STDOUT_LOG}" 2>>"${STDERR_LOG}"
else
  # Фоллбек без песочницы.
  "${TIMEOUT_PREFIX[@]}" bash -lc "cd \"${SWARM_SANDBOX_PATH}\" && exec \"${CLI_CMD[@]}\"" \
    >>"${STDOUT_LOG}" 2>>"${STDERR_LOG}"
fi

