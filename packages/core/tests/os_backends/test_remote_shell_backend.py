# packages/core/tests/os_backends/test_remote_shell_backend.py
"""RemoteShellBackend с префиксом ["env"]: «удалённая» сторона = локальный sh."""

import asyncio
import os
import shutil
import subprocess

import pytest
from gc_core.backends.protocol import FsBackend
from gc_core.backends.remote_shell import (
    RemoteShellBackend,
    docker_argv_prefix,
    ssh_argv_prefix,
)

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or os.name == "nt", reason="нужен POSIX sh"
)


def _gnu(tool: str) -> bool:
    try:
        out = subprocess.run(
            [tool, "--version"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return False
    return "GNU" in (out.stdout + out.stderr)


gnu_only = pytest.mark.skipif(
    not (_gnu("stat") and _gnu("find")),
    reason="нужны GNU stat/find (см. docker live test)",
)


@pytest.fixture
def be():
    return RemoteShellBackend(["env"])


def test_is_a_full_backend():
    assert isinstance(RemoteShellBackend(["env"]), FsBackend)
    assert RemoteShellBackend(["env"]).is_sandbox is True
    assert RemoteShellBackend(["env"]).supports_background is True


async def test_probe_precedes_the_remote_command(monkeypatch):
    """Проба — первый ``sh -c``, без ``cd``. Команда идёт только после суммы."""
    from gc_core.backends.protocol import ExecResult
    from gc_core.backends.runtime_probe import RuntimeUnavailable
    from gc_testkit.runtime_probe import probe_stdout

    calls = []

    async def fake_process(argv, **kw):
        calls.append(argv[-1])
        hit = probe_stdout(argv[-1])
        if hit is not None:
            return ExecResult(stdout=hit, return_code=0)
        return ExecResult(stdout="ok\n", return_code=0)

    monkeypatch.setattr(
        "gc_core.backends.remote_shell._arun_bounded_process", fake_process
    )
    backend = RemoteShellBackend(["docker", "exec", "-i", "box"])
    result = await backend.aexec("echo hi", cwd="/work")
    assert result.stdout == "ok\n"
    assert probe_stdout(calls[0]) is not None
    assert not calls[0].startswith("cd ")
    assert calls[1].startswith("cd /work || exit; ")
    assert "echo hi" in calls[1]

    calls.clear()

    async def dead_process(argv, **kw):
        calls.append((argv[-1], kw.get("capture_max_chars")))
        return ExecResult(stderr="permission denied\n", return_code=1)

    monkeypatch.setattr(
        "gc_core.backends.remote_shell._arun_bounded_process", dead_process
    )
    # Свежий бэкенд: успешная проба первого жива 10 с и вторую бы пропустила.
    backend = RemoteShellBackend(
        ["docker", "exec", "-i", "box"],
        exec_output_capture_max_chars=4_000,
        walk_output_max_chars=50_000_000,
    )
    with pytest.raises(
        RuntimeUnavailable, match="Runtime connection failed"
    ) as err:
        await backend.aexec("echo hi")
    assert "turn_finish" in str(err.value)
    assert len(calls) == 1
    command, capture = calls[0]
    assert probe_stdout(command) is not None
    assert capture == backend.exec_output_capture_max_chars
    assert capture != backend.walk_output_max_chars


async def test_short_command_timeout_does_not_shrink_the_probe(monkeypatch):
    """``timeout: 1`` у команды не режет echo: проба всегда ждёт свои 60 с."""
    from gc_core.backends.protocol import ExecResult
    from gc_core.backends.runtime_probe import _RUNTIME_PROBE_TIMEOUT_SEC
    from gc_testkit.runtime_probe import probe_stdout

    seen = []

    async def fake_process(argv, **kw):
        if probe_stdout(argv[-1]) is not None:
            seen.append(kw.get("timeout"))
            return ExecResult(stdout=probe_stdout(argv[-1]), return_code=0)
        return ExecResult(stdout="ok\n", return_code=0)

    monkeypatch.setattr(
        "gc_core.backends.remote_shell._arun_bounded_process", fake_process
    )
    backend = RemoteShellBackend(["docker", "exec", "-i", "box"])
    result = await backend.aexec("echo hi", timeout=1)
    assert result.stdout == "ok\n"
    assert seen == [_RUNTIME_PROBE_TIMEOUT_SEC]


async def test_aexec_runs_through_prefix_sh_c(be):
    res = await be.aexec("echo $0; echo hi")
    assert res.return_code == 0
    assert res.stdout.endswith("hi\n")


async def test_exec_init_only_on_model_channel():
    be = RemoteShellBackend(["env"], exec_init="export GC_INIT=1")
    assert (await be.aexec("echo ${GC_INIT:-none}")).stdout.strip() == "1"
    assert (
        await be.aexec_raw("echo ${GC_INIT:-none}")
    ).stdout.strip() == "none"


async def test_cwd_and_env_apply(be, tmp_path):
    # `pwd -P`: логический pwd на macOS печатает /var/…, resolve() даёт /private/var/…
    res = await be.aexec(
        "pwd -P; echo $FOO", cwd=str(tmp_path), env={"FOO": "bar baz"}
    )
    assert res.stdout.splitlines() == [str(tmp_path.resolve()), "bar baz"]


async def test_bytes_round_trip_non_utf8(be, tmp_path):
    data = bytes(range(256)) * 300  # > одной строки base64, не UTF-8
    p = str(tmp_path / "deep" / "blob.bin")
    await be.write_bytes(p, data)
    assert await be.read_bytes(p) == data
    assert await be.exists(p)


async def test_write_read_path_with_quote_and_space(be, tmp_path):
    p = str(tmp_path / "it's here" / "a b.txt")
    await be.write(p, "привет\n")
    assert await be.read(p) == "привет\n"
    assert await be.is_file(p) and not await be.is_dir(p)


async def test_append_round_trip(be, tmp_path):
    p = str(tmp_path / "log.txt")
    await be.write(p, "a\n")
    await be.append(p, "b\n")
    assert await be.read(p) == "a\nb\n"


async def test_read_missing_raises_file_not_found(be, tmp_path):
    with pytest.raises(FileNotFoundError):
        await be.read(str(tmp_path / "nope"))


async def test_run_python_feeds_code_on_stdin(be):
    res = await be.arun_python("import sys; print(sys.argv, 2+2)")
    assert res.return_code == 0 and res.stdout.strip().endswith("4")


async def test_timeout_kills_client_and_reports_timed_out(be):
    t0 = asyncio.get_running_loop().time()
    res = await be.aexec("sleep 30", timeout=1)
    assert res.timed_out is True and res.return_code is None
    assert asyncio.get_running_loop().time() - t0 < 10


async def test_aspawn_background_returns_pid(be, tmp_path):
    info = await be.aspawn_background("sleep 5", cwd=str(tmp_path))
    assert isinstance(info["pid"], int) and info["pid"] > 0


@gnu_only
async def test_stat_and_walk_group(be, tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x.txt").write_text("12345")
    assert await be.getsize(str(tmp_path / "d" / "x.txt")) == 5
    assert await be.listdir(str(tmp_path)) == [("d", True)]
    entries = await be.walk_entries(str(tmp_path))
    assert {(e.rel, e.is_dir) for e in entries} == {
        ("d", True),
        ("d/x.txt", False),
    }


async def test_aprime_default_cwd_uses_configured_cwd_first(tmp_path):
    be = RemoteShellBackend(["env"], cwd=str(tmp_path))
    assert be.default_cwd() == str(tmp_path)


async def test_aprime_default_cwd_asks_pwd_when_unset(be):
    assert (await be.aprime_default_cwd()) == os.getcwd()


# --- префиксы ---
def test_docker_prefix():
    assert docker_argv_prefix({"docker": {"name": "box"}}) == [
        "docker",
        "exec",
        "-i",
        "box",
    ]
    assert docker_argv_prefix(
        {"docker": {"name": "box", "user": "me", "binary": "podman"}}
    ) == [
        "podman",
        "exec",
        "-i",
        "-u",
        "me",
        "box",
    ]


def test_ssh_prefix_batch_mode_and_control_master(tmp_path):
    pre = ssh_argv_prefix(
        {
            "ssh": {
                "host": "h",
                "port": 2222,
                "user": "u",
                "key_path": "~/.ssh/k",
                "options": ["StrictHostKeyChecking=accept-new"],
                "control_master": True,
            }
        },
        control_dir=tmp_path,
    )
    assert pre[:2] == ["ssh", "-T"]
    assert (
        "BatchMode=yes" in pre
        and "-p" in pre
        and pre[pre.index("-p") + 1] == "2222"
    )
    assert pre[pre.index("-i") + 1] == os.path.expanduser("~/.ssh/k")
    assert any(
        o.startswith("ControlPath=") and str(tmp_path) in o for o in pre
    )
    assert pre[-2:] == ["u@h", "--"]


def test_ssh_prefix_without_user_key_or_control(tmp_path):
    pre = ssh_argv_prefix(
        {"ssh": {"host": "h", "control_master": False}}, control_dir=tmp_path
    )
    assert "-i" not in pre and not any("ControlMaster" in o for o in pre)
    assert pre[-2:] == ["h", "--"]


# --- транспорт, который склеивает argv (так делает ssh) ---
# Шим ведёт себя как OpenSSH: всё после хоста склеивается пробелами и уходит
# одной строкой в шелл той стороны.
_SSH_LIKE = ["sh", "-c", 'exec sh -c "$*"', "fake-ssh"]


async def test_joining_transport_keeps_cwd_and_writes(tmp_path):
    d = tmp_path / "rv dir"
    d.mkdir()
    be = RemoteShellBackend(_SSH_LIKE, cwd=str(d), joins_argv=True)
    res = await be.aexec("echo hi; pwd -P")
    assert res.stdout.splitlines() == ["hi", str(d.resolve())]
    p = str(d / "it's" / "a b.txt")
    await be.write(p, "x")
    assert await be.read(p) == "x"


async def test_fs_ops_have_a_timeout(monkeypatch):
    # Зависший транспорт (пауза контейнера, мёртвый хост) не вешает ФС-операции.
    # До самой операции идёт проба, и режет её свой потолок (в бою 60 с), не
    # fs_op_timeout_sec. sleep 30 короче этого потолка и успевает кончиться,
    # суммы нет, отказ не кэшируется: три вызова держали бы ~90 с, и граница
    # ниже мерила бы конец sleep, а не отсечку. Здесь потолок ужат до 1 с.
    from gc_core.backends.runtime_probe import RuntimeUnavailable

    monkeypatch.setattr(
        "gc_core.backends.runtime_probe._RUNTIME_PROBE_TIMEOUT_SEC", 1
    )
    be = RemoteShellBackend(["sh", "-c", "sleep 30", "hang"])
    t0 = asyncio.get_running_loop().time()
    # Проба не получает сумму и обрывается по своему потолку. Это не «файла
    # нет»: иначе модель пересоздаёт существующее.
    with pytest.raises(RuntimeUnavailable):
        await be.exists("/x")
    with pytest.raises(RuntimeUnavailable):
        await be.write("/x", "y")
    with pytest.raises(RuntimeUnavailable):
        await be.read_bytes("/x")
    assert asyncio.get_running_loop().time() - t0 < 15


def test_ssh_prefix_has_connect_timeout_and_keepalive(tmp_path):
    pre = ssh_argv_prefix({"ssh": {"host": "h"}}, control_dir=tmp_path)
    assert "ConnectTimeout=10" in pre and "ServerAliveInterval=15" in pre


async def test_capability_probes_work(be):
    # FsBackend._cap(name, cache_attr) — общий кэш проб; свой `_cap` на бэкенде
    # перекрывал его, и `has_find` падал с TypeError (поймано docker live).
    await be.awarm_capabilities()
    assert isinstance(be.has_find, bool) and isinstance(be.has_grep, bool)


async def test_download_errors_are_not_all_file_not_found(be, tmp_path):
    # Каталог и запрет на чтение — не «файла нет»: иначе модель решит, что файла
    # не существует, и перезапишет его.
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(OSError) as ei:
        await be.read_bytes(str(d))
    assert not isinstance(ei.value, FileNotFoundError)
    with pytest.raises(FileNotFoundError):
        await be.read_bytes(str(tmp_path / "missing.txt"))


def test_env_names_must_be_identifiers():
    # Имя переменной уходит в `export K=V` без кавычек: `X=1; cmd; Y` в имени —
    # команда мимо модерации. Тул режет раньше, бэкенд — страховка.
    import pytest

    from gc_core.backends.remote_shell import RemoteShellBackend
    from gc_core.tools.exec.execute_command import _parse_env_vars

    b = RemoteShellBackend(
        ["env"],
        cwd=None,
        env={},
        exec_init=None,
        exec_output_capture_max_chars=1000,
        walk_output_max_chars=1000,
    )
    with pytest.raises(ValueError, match="invalid environment variable name"):
        b._shell("echo hi", env={"X=1; touch /tmp/pwned; Y": "v"})
    assert "export OK_1=v;" in b._shell("echo hi", env={"OK_1": "v"})

    env, err = _parse_env_vars(
        [{"name": "X=1; touch /tmp/pwned; Y", "value": "v"}]
    )
    assert env == {} and err["field"] == "env_vars[0].name"
    env, err = _parse_env_vars([{"name": "GOOD_NAME", "value": "v"}])
    assert env == {"GOOD_NAME": "v"} and err is None


def test_watchdog_wrap_variants():
    import shlex

    from gc_core.backends.remote_shell import watchdog_wrap

    body = "cd '/w' || exit; echo hi"
    assert watchdog_wrap(body, None, "timeout") == body
    assert watchdog_wrap(body, 5, None) == body
    assert watchdog_wrap(body, 5, "timeout").startswith("timeout 7 sh -c ")
    assert watchdog_wrap(body, 5, "gtimeout").startswith("gtimeout 7 sh -c ")
    perl = watchdog_wrap(body, 5, "perl")
    assert perl.startswith("perl -e ") and perl.endswith(
        " 7 sh -c " + shlex.quote(body)
    )
    assert "setpgrp" in perl and "alarm" in perl
    setsid = watchdog_wrap(body, 5, "setsid")
    # busybox: своя сессия + kill по группе, stdin возвращён через fd 3
    assert f"setsid sh -c {shlex.quote(body)} <&3 3<&- &" in setsid
    assert "sleep 7; kill -TERM -- -$p" in setsid and setsid.startswith(
        "exec 3<&0;"
    )


async def test_timed_out_command_does_not_outlive_the_timeout():
    # Таймаут убивал только локальный клиент: `sleep 300 &` в контейнере жил
    # дальше. Сторож той стороны (timeout / gtimeout / perl) убивает группу.
    import asyncio
    import os
    import signal

    from gc_core.backends.remote_shell import RemoteShellBackend

    be = RemoteShellBackend(
        ["env"],
        cwd=None,
        env={},
        exec_init=None,
        exec_output_capture_max_chars=10000,
        walk_output_max_chars=10000,
    )
    if await be._awatchdog() is None:
        pytest.skip("no timeout/gtimeout/perl on this host")
    res = await be.aexec("sleep 300 & echo $!; wait", timeout=1)
    assert res.timed_out is True
    pid = int(res.stdout.strip().splitlines()[0])
    await asyncio.sleep(3.5)  # сторож срабатывает на timeout + 2 с
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_watchdog_probe_is_cached_and_reported():
    from gc_core.backends.remote_shell import RemoteShellBackend

    be = RemoteShellBackend(
        ["env"],
        cwd=None,
        env={},
        exec_init=None,
        exec_output_capture_max_chars=1000,
        walk_output_max_chars=1000,
    )
    first = await be._awatchdog()
    assert first in ("timeout", "gtimeout", "setsid", "perl", None)
    if first is None:
        return
    be._afs_exec = None  # найденный сторож второй раз не спрашивается
    assert await be._awatchdog() == first


async def test_dead_runtime_is_not_cached_as_no_watchdog():
    from types import SimpleNamespace

    from gc_core.backends.remote_shell import RemoteShellBackend
    from gc_core.backends.runtime_probe import RuntimeUnavailable

    be = RemoteShellBackend(
        ["env"],
        cwd=None,
        env={},
        exec_init=None,
        exec_output_capture_max_chars=1000,
        walk_output_max_chars=1000,
    )

    async def down(*_args, **_kwargs):
        raise RuntimeUnavailable()

    be._afs_exec = down
    with pytest.raises(RuntimeUnavailable):
        await be._awatchdog()
    assert "_watchdog_tool" not in be.__dict__

    async def live(*_args, **_kwargs):
        return SimpleNamespace(
            stdout="timeout\n", stderr="", return_code=0, timed_out=False
        )

    be._afs_exec = live
    assert await be._awatchdog() == "timeout"


async def test_missing_watchdog_is_probed_again():
    """«Сторожа нет» не запоминается: стоп и честный none спрашиваются снова."""
    from types import SimpleNamespace

    from gc_core.backends.remote_shell import RemoteShellBackend

    def _backend():
        return RemoteShellBackend(
            ["env"],
            cwd=None,
            env={},
            exec_init=None,
            exec_output_capture_max_chars=1000,
            walk_output_max_chars=1000,
        )

    def _result(stdout: str, return_code: int, **extra):
        return SimpleNamespace(
            stdout=stdout,
            stderr="",
            return_code=return_code,
            timed_out=False,
            **extra,
        )

    stopped = _backend()
    calls = {"n": 0}

    async def stopped_then_timeout(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _result("", -15, stopped_by_user=True)
        return _result("timeout\n", 0)

    stopped._afs_exec = stopped_then_timeout
    assert await stopped._awatchdog() is None
    assert "_watchdog_tool" not in stopped.__dict__
    assert await stopped._awatchdog() == "timeout"
    assert calls["n"] == 2
    stopped._afs_exec = None
    assert await stopped._awatchdog() == "timeout"

    absent = _backend()
    asked = {"n": 0}

    async def always_none(*_args, **_kwargs):
        asked["n"] += 1
        return _result("none\n", 0)

    absent._afs_exec = always_none
    assert await absent._awatchdog() is None
    assert await absent._awatchdog() is None
    assert asked["n"] == 2
    assert "_watchdog_tool" not in absent.__dict__


async def test_download_rejects_non_base64_noise():
    # Чужой текст в stdout (эхо профиля, предупреждение sh) из букв алфавита
    # base64 раньше вклеивался в байты файла при rc 0.
    from types import SimpleNamespace

    from gc_core.backends.remote_shell import RemoteShellBackend

    class Noisy(RemoteShellBackend):
        async def _run(self, *a, **k):
            return SimpleNamespace(
                stdout="Welcome to host!\naGk=\n",
                stderr="",
                return_code=0,
                timed_out=False,
                stdout_hit_capture_cap=False,
            )

    class Clean(Noisy):
        async def _run(self, *a, **k):
            # `base64` переносит строку каждые 76 символов: это одна строка данных
            return SimpleNamespace(
                stdout="aGlo\naQ==\n",
                stderr="",
                return_code=0,
                timed_out=False,
                stdout_hit_capture_cap=False,
            )

    kw = dict(
        cwd=None,
        env={},
        exec_init=None,
        exec_output_capture_max_chars=1000,
        walk_output_max_chars=1000,
    )
    with pytest.raises(OSError, match="non-base64"):
        await Noisy(["env"], **kw)._adownload_bytes("/f")
    assert await Clean(["env"], **kw)._adownload_bytes("/f") == b"hihi"
