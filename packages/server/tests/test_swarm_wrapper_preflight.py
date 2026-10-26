"""Обёртка песочницы: проверка наличия CLI до запуска.

Сама run-cli-in-sandbox.sh не покрывалась ничем — её гоняли только настоящие
sworm'ы. Поводом завести сетку стал разъезд дистрибутивов: сервер больше не
приносил gc-cli, и каждый sworm заканчивался голым `exit 127` с
`error_type=wrapper_setup_error`. Код правильный, но по нему не видно, что не
найдена ИМЕННО команда CLI, — человек шёл искать поломку в bwrap.

Отдельно про PATH-ветку: `uv tool install gc-server` теперь приносит gc-cli
зависимостью, но в PATH выносит только команды самого дистрибутива. Проверено
установкой во временный UV_TOOL_DIR: бинарь лежит в
<UV_TOOL_DIR>/gc-server/bin/gc-cli, симлинка в UV_TOOL_BIN_DIR нет. Поэтому
сообщение обязано называть лекарство — swarm.cli_venv_root.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "gc_server"
    / "swarm"
    / "run-cli-in-sandbox.sh"
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="обёртка — bash-скрипт"
)


def _run(tmp_path, **overrides):
    """Запустить обёртку с минимальным окружением; вернуть (proc, completion)."""
    env = dict(os.environ)
    env.update(
        {
            "SWARM_ID": "swarm_g_t1",
            "SWARM_THREADS_DIR": str(tmp_path / "threads"),
            "SWARM_SANDBOX_PATH": str(tmp_path / "sandbox"),
            # Без bubblewrap: на dev-машинах его нет, а проверка наличия CLI идёт
            # до выбора песочницы и от неё не зависит.
            "SWARM_SANDBOX_TOOL": "none",
            "SWARM_PAYLOAD_JSON": "{}",
            "SWARM_LIMIT_TIMEOUT_SECONDS": "30",
        }
    )
    env.update(overrides)
    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
    )
    completion_path = tmp_path / "threads" / "swarm_g_t1" / "completion.json"
    completion = (
        json.loads(completion_path.read_text(encoding="utf-8"))
        if completion_path.exists()
        else None
    )
    return proc, completion


def test_missing_cli_in_path_names_the_command_and_the_cure(tmp_path):
    """Команды нет в PATH — обёртка обязана сказать, ЧТО не нашлось и что делать."""
    proc, completion = _run(
        tmp_path,
        SWARM_CLI_COMMAND="gc-cli-not-installed",
        SWARM_CLI_VENV_ROOT="",
    )

    assert proc.returncode == 127
    assert completion is not None, (
        "completion.json обязан появиться и в этом случае"
    )
    assert completion["exit_code"] == 127
    assert completion["error_type"] == "wrapper_setup_error"
    message = completion["error_message"]
    assert "gc-cli-not-installed" in message
    assert "PATH" in message
    assert "cli_venv_root" in message
    # То же самое обязано быть видно в stderr.log прогона, а не только в JSON.
    stderr_log = (
        tmp_path / "threads" / "swarm_g_t1" / "stderr.log"
    ).read_text()
    assert "gc-cli-not-installed" in stderr_log


def test_missing_cli_in_venv_names_the_path_it_tried(tmp_path):
    """Задан cli_venv_root, а бинаря в нём нет — в сообщении полный путь."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    proc, completion = _run(
        tmp_path,
        SWARM_CLI_COMMAND="gc-cli",
        SWARM_CLI_VENV_ROOT=str(venv),
    )

    assert proc.returncode == 127
    assert completion["error_type"] == "wrapper_setup_error"
    assert str(venv / "bin" / "gc-cli") in completion["error_message"]
    assert "cli_venv_root" in completion["error_message"]


def test_existing_cli_is_not_blocked_by_the_preflight(tmp_path):
    """Положительный контроль: живая команда проходит проверку и запускается."""
    proc, completion = _run(
        tmp_path,
        SWARM_CLI_COMMAND="true",
        SWARM_CLI_VENV_ROOT="",
    )

    assert proc.returncode == 0, proc.stderr
    assert completion["exit_code"] == 0
    assert completion["error_type"] == ""


def test_cli_is_started_as_an_external_request(tmp_path):
    """Swarm-ран — запрос внешней системы, как webhook: обёртка всегда
    передаёт CLI `--external-request` (человека нет, слова — внешние)."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    argv_file = tmp_path / "argv.txt"
    cli = venv / "bin" / "gc-cli"
    cli.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\n')
    cli.chmod(0o755)
    proc, completion = _run(
        tmp_path, SWARM_CLI_COMMAND="gc-cli", SWARM_CLI_VENV_ROOT=str(venv)
    )

    assert proc.returncode == 0, proc.stderr
    assert "--external-request" in argv_file.read_text().splitlines()


@pytest.mark.parametrize(
    "venv_name",
    ['ve"nv', "ve\\nv", 've"nv\\back'],
    ids=["кавычка", "обратный слеш", "оба сразу"],
)
def test_completion_json_survives_quotes_in_user_paths(tmp_path, venv_name):
    """Кавычка в пути не должна ломать JSON — иначе диагностика теряется вся.

    `error_message` собирается из значений, которые человек пишет в
    `config_server.yml` (`swarm.cli_command`, `swarm.cli_venv_root`), и уезжает
    в JSON через heredoc. До экранирования одна кавычка делала файл невалидным:
    `storage.load_completion_info` возвращал `{}`, `lifecycle` отчёта не видел,
    ждал смерти PID и ставил `unknown_failed`. Терялась ровно та подсказка, ради
    которой preflight и заведён, — и симптом («задача умерла неизвестно от
    чего») уводил искать причину не туда.

    До PR такого класса не было: в `write_completion` шли только фиксированные
    строки и числа.
    """
    venv = tmp_path / venv_name
    (venv / "bin").mkdir(parents=True)

    proc, completion = _run(
        tmp_path,
        SWARM_CLI_COMMAND="gc-cli",
        SWARM_CLI_VENV_ROOT=str(venv),
    )

    assert proc.returncode == 127
    # _run уже сделал json.loads — до починки он бросал JSONDecodeError.
    assert completion is not None
    assert completion["error_type"] == "wrapper_setup_error"
    # Путь обязан не только сохранить валидность файла, но и доехать целиком:
    # экранирование не должно превратиться в потерю символов.
    assert str(venv / "bin" / "gc-cli") in completion["error_message"]
