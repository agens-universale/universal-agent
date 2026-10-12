import asyncio
import os
import shlex
import subprocess
import threading

import pytest
from gc_core.backends.harbor import HarborBackend, build_harbor_shell_command
from gc_core.backends.runtime_probe import (
    _RUNTIME_PROBE_TIMEOUT_SEC,
    RuntimeUnavailable,
)
from gc_testkit.runtime_probe import probe_stdout
from gc_core.backends.protocol import FsBackend
from gc_testkit.settings import make_settings


def _bash_runs(probe: str) -> bool:
    """Хостовый bash реально исполняет `probe`.

    Наличия имени мало: на Windows «bash» бывает заглушкой WSL без
    дистрибутива — имя есть, а запуск падает на execvpe(/bin/bash).
    """
    try:
        proc = subprocess.run(
            ["bash", "-c", probe], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _user_shell(command, *, cwd=None, env=None):
    return build_harbor_shell_command(
        command, cwd=cwd, env=env, exec_init="bashrc"
    )


def _probe_result(command):
    """Живой ответ пробы, если команда — она. Иначе ``None``."""
    hit = probe_stdout(command)
    if hit is None:
        return None
    return FakeExecResult(stdout=hit, return_code=0)


class FakeExecResult:
    def __init__(self, stdout="", stderr="", return_code=0, timed_out=False):
        self.stdout, self.stderr, self.return_code = (
            stdout,
            stderr,
            return_code,
        )
        self.timed_out = timed_out


class FakeEnv:
    def __init__(self):
        self.calls = []
        self.uploaded = []
        self.uploaded_bytes = []
        self.downloaded = []

    async def exec(self, command, **kw):
        # Проба не попадает в calls: тесты сверяют команды действия.
        hit = probe_stdout(command)
        if hit is not None:
            return FakeExecResult(stdout=hit, return_code=0)
        self.calls.append(command)
        if command.startswith("if test "):
            # Проверки существования отвечают явными 0/1 (см. PosixShellFsMixin._test).
            return FakeExecResult(stdout="1\n", return_code=0)
        return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    async def upload_file(self, src, dst):
        from pathlib import Path

        data = Path(src).read_bytes()
        self.uploaded.append((str(src), dst))
        self.uploaded_bytes.append(data)

    async def download_file(self, src, dst):
        from pathlib import Path

        Path(dst).write_bytes(b"old-content")
        self.downloaded.append((src, str(dst)))

    async def is_file(self, path, user=None):
        return path.endswith(".txt")

    async def is_dir(self, path, user=None):
        return path.endswith("/")


@pytest.fixture
def loop_in_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


async def test_aexec_reaches_the_harbor_loop_from_another_one(loop_in_thread):
    """Ход агента и loop Harbor — разные loop'ы, и это норма.

    Имя «мост sync→async» ушло вместе с синхронной формой: моста нет,
    ``_aawait`` ставит корутину env на loop владельца и ждёт её через
    ``wrap_future`` — у вызывающего это обычный ``await``, нового потока не
    появляется.
    """
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    result = await backend.aexec("echo hi")
    assert result.return_code == 0
    assert result.stdout == "ran:echo hi"
    assert env.calls == ["echo hi"]


async def test_write_uses_upload(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    await backend.write("/work/new.txt", "hello")
    assert env.uploaded and env.uploaded[0][1] == "/work/new.txt"


async def test_is_file_reads_shell_answer_and_raises_when_backend_is_down(
    loop_in_thread,
):
    """Нативный env.is_file врёт False на мёртвом Docker. Shell-ответ 0/1 — нет."""
    env = FakeEnv()
    answers = {
        "if test -f /work/a.txt; then echo 1; else echo 0; fi": FakeExecResult(
            stdout="1\n"
        ),
        "if test -f /work/dir; then echo 1; else echo 0; fi": FakeExecResult(
            stdout="0\n"
        ),
    }

    async def exec_answer(command, **kw):
        hit = probe_stdout(command)
        if hit is not None:
            return FakeExecResult(stdout=hit, return_code=0)
        env.calls.append(command)
        if command in answers:
            return answers[command]
        return FakeExecResult(
            stderr="Cannot connect to the Docker daemon at tcp://localhost:2375",
            return_code=1,
        )

    env.exec = exec_answer
    backend = HarborBackend(env, loop_in_thread)
    assert await backend.is_file("/work/a.txt") is True
    assert await backend.is_file("/work/dir") is False
    with pytest.raises(OSError, match="Docker daemon"):
        await backend.is_file("/dead")


async def test_is_dir_reads_shell_answer_and_raises_when_backend_is_down(
    loop_in_thread,
):
    """Нативный env.is_dir врёт False на мёртвом Docker. Shell-ответ 0/1 — нет."""
    env = FakeEnv()
    answers = {
        "if test -d /work; then echo 1; else echo 0; fi": FakeExecResult(
            stdout="1\n"
        ),
        "if test -d /missing; then echo 1; else echo 0; fi": FakeExecResult(
            stdout="0\n"
        ),
    }

    async def exec_answer(command, **kw):
        hit = probe_stdout(command)
        if hit is not None:
            return FakeExecResult(stdout=hit, return_code=0)
        env.calls.append(command)
        if command in answers:
            return answers[command]
        return FakeExecResult(
            stderr="Cannot connect to the Docker daemon at tcp://localhost:2375",
            return_code=1,
        )

    env.exec = exec_answer
    backend = HarborBackend(env, loop_in_thread)
    assert await backend.is_dir("/work") is True
    assert await backend.is_dir("/missing") is False
    with pytest.raises(OSError, match="Docker daemon"):
        await backend.is_dir("/dead")


def test_is_sandbox_and_default_exec_timeout(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    assert backend.is_sandbox is True
    # Timeout comes from agent.exec_timeout_sec; backend does not hardcode one.
    assert backend.default_exec_timeout is None
    assert backend.exec_output_capture_max_chars == 40_000


async def test_exec_bounds_large_stdout_to_capture_cap(loop_in_thread):
    """D8: Harbor ExecResult on agent is capped; hit_capture_cap set."""
    env = FakeEnv()
    huge = "X" * 1000

    async def big_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        env.calls.append(command)
        return FakeExecResult(stdout=huge, return_code=0)

    env.exec = big_exec
    backend = HarborBackend(env, loop_in_thread)
    backend.exec_output_capture_max_chars = 100
    result = await backend.aexec("echo huge")
    assert len(result.stdout) == 100
    assert result.stdout_hit_capture_cap is True
    assert result.stderr_hit_capture_cap is False
    assert result.return_code == 0


async def test_exec_per_call_capture_max_overrides_tool_cap(loop_in_thread):
    """Internal post-processed exec (native grep) may raise the RAM cap."""
    env = FakeEnv()
    huge = "X" * 1000

    async def big_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        return FakeExecResult(stdout=huge, return_code=0)

    env.exec = big_exec
    backend = HarborBackend(env, loop_in_thread)
    backend.exec_output_capture_max_chars = 100
    result = await backend.aexec("echo huge", capture_max_chars=500)
    assert len(result.stdout) == 500
    assert result.stdout_hit_capture_cap is True


async def test_exec_small_output_no_cap_hit(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    result = await backend.aexec("echo hi")
    assert result.stdout_hit_capture_cap is False
    assert result.stderr_hit_capture_cap is False


def test_implements_full_fsbackend_protocol(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    assert isinstance(backend, FsBackend)


async def test_listdir_parses_find_output(loop_in_thread):
    env = FakeEnv()
    env.exec = _make_exec_returning(env, "b\td\na\tf\n")
    backend = HarborBackend(env, loop_in_thread)
    result = await backend.listdir("/work")
    assert result == [("a", False), ("b", True)]


async def test_listdir_raises_on_nonzero_rc(loop_in_thread):
    # Ненулевой код без «No such file» в stderr — отказ транспорта/команды,
    # а не отсутствие каталога: OSError, но не FileNotFoundError.
    env = FakeEnv()
    env.exec = _make_exec_returning(env, "", return_code=1)
    backend = HarborBackend(env, loop_in_thread)
    with pytest.raises(OSError) as ei:
        await backend.listdir("/missing")
    assert not isinstance(ei.value, FileNotFoundError)


async def test_append_creates_file_if_missing(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)

    # exists() -> exec("test -e ...") returns rc 0 by default FakeEnv.exec, so
    # simulate missing file via a custom exec that fails "test -e" but succeeds otherwise.
    async def fake_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        env.calls.append(command)
        if command.startswith("test -e"):
            return FakeExecResult(stdout="", return_code=1)
        return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    env.exec = fake_exec
    await backend.append("/work/new.txt", "hello")
    assert env.uploaded and ".gc-append-tmp." in env.uploaded[0][1]
    assert env.uploaded[0][1].startswith("/work/new.txt.gc-append-tmp.")
    assert any(">>" in c and "/work/new.txt" in c for c in env.calls)


async def test_append_concatenates_existing_content(loop_in_thread):
    env = FakeEnv()
    uploaded_payloads = []

    async def fake_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        env.calls.append(command)
        return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    async def fake_upload_file(src, dst):
        from pathlib import Path

        uploaded_payloads.append((dst, Path(src).read_bytes()))
        env.uploaded.append((str(src), dst))

    env.exec = fake_exec
    env.upload_file = fake_upload_file
    backend = HarborBackend(env, loop_in_thread)
    await backend.append("/work/f.txt", "more")
    assert len(uploaded_payloads) == 1
    remote_tmp, payload = uploaded_payloads[0]
    assert remote_tmp.startswith("/work/f.txt.gc-append-tmp.")
    assert ".gc-append-tmp." in remote_tmp
    assert payload == b"more"
    assert any(
        "cat " in c and ">>" in c and "/work/f.txt" in c for c in env.calls
    )
    assert not any(c.startswith("test -e") for c in env.calls)


async def test_exec_env_prefix_quoting(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    await backend.aexec("echo hi", env={"FOO": "bar baz"})
    assert env.calls == [_user_shell("echo hi", env={"FOO": "bar baz"})]
    assert env.calls[0].startswith("env FOO='bar baz' bash -c ")


async def test_exec_env_prefix_with_cwd(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    await backend.aexec("echo hi", cwd="/work", env={"FOO": "bar"})
    wired = env.calls[0]
    assert wired == _user_shell("echo hi", cwd="/work", env={"FOO": "bar"})
    assert wired.startswith("cd /work && env FOO=bar bash -c ")
    assert not wired.startswith("env ")


async def test_exec_env_prefix_applies_to_whole_command(
    loop_in_thread, tmp_path
):
    """`env` must not swallow `cd`/`&&`: with exec_init the script is one bash -c arg."""
    work = tmp_path / "proj"
    work.mkdir()
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    await backend.aexec(
        'cd sub && printf "%s\\n" "$FOO" && printf "%s\\n" "$(pwd)"',
        cwd=str(work),
        env={"FOO": "bar baz"},
    )
    script = env.calls[0]
    assert script.startswith(
        f"cd {shlex.quote(str(work))} && env FOO='bar baz' bash -c "
    )
    # cwd снаружи, скрипт агента — один закавыченный аргумент, не префикс env.
    assert "cd sub &&" in script.split("bash -c ", 1)[1]

    # Дальше путь хоста играет роль cwd POSIX-контейнера: на Windows bash
    # вернёт из pwd /c/… вместо C:\…, сверять не с чем.
    if os.name == "nt":
        pytest.skip(
            "исполнение скрипта с путём хоста как cwd контейнера — только POSIX-хост"
        )
    (work / "sub").mkdir()
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"bar baz\n{work / 'sub'}\n"


async def test_run_python_env_survives_bash_c_wrap(loop_in_thread):
    """Heredoc of run_python must still run after the env prefix is quoted."""
    if not _bash_runs("python3 -c pass"):
        pytest.skip("нет рабочего bash с python3 на хосте")
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    await backend.arun_python(
        "import os\nprint(os.environ['FOO'])\n", env={"FOO": "it's"}
    )
    proc = subprocess.run(
        ["bash", "-c", env.calls[0]],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "it's\n"


async def test_exec_raw_is_bare_posix(loop_in_thread):
    """Сырой канал игнорирует инициализацию, даже когда она задана."""
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    result = await backend.aexec_raw("echo hi")
    assert result.stdout == "ran:echo hi"
    assert env.calls == ["echo hi"]


async def test_exec_raw_env_cwd_without_profile(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    await backend.aexec_raw("echo hi", cwd="/work", env={"FOO": "bar baz"})
    assert env.calls == ["cd /work && env FOO='bar baz' echo hi"]
    assert ".bashrc" not in env.calls[0]


async def test_argv_and_python_go_raw_even_with_exec_init(loop_in_thread):
    """argv — программа, python — heredoc: оба идут сырым каналом, как у develop."""
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    await backend.aexec_argv(["echo", "hi"])
    await backend.arun_python("print(1)")
    assert len(env.calls) == 2
    assert not any(".bashrc" in c for c in env.calls), env.calls


def test_build_harbor_shell_command_quotes_user_inner():
    import shlex

    from gc_core.backends.harbor import _BASHRC_EXEC_INIT

    wired = build_harbor_shell_command(
        "echo hi", cwd="/work", env={"FOO": "bar baz"}, exec_init="bashrc"
    )
    assert wired.startswith("cd /work && env FOO='bar baz' bash -c ")
    inner = shlex.split(wired)[-1]
    assert inner.startswith(_BASHRC_EXEC_INIT)
    assert inner.endswith("echo hi")
    raw = build_harbor_shell_command("echo hi", cwd="/work", env={"FOO": "x"})
    assert raw == "cd /work && env FOO=x echo hi"


def test_bashrc_init_keeps_env_path_and_profile_additions(tmp_path):
    """Debian-профиль переписывает PATH. Порядок образа остаётся.

    Впереди только каталоги, которых не было в ENV (conda и системные,
    отсутствовавшие в образе). Уже присутствующие не дублируются, шум
    профиля не попадает в stdout.
    """
    if not _bash_runs("true"):
        pytest.skip("нет рабочего bash на хосте")
    from gc_core.backends.harbor import _BASHRC_EXEC_INIT

    profile = tmp_path / "profile"
    profile.write_text(
        "echo PROFILE_STDOUT\n"
        "echo PROFILE_STDERR >&2\n"
        'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
        "export PATH\n"
        "set -e\n"
        "false\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    glob_dir = tmp_path / "glob"
    glob_dir.mkdir()
    (glob_dir / "hit").write_text("", encoding="utf-8")
    (home / ".bashrc").write_text(
        'echo BASHRC_STDOUT\nexport PATH="/opt/conda/bin:$PATH"\n',
        encoding="utf-8",
    )
    init = _BASHRC_EXEC_INIT.replace(
        ". /etc/profile >/dev/null 2>&1 || true",
        f". {shlex.quote(str(profile))} >/dev/null 2>&1 || true",
        1,
    )
    script = (
        init
        + "printf '%s\\n' \"$PATH\"\n"
        + "printf 'saved=%s\\n' \"${_gc_saved_path-unset}\"\n"
        + "printf 'glob=%s\\n' \"$GC_GLOB_DIR\"/hi?\n"
    )
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/local/go/bin:/opt/gc tool:/opt/star*:/usr/bin:/bin",
            "HOME": str(home),
            "GC_GLOB_DIR": str(glob_dir),
        },
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[1:] == ["saved=unset", f"glob={glob_dir / 'hit'}"], (
        proc.stdout
    )
    assert "PROFILE_STDOUT" not in proc.stdout
    assert "BASHRC_STDOUT" not in proc.stdout
    assert proc.stderr == ""
    assert lines[0] == ":".join(
        [
            "/opt/conda/bin",
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/sbin",
            "/usr/local/go/bin",
            "/opt/gc tool",
            "/opt/star*",
            "/usr/bin",
            "/bin",
        ]
    )
    image_path = (
        "/opt/venv/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:"
        "/usr/sbin:/usr/bin:/sbin:/bin"
    )
    image_proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": image_path,
            "HOME": str(home),
            "GC_GLOB_DIR": str(glob_dir),
        },
    )
    assert image_proc.returncode == 0, image_proc.stderr
    assert image_proc.stdout.splitlines()[0] == "/opt/conda/bin:" + image_path


async def test_exec_defaults_to_raw_and_custom_init_is_prefixed(
    loop_in_thread,
):
    import shlex

    env = FakeEnv()
    await HarborBackend(env, loop_in_thread).aexec("echo raw")
    assert env.calls == ["echo raw"]

    env.calls.clear()
    await HarborBackend(env, loop_in_thread, exec_init="export READY=1").aexec(
        "echo custom"
    )
    inner = shlex.split(env.calls[0])[-1]
    assert inner == "export READY=1\necho custom"


def test_every_impl_signature_accepts_protocol_parameters():
    # ABC registration only checks method NAMES; this guards signatures.
    # For each abstract method, a protocol-shaped call (keyword-capable params
    # passed by name — catches renamed/missing params like write() without
    # `encoding`) must bind onto the HarborBackend implementation.
    import inspect

    for name in sorted(FsBackend.__abstractmethods__):
        proto = inspect.signature(getattr(FsBackend, name))
        impl = inspect.signature(getattr(HarborBackend, name))
        args, kwargs = [None], {}  # leading None stands for self
        for p in list(proto.parameters.values())[1:]:
            if p.kind == p.POSITIONAL_ONLY:
                args.append(None)
            else:
                kwargs[p.name] = None
        try:
            impl.bind(*args, **kwargs)
        except TypeError as e:
            pytest.fail(
                f"HarborBackend.{name}{impl} rejects protocol call {proto}: {e}"
            )


async def test_write_and_append_accept_encoding_kwarg(loop_in_thread):
    # Regression guard: unified fs tools call await backend.write(..., encoding=...)
    # (e.g. fs_write_file); must not TypeError against the container backend.
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    await backend.write("/work/a.txt", "data", encoding="utf-8")
    await backend.append("/work/a.txt", "more", encoding="utf-8")
    assert any(dst == "/work/a.txt" for _, dst in env.uploaded)
    assert any(".gc-append-tmp." in dst for _, dst in env.uploaded)


async def test_harbor_write_bytes_preserves_cp1251(loop_in_thread):
    """Harbor text-write must encode on host and upload raw bytes (not force utf-8)."""
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    text = "Привет"
    await backend.write("/work/ru.txt", text, encoding="cp1251")
    assert env.uploaded_bytes[-1] == text.encode("cp1251")
    assert env.uploaded_bytes[-1] != text.encode("utf-8")


def _make_exec_returning(env, stdout, return_code=0):
    async def fake_exec(command, **kw):
        hit = probe_stdout(command)
        if hit is not None:
            return FakeExecResult(stdout=hit, return_code=0)
        env.calls.append(command)
        return FakeExecResult(stdout=stdout, return_code=return_code)

    return fake_exec


async def test_exec_propagates_timed_out_from_environment(loop_in_thread):
    env = FakeEnv()

    async def timed_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        env.calls.append(command)
        return FakeExecResult(
            stdout="partial", stderr="", return_code=None, timed_out=True
        )

    env.exec = timed_exec
    backend = HarborBackend(env, loop_in_thread)
    result = await backend.aexec("sleep 99", timeout=1)
    assert result.timed_out is True
    assert result.return_code is None
    assert result.stdout == "partial"


async def test_read_head_bytes_uses_head_not_full_file(loop_in_thread):
    import base64

    env = FakeEnv()
    raw = b"hel"

    async def head_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        env.calls.append(command)
        if "head -c" in command and "base64" in command:
            # Default GNU wrap (76 cols). ``-w 0`` is a one-line blob and
            # trips Harbor's 64 KiB readline.
            wrapped = base64.encodebytes(raw).decode("ascii")
            return FakeExecResult(stdout=wrapped, return_code=0)
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread)
    sample = await backend.read_head_bytes("/work/a.bin", 3)
    assert sample == raw
    assert any(
        c.startswith("head -c 3 ") and c.endswith("| base64")
        for c in env.calls
    )


async def test_read_head_bytes_append_sample_not_cut_by_tool_60k(
    loop_in_thread,
):
    """fs_append samples 100KiB; base64 (~137k) exceeds tool-exec 60k."""
    import base64

    from gc_core.backends.protocol import DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    from gc_core.utils.file_text import APPEND_HEAD_SAMPLE_BYTES

    env = FakeEnv()
    raw = b"A" * APPEND_HEAD_SAMPLE_BYTES
    encoded = base64.b64encode(raw).decode("ascii")
    assert len(encoded) > DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS

    async def head_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        env.calls.append(command)
        if "head -c" in command and "base64" in command:
            return FakeExecResult(stdout=encoded, return_code=0)
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread)
    sample = await backend.read_head_bytes(
        "/work/big.txt", APPEND_HEAD_SAMPLE_BYTES
    )
    assert sample == raw

    async def huge_tool(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        return FakeExecResult(stdout="Y" * 100_000, return_code=0)

    env.exec = huge_tool
    tool = await backend.aexec("echo y")
    assert len(tool.stdout) == DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    assert tool.stdout_hit_capture_cap is True


async def test_read_head_bytes_raises_if_capture_ceiling_hit(loop_in_thread):
    import base64

    env = FakeEnv()
    raw = b"B" * 200
    encoded = base64.b64encode(raw).decode("ascii")

    async def head_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        if "head -c" in command and "base64" in command:
            return FakeExecResult(stdout=encoded, return_code=0)
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread, walk_output_max_chars=50)
    with pytest.raises(OSError, match="capture ceiling"):
        await backend.read_head_bytes("/work/a.bin", 200)


async def test_read_head_bytes_preserves_non_utf8_bytes(loop_in_thread):
    """D: raw bytes must survive exec (no utf-8/surrogateescape mangling)."""
    import base64

    env = FakeEnv()
    # High bytes + NUL — would break under encode("utf-8") of a text stdout.
    raw = bytes([0xCF, 0xF0, 0xE8, 0xE2, 0xE5, 0xF2, 0x00, 0xFF])

    async def head_exec(command, **kw):
        probed = _probe_result(command)
        if probed is not None:
            return probed
        env.calls.append(command)
        if "head -c" in command and "base64" in command:
            return FakeExecResult(
                stdout=base64.b64encode(raw).decode("ascii"),
                return_code=0,
            )
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread)
    sample = await backend.read_head_bytes("/work/legacy.txt", len(raw))
    assert sample == raw
    assert b"\x00" in sample
    assert sample[0] == 0xCF


# --- walk_entries: prune внутри find, размер из -printf %s ----------------------


async def test_walk_entries_prunes_in_find_and_parses_sizes(loop_in_thread):
    env = FakeEnv()
    env.exec = _make_exec_returning(env, "src\td\t4096\nsrc/a.py\tf\t12\n")
    backend = HarborBackend(env, loop_in_thread)

    entries = await backend.walk_entries(
        "/work", prune_names=("node_modules",), prune_dot_dirs=True
    )

    assert [(e.rel, e.is_dir, e.size) for e in entries] == [
        ("src", True, None),
        ("src/a.py", False, 12),
    ]
    cmd = env.calls[-1]
    # -H: паритет с os.walk по симлинк-вершине; -printf ДО -prune: сам каталог в листинге остаётся.
    assert cmd.startswith(
        "find -H /work -mindepth 1 '(' -type d -name node_modules -o -type d -name '.*' ')' -printf"
    )
    assert " -prune -o -printf" in cmd
    assert cmd.endswith(r"-printf '%P\t%y\t%s\n'")


async def test_walk_keeps_tuples_and_never_prunes(loop_in_thread):
    env = FakeEnv()
    env.exec = _make_exec_returning(env, "src\td\t4096\nsrc/a.py\tf\t12\n")
    backend = HarborBackend(env, loop_in_thread)

    assert await backend.walk("/work") == [("src", True), ("src/a.py", False)]
    assert "-prune" not in env.calls[-1]


async def test_walk_entries_hit_ceiling_raises(loop_in_thread):
    from gc_core.backends.protocol import WalkOutputTooLargeError

    env = FakeEnv()
    env.exec = _make_exec_returning(env, "x\tf\t1\n" * 200)
    backend = HarborBackend(env, loop_in_thread, walk_output_max_chars=500)
    with pytest.raises(WalkOutputTooLargeError):
        await backend.walk_entries("/work")


def test_normpath_collapses_dot_and_double_slash(loop_in_thread):
    """find печатает пути префиксом вызова; prune сравнивает строки —
    rstrip-нормализация пропускала «/проект//x/./y»."""
    backend = HarborBackend(FakeEnv(), loop_in_thread)
    assert backend.normpath("/w//x/./y/") == "/w/x/y"
    assert backend.normpath("/w/a/../b") == "/w/b"
    assert backend.normpath("/") == "/"


# --- асинхронный порт запуска -------------------------------------------------
#
# Под Harbor уже нативный async: `env.exec` — корутина. Пока бэкенд объявлял
# только синхронные имена, `aexec_port` заворачивал его в `SyncExecAdapter`, и
# КАЖДЫЙ запуск команды уходил в поток из общего пула — при том, что ждать
# было нечего, кроме чужого loop.


def test_harbor_declares_the_async_port_and_skips_the_adapter(loop_in_thread):
    """`aexec_port` отдаёт сам бэкенд, а не обёртку с потоком."""
    from gc_core.backends.async_protocol import SyncExecAdapter, aexec_port

    backend = HarborBackend(FakeEnv(), loop_in_thread)
    port = aexec_port(backend)

    assert port is backend, (
        "Harbor снова заворачивается в SyncExecAdapter — значит запуск команды опять уходит в поток из общего пула"
    )
    assert not isinstance(port, SyncExecAdapter)


async def test_aexec_runs_without_taking_a_thread(loop_in_thread):
    """Асинхронный запуск идёт на loop владельца, не занимая ни одного потока.

    Проверяется именно ЭТО свойство, а не «результат совпал»: результат
    совпадал и через `SyncExecAdapter`, только ценой слота в пуле на каждый
    запуск.
    """
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)

    # Считаем СОЗДАНИЕ потоков, а не живые: короткий поток успевает умереть до
    # замера, и `active_count()` возвращается к базе — такой зонд регрессию
    # пропускает (проверено подменой на соседнем тесте).
    spawned: list = []
    real_start = threading.Thread.start

    def _counting_start(self):
        spawned.append(self.name)
        return real_start(self)

    threading.Thread.start = _counting_start
    try:
        results = await asyncio.gather(
            *(backend.aexec(f"echo {i}") for i in range(5))
        )
    finally:
        threading.Thread.start = real_start

    assert [r.stdout for r in results] == [f"ran:echo {i}" for i in range(5)]
    assert spawned == [], (
        f"пять асинхронных запусков завели потоки {spawned} — путь снова идёт через пул"
    )


async def test_every_exec_entry_funnels_through_one_command_builder(
    loop_in_thread,
):
    """Три входа (команда, argv, python) собирают команду ОДНИМ составителем.

    Раньше здесь сравнивались синхронная и асинхронная формы: «расхождение
    означало бы, что они запускают РАЗНЫЕ команды». Синхронной формы больше
    нет, и в этом виде тест сравнивал бы вызов с самим собой — то есть зеленел
    бы при любой поломке составителя.

    Проверяется то же свойство, но по существу: у каждого входа команда равна
    той, что даёт общий составитель. Разъехаться им негде — а цена
    расхождения прежняя: оба входа «работают», просто запускают разное.
    """
    from gc_core.backends.protocol import argv_to_posix_command

    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)

    await backend.aexec("ls", cwd="/work", env={"A": "1"})
    expected = build_harbor_shell_command("ls", cwd="/work", env={"A": "1"})
    assert env.calls == [expected]
    assert "cd " in expected and "A=1" in expected, expected

    env.calls.clear()
    argv = ["grep", "-n", "TOKEN", "/work/a b.py"]
    await backend.aexec_argv(argv)
    assert env.calls == [
        build_harbor_shell_command(
            argv_to_posix_command(argv), cwd=None, env=None
        )
    ]

    env.calls.clear()
    await backend.arun_python("print(1)")
    assert env.calls == [
        build_harbor_shell_command(
            backend._python_command("print(1)"), cwd=None, env=None
        )
    ]


async def test_fs_helpers_use_raw_channel(loop_in_thread):
    """Помощники ФС — сырым каналом, и их проверки возвращают bool.

    Тест из develop; вошёл вместе с починкой #5 ревью: у `exists`, `lexists`
    и `is_symlink` не хватало скобок у `await` — `.return_code` брался у
    корутины, и любой прогон в Harbor падал на первой же проверке файла.
    Ни один тест этих методов не исполнял: фейк тест-кита — отдельный класс.
    """
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    assert await backend.exists("/work/a.txt") is True
    assert env.calls == [
        "if test -e /work/a.txt; then echo 1; else echo 0; fi"
    ]
    assert await backend.lexists("/work/a.txt") is True
    assert await backend.is_symlink("/work/a.txt") is True
    env.calls.clear()
    await backend.walk_entries("/work")
    assert env.calls[-1].startswith("find -H /work")
    assert not any(".bashrc" in c for c in env.calls)


async def test_workspace_root_is_the_container_workdir(loop_in_thread):
    """Корень рабочей папки — WORKDIR контейнера, а не `/`.

    Синхронный `discover_backend_cwd` спрашивал `exec("pwd")`, а синхронного
    `exec` у Harbor больше нет: отказ глотался, и корнем становился `/`.
    Теперь WORKDIR прогревается корутиной при сборке контекста.
    """
    from gc_core.execution_workspace import (
        aprime_backend_cwd,
        build_execution_workspace,
    )

    class _PwdEnv(FakeEnv):
        async def exec(self, command, **kw):
            probed = _probe_result(command)
            if probed is not None:
                return probed
            self.calls.append(command)
            if command == "pwd":
                return FakeExecResult(stdout="/testbed\n", return_code=0)
            return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    env = _PwdEnv()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    assert backend.default_cwd() is None
    await aprime_backend_cwd(backend)
    assert backend.default_cwd() == "/testbed"
    assert env.calls == ["pwd"], (
        "pwd — внутренний вызов: сырой канал, без профиля"
    )
    assert (
        build_execution_workspace(backend, make_settings()).root == "/testbed"
    )


async def test_probe_runs_before_the_command_and_does_not_cd(loop_in_thread):
    """Сумма — первый exec, без cd. Команда модели идёт следом."""
    env = FakeEnv()
    recorded = []

    async def exec_record(command, **kw):
        recorded.append(command)
        probed = _probe_result(command)
        if probed is not None:
            return probed
        return FakeExecResult(stdout="ok\n", return_code=0)

    env.exec = exec_record
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")
    result = await backend.aexec("echo hi", cwd="/work")
    assert result.stdout == "ok\n"
    assert probe_stdout(recorded[0]) is not None
    assert "cd " not in recorded[0]
    assert recorded[1] == _user_shell("echo hi", cwd="/work")


async def test_successful_probe_is_reused_until_ttl(
    loop_in_thread, monkeypatch
):
    """Вторая команда в окне TTL не шлёт echo. После срока — снова."""
    env = FakeEnv()
    recorded = []

    async def exec_record(command, **kw):
        recorded.append(command)
        probed = _probe_result(command)
        if probed is not None:
            return probed
        return FakeExecResult(stdout="ok\n", return_code=0)

    env.exec = exec_record
    clock = {"t": 1_000.0}
    monkeypatch.setattr(
        "gc_core.backends.runtime_probe.time.monotonic", lambda: clock["t"]
    )
    backend = HarborBackend(env, loop_in_thread)
    await backend.aexec("echo one")
    await backend.aexec("echo two")
    assert sum(probe_stdout(cmd) is not None for cmd in recorded) == 1
    clock["t"] += 11
    await backend.aexec("echo three")
    assert sum(probe_stdout(cmd) is not None for cmd in recorded) == 2


async def test_failed_probe_does_not_run_the_command(loop_in_thread):
    env = FakeEnv()
    recorded = []

    async def exec_dead(command, **kw):
        recorded.append(command)
        return FakeExecResult(stderr="permission denied\n", return_code=1)

    env.exec = exec_dead
    backend = HarborBackend(env, loop_in_thread)
    with pytest.raises(
        RuntimeUnavailable, match="Runtime connection failed"
    ) as err:
        await backend.aexec("echo hi")
    assert "permission denied" in str(err.value)
    assert "turn_finish" in str(err.value)
    assert len(recorded) == 1
    assert probe_stdout(recorded[0]) is not None


async def test_short_command_timeout_does_not_shrink_the_probe(loop_in_thread):
    """``timeout: 1`` у команды не режет echo: проба всегда ждёт свои 60 с."""
    env = FakeEnv()
    seen = []

    async def exec_record(command, **kw):
        if probe_stdout(command) is not None:
            seen.append(kw.get("timeout_sec"))
            return FakeExecResult(stdout=probe_stdout(command), return_code=0)
        return FakeExecResult(stdout="ok\n", return_code=0)

    env.exec = exec_record
    backend = HarborBackend(env, loop_in_thread)
    await backend.aexec("echo hi", timeout=1)
    assert seen == [_RUNTIME_PROBE_TIMEOUT_SEC]


async def test_probe_timeout_is_runtime_unavailable(loop_in_thread):
    env = FakeEnv()

    async def exec_slow(command, **kw):
        return FakeExecResult(return_code=None, timed_out=True)

    env.exec = exec_slow
    backend = HarborBackend(env, loop_in_thread)
    with pytest.raises(RuntimeUnavailable) as err:
        await backend.aexec("echo hi")
    assert err.value.timed_out is True
    assert "turn_finish" in str(err.value)


async def test_upload_probes_before_mkdir(loop_in_thread):
    env = FakeEnv()
    recorded = []

    async def exec_record(command, **kw):
        recorded.append(command)
        probed = _probe_result(command)
        if probed is not None:
            return probed
        return FakeExecResult(stdout="", return_code=0)

    env.exec = exec_record
    backend = HarborBackend(env, loop_in_thread)
    await backend.write_bytes("/work/a.bin", b"x")
    assert probe_stdout(recorded[0]) is not None
    assert recorded[1].startswith("mkdir -p ")
    assert env.uploaded_bytes == [b"x"]


def test_env_prefix_rejects_non_identifier_names():
    from gc_core.backends.harbor import _env_assignment_prefix

    with pytest.raises(ValueError, match="invalid environment variable name"):
        _env_assignment_prefix({"X=1; touch /tmp/pwned; Y": "v"})
    assert _env_assignment_prefix({"OK": "v"}) == "env OK=v "
