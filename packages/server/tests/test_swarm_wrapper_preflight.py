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


def _argv(tmp_path, **env):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    argv_file = tmp_path / "argv.txt"
    cli = venv / "bin" / "gc-cli"
    cli.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\n')
    cli.chmod(0o755)
    proc, _ = _run(
        tmp_path,
        SWARM_CLI_COMMAND="gc-cli",
        SWARM_CLI_VENV_ROOT=str(venv),
        SWARM_SYSTEM="g",
        SWARM_TASK_ID="t1",
        **env,
    )
    assert proc.returncode == 0, proc.stderr
    return argv_file.read_text().splitlines()


def test_thread_in_the_store_is_the_task_thread(tmp_path):
    """Тред задачи в общей базе: CLI ведёт тред (system, task_id) — тот,
    что читает /logs и в который вебхук кладёт сообщения."""
    argv = _argv(tmp_path, SWARM_THREAD_IN_STORE="1")
    assert argv[argv.index("-t") + 1] == "t1"
    assert argv[argv.index("--system") + 1] == "g"


def test_files_mode_runs_without_a_thread(tmp_path):
    assert "-t" not in _argv(tmp_path)


def _fake_cli(tmp_path, body: str):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    cli = venv / "bin" / "gc-cli"
    cli.write_text("#!/bin/sh\n" + body)
    cli.chmod(0o755)
    return {"SWARM_CLI_COMMAND": "gc-cli", "SWARM_CLI_VENV_ROOT": str(venv)}


def _bwrap_args(tmp_path, **env):
    """Аргументы bwrap: подставной bwrap записывает их и выходит."""
    fake = tmp_path / "fakebin"
    fake.mkdir()
    out = tmp_path / "bwrap.txt"
    bwrap = fake / "bwrap"
    bwrap.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{out}"\n')
    bwrap.chmod(0o755)
    proc, _ = _run(
        tmp_path,
        SWARM_SANDBOX_TOOL="bubblewrap",
        PATH=f"{fake}{os.pathsep}{os.environ['PATH']}",
        **_fake_cli(tmp_path, ""),
        **env,
    )
    assert proc.returncode == 0, proc.stderr
    args = out.read_text().splitlines()
    return [args[i : i + 3] for i in range(len(args))]


def test_sandbox_mounts_the_thread_socket(tmp_path):
    """Каталог сокета прокси — в песочнице по известному пути и только на
    чтение (подключиться к сокету можно, писать в каталог хоста — нет:
    ревью, раунд 2, R8); CLI находит сокет через GC_THREAD_SOCKET."""
    sockets = tmp_path / "gct"
    sockets.mkdir()
    triples = _bwrap_args(
        tmp_path,
        SWARM_THREAD_IN_STORE="1",
        SWARM_THREAD_SOCKET_DIR=str(sockets),
    )
    assert ["--ro-bind", str(sockets), "/run/gc-thread"] in triples
    assert ["--bind", str(sockets), "/run/gc-thread"] not in triples
    assert [
        "--setenv",
        "GC_THREAD_SOCKET",
        "/run/gc-thread/thread.sock",
    ] in triples


def test_files_mode_mounts_no_socket(tmp_path):
    triples = _bwrap_args(tmp_path)
    assert not any("/run/gc-thread" in t for t in triples)


def test_without_sandbox_the_cli_gets_the_host_socket(tmp_path):
    sockets = tmp_path / "gct"
    sockets.mkdir()
    seen = tmp_path / "socket.txt"
    proc, _ = _run(
        tmp_path,
        SWARM_THREAD_IN_STORE="1",
        SWARM_THREAD_SOCKET_DIR=str(sockets),
        **_fake_cli(tmp_path, f'printf "%s" "$GC_THREAD_SOCKET" > "{seen}"\n'),
    )
    assert proc.returncode == 0, proc.stderr
    assert seen.read_text() == str(sockets / "thread.sock")


def test_inbox_turn_runs_without_a_query(tmp_path):
    """Продолжение задачи: ход из входящего потока треда, без -q."""
    argv = _argv(tmp_path, SWARM_THREAD_IN_STORE="1", SWARM_INBOX_TURN="1")
    assert "--inbox" in argv
    assert "-q" not in argv
    assert argv[argv.index("-t") + 1] == "t1"
