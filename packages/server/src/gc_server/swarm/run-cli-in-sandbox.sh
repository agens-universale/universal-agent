#!/usr/bin/env bash
# shellcheck shell=bash
# Обёртка для запуска gc-cli внутри bubblewrap-песочницы.
#
# Окружение заполнено src/swarm/runner.py:
#   Обязательные:
#     SWARM_ID, SWARM_SYSTEM, SWARM_TASK_ID,
#     SWARM_THREADS_DIR, SWARM_SANDBOX_DIR, SWARM_SANDBOX_PATH,
#     SWARM_CLI_COMMAND, SWARM_SANDBOX_TOOL,
#     SWARM_LIMIT_MEMORY_MB, SWARM_LIMIT_DISK_MB, SWARM_LIMIT_CPU_PERCENT,
#     SWARM_LIMIT_TIMEOUT_SECONDS,
#     SWARM_PAYLOAD_JSON.
#   Опциональные (см. AR-6/AR-7 в swarm_spec.md §10):
#     SWARM_CLI_VENV_ROOT — путь к venv с gc-cli (read-only bind).
#     SWARM_AGENT_DEP_DIR — путь к Node.js + npm-кэш для npx MCP (read-only).
#     SWARM_EXTRA_MOUNTS  — multi-line (newline-separated) список путей для
#                           дополнительных read-only bind-mount'ов.
#
# По завершении пишет ${SWARM_THREADS_DIR}/${SWARM_ID}/completion.json
# с полями exit_code, finished_at, error_type (см. swarm_spec.md шаг 1.1).
#
# ВАЖНО: set -e отключаем при выполнении основной команды, чтобы успеть
# поймать exit code в любом случае. set -uo pipefail оставляем.

set -uo pipefail

SWARM_ID="${SWARM_ID:-swarm_unknown}"
SWARM_SYSTEM="${SWARM_SYSTEM:-generic}"
SWARM_TASK_ID="${SWARM_TASK_ID:-unknown}"
SWARM_THREADS_DIR="${SWARM_THREADS_DIR:-./swarm_threads}"
SWARM_SANDBOX_PATH="${SWARM_SANDBOX_PATH:?SWARM_SANDBOX_PATH is required}"
SWARM_CLI_COMMAND="${SWARM_CLI_COMMAND:-gc-cli}"
SWARM_SANDBOX_TOOL="${SWARM_SANDBOX_TOOL:-bubblewrap}"
SWARM_LIMIT_TIMEOUT_SECONDS="${SWARM_LIMIT_TIMEOUT_SECONDS:-1800}"
SWARM_CLI_VENV_ROOT="${SWARM_CLI_VENV_ROOT:-}"
SWARM_AGENT_DEP_DIR="${SWARM_AGENT_DEP_DIR:-}"
SWARM_EXTRA_MOUNTS="${SWARM_EXTRA_MOUNTS:-}"

# --- Подготовка лог-директории (канал B в spec §6.1) ---

SWARM_LOG_DIR="${SWARM_THREADS_DIR}/${SWARM_ID}"
mkdir -p "${SWARM_LOG_DIR}"
STDOUT_LOG="${SWARM_LOG_DIR}/stdout.log"
STDERR_LOG="${SWARM_LOG_DIR}/stderr.log"
COMPLETION_FILE="${SWARM_LOG_DIR}/completion.json"

# --- Sandbox root ---
# Запрос пользователя доставляется в CLI напрямую через -q "${SWARM_PAYLOAD_JSON}"
# (весь payload как JSON-строка, симметрично webhook-режиму). Файл payload.json
# больше не пишем — gc-cli не умеет --payload-file, а -q это штатный вход.

mkdir -p "${SWARM_SANDBOX_PATH}"

# --- Подкаталог .gc/ для runtime_config + thread store (каналы A + E) ---

mkdir -p "${SWARM_SANDBOX_PATH}/.gc/threads"
mkdir -p "${SWARM_SANDBOX_PATH}/.gc/logs"

# --- Утилита: записать completion.json с финальным состоянием ---

write_completion() {
  local exit_code="$1"
  local error_type="${2:-}"
  local error_message="${3:-}"
  local finished_at

  # POSIX-формат секунд UTC — работает и на GNU date, и на BSD (macOS).
  # Микросекунды не используем: лишняя точность для жизненного цикла sworm'ов.
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # Atomic write через .tmp + mv (избегаем половинного файла при kill).
  cat > "${COMPLETION_FILE}.tmp" <<EOF
{
  "exit_code": ${exit_code},
  "finished_at": "${finished_at}",
  "error_type": "${error_type}",
  "error_message": "${error_message}"
}
EOF
  mv "${COMPLETION_FILE}.tmp" "${COMPLETION_FILE}"
}

# --- Сборка bwrap-команды ---

if [[ "${SWARM_SANDBOX_TOOL}" == "bubblewrap" ]]; then
  # Динамические базовые ro-bind'ы — терпимо к Alpine (без /lib64), Arch
  # (lib → symlink в /usr/lib), Fedora и т.п.
  EXTRA_BWRAP=()
  [[ -d /lib ]]   && EXTRA_BWRAP+=(--ro-bind /lib /lib)
  [[ -d /lib64 ]] && EXTRA_BWRAP+=(--ro-bind /lib64 /lib64)
  [[ -d /bin ]]   && EXTRA_BWRAP+=(--ro-bind /bin /bin)
  [[ -d /sbin ]]  && EXTRA_BWRAP+=(--ro-bind /sbin /sbin)
  [[ -d /etc ]]   && EXTRA_BWRAP+=(--ro-bind /etc /etc)

  # CLI venv (AR-6).
  if [[ -n "${SWARM_CLI_VENV_ROOT}" && -d "${SWARM_CLI_VENV_ROOT}" ]]; then
    EXTRA_BWRAP+=(--ro-bind "${SWARM_CLI_VENV_ROOT}" "${SWARM_CLI_VENV_ROOT}")

    # Auto-resolve Python interpreter symlink (для uv-managed venv'ов,
    # которые ссылаются на ~/.local/share/uv/python/.../bin/python3.X).
    if [[ -x "${SWARM_CLI_VENV_ROOT}/bin/python" ]]; then
      PYTHON_BIN="$(readlink -f "${SWARM_CLI_VENV_ROOT}/bin/python" 2>/dev/null || true)"
      if [[ -n "${PYTHON_BIN}" && -e "${PYTHON_BIN}" ]]; then
        PYTHON_ROOT="$(dirname "$(dirname "${PYTHON_BIN}")")"
        case "${PYTHON_ROOT}" in
          "${SWARM_CLI_VENV_ROOT}"*) : ;;       # уже внутри venv
          /usr*|/lib*|/lib64*|/bin*|/sbin*) : ;; # уже в базовых bind'ах
          *)
            EXTRA_BWRAP+=(--ro-bind "${PYTHON_ROOT}" "${PYTHON_ROOT}")
            ;;
        esac
      fi
    fi
  fi

  # Node для npx MCP (AR-6).
  if [[ -n "${SWARM_AGENT_DEP_DIR}" && -d "${SWARM_AGENT_DEP_DIR}" ]]; then
    EXTRA_BWRAP+=(--ro-bind "${SWARM_AGENT_DEP_DIR}" "${SWARM_AGENT_DEP_DIR}")
  fi

  # Дополнительные пути для специфичных кэшей (AR-6).
  # Пустые строки и несуществующие пути пропускаем без ошибки.
  while IFS= read -r mount_path; do
    [[ -z "${mount_path}" ]] && continue
    if [[ -d "${mount_path}" || -f "${mount_path}" ]]; then
      EXTRA_BWRAP+=(--ro-bind "${mount_path}" "${mount_path}")
    fi
  done <<< "${SWARM_EXTRA_MOUNTS}"

  BWRAP_CMD=(
    bwrap
    --unshare-all
    --share-net
    --dev /dev
    --proc /proc
    # Ephemeral writable /tmp внутри песочницы. Нужен любым тулзам, которые
    # пишут во временные файлы (например playwright-mcp: mkdtemp /tmp/playwright-artifacts-*).
    # tmpfs эфемерен — исчезает вместе с песочницей, изоляцию не ослабляет.
    --tmpfs /tmp
    --ro-bind /usr /usr
    # HOME=/work делает /work «домашним» — npm пишет логи/кэш в /work/.npm
    # (writable канал A), а не в /root/.npm (read-only).
    # XDG_CACHE_HOME оставляем на /root/.cache — туда bind-mount'ится
    # ms-playwright chromium через extra_mounts, playwright читает его оттуда.
    --setenv HOME /work
    --setenv NPM_CONFIG_CACHE /work/.npm
    --setenv NPM_CONFIG_PREFIX /work/.npm-global
  )
  if [[ ${#EXTRA_BWRAP[@]} -gt 0 ]]; then
    BWRAP_CMD+=("${EXTRA_BWRAP[@]}")
  fi
  BWRAP_CMD+=(
    --bind "${SWARM_SANDBOX_PATH}" /work
    --chdir /work
  )
else
  # Fallback: запуск без песочницы (только для dev/debug).
  BWRAP_CMD=(bash -lc "cd \"${SWARM_SANDBOX_PATH}\" && exec \"${SWARM_CLI_COMMAND}\"")
fi

# --- Сборка CLI-команды (AR-1, AR-5) ---

# Если задан venv — запускаем CLI по абсолютному пути из него.
# Иначе fallback на legacy-поведение (имя в $PATH).
if [[ -n "${SWARM_CLI_VENV_ROOT}" ]]; then
  CLI_BIN="${SWARM_CLI_VENV_ROOT}/bin/${SWARM_CLI_COMMAND}"
else
  CLI_BIN="${SWARM_CLI_COMMAND}"
fi

# Проверяем, что команда вообще есть, ДО запуска. Раньше единственным следом
# отсутствующего CLI были exit 127 и error_type=wrapper_setup_error — по ним не
# видно, что не найден именно gc-cli, и человек ищет поломку в bwrap.
# Ветка без cli_venv_root особенно коварна: `uv tool install gc-server`
# экспортирует в PATH только СВОИ команды (проверено установкой во временный
# UV_TOOL_DIR: gc-cli лежит в <UV_TOOL_DIR>/gc-server/bin, а симлинка в
# UV_TOOL_BIN_DIR у него нет), поэтому gc-cli стоит рядом с сервером, но по
# имени не находится. Лечится заданием swarm.cli_venv_root.
CLI_FOUND=0
if [[ -n "${SWARM_CLI_VENV_ROOT}" ]]; then
  [[ -x "${CLI_BIN}" ]] && CLI_FOUND=1
  CLI_HINT="check swarm.cli_venv_root in config_server.yml (now: ${SWARM_CLI_VENV_ROOT})"
else
  command -v "${CLI_BIN}" >/dev/null 2>&1 && CLI_FOUND=1
  CLI_HINT="uv tool install exports only the commands of the requested distribution, so gc-cli from the gc-server tool venv is NOT in PATH; point swarm.cli_venv_root at that venv"
fi
if [[ "${CLI_FOUND}" -ne 1 ]]; then
  CLI_ERROR="CLI ${SWARM_CLI_COMMAND} not found (tried: ${CLI_BIN}); ${CLI_HINT}"
  printf '%s\n' "${CLI_ERROR}" >> "${STDERR_LOG}"
  # 127 — тот же код, что вернул бы сам запуск ненайденной команды: сторонние
  # мониторы, которые уже смотрят на exit_code, ничего не теряют.
  write_completion 127 "wrapper_setup_error" "${CLI_ERROR}"
  exit 127
fi

CLI_CMD=(
  "${CLI_BIN}"
  --config "/work/.gc/config.yml"
  -q "${SWARM_PAYLOAD_JSON:-}"
)

# Внешний trace_id (SWARM_TRACE_ID). Флаг добавляем ТОЛЬКО при непустом
# значении: пустой --trace-id "" ничего бы не дал, а лишний аргумент в командной
# строке — это уже отличие от нетрассируемого рана. Через конфиг трейс передать
# нельзя: ребёнку материализуется сырой YAML с extra="forbid".
if [[ -n "${SWARM_TRACE_ID:-}" ]]; then
  CLI_CMD+=(--trace-id "${SWARM_TRACE_ID}")
fi

# --- Таймаут через GNU timeout (если есть) ---

if command -v timeout >/dev/null 2>&1; then
  # SIGTERM (15) c grace-period 30s, потом SIGKILL (9).
  # Grace нужен чтобы CLI успел сделать summary compaction если она в процессе
  # (см. swarm_spec.md §6, «Суммаризация в swarm-режиме»).
  TIMEOUT_PREFIX=(timeout --kill-after=30 --signal=TERM "${SWARM_LIMIT_TIMEOUT_SECONDS}")
else
  TIMEOUT_PREFIX=()
fi

# --- Запуск CLI и захват exit code ---

# Защита под set -u: пустой массив через ${arr[@]+"${arr[@]}"} (bash 3.2-safe).
# set +e локально, чтобы поймать exit code даже при ошибке.
set +e
if [[ "${SWARM_SANDBOX_TOOL}" == "bubblewrap" ]]; then
  ${TIMEOUT_PREFIX[@]+"${TIMEOUT_PREFIX[@]}"} \
    "${BWRAP_CMD[@]}" -- "${CLI_CMD[@]}" \
    >>"${STDOUT_LOG}" 2>>"${STDERR_LOG}"
  EXIT_CODE=$?
else
  ${TIMEOUT_PREFIX[@]+"${TIMEOUT_PREFIX[@]}"} \
    "${CLI_CMD[@]}" \
    >>"${STDOUT_LOG}" 2>>"${STDERR_LOG}"
  EXIT_CODE=$?
fi
set -e

# --- Финализация: интерпретация exit code → error_type ---

ERROR_TYPE=""
ERROR_MESSAGE=""
case "${EXIT_CODE}" in
  0)
    # success
    ;;
  124)
    # GNU timeout: SIGTERM послан, процесс убит по таймауту.
    ERROR_TYPE="timeout"
    ERROR_MESSAGE="Killed by GNU timeout after ${SWARM_LIMIT_TIMEOUT_SECONDS}s"
    ;;
  125|126|127)
    # 125 — ошибка timeout binary, 126 — нельзя выполнить, 127 — не найдено.
    ERROR_TYPE="wrapper_setup_error"
    ERROR_MESSAGE="Wrapper command launch failed (exit ${EXIT_CODE})"
    ;;
  137)
    # 128+9 = SIGKILL. Прибит после grace-period таймаута.
    ERROR_TYPE="timeout"
    ERROR_MESSAGE="Killed by SIGKILL (post-grace)"
    ;;
  143)
    # 128+15 = SIGTERM. Например, graceful shutdown сервера.
    ERROR_TYPE="signal_term"
    ERROR_MESSAGE="Killed by SIGTERM"
    ;;
  *)
    ERROR_TYPE="cli_error"
    ERROR_MESSAGE="CLI exited with code ${EXIT_CODE}"
    ;;
esac

write_completion "${EXIT_CODE}" "${ERROR_TYPE}" "${ERROR_MESSAGE}"

exit "${EXIT_CODE}"
