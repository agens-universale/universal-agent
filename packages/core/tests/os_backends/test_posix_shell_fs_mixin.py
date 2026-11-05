# packages/core/tests/os_backends/test_posix_shell_fs_mixin.py
"""PosixShellFsMixin: файловые операции как POSIX-команды поверх трёх примитивов."""

import shlex

import pytest
from gc_core.backends.posix_shell_fs import PosixShellFsMixin
from gc_core.backends.protocol import ExecResult, FsBackend
from gc_core.backends.walk_util import FlatWalkMixin


class RecordingShell(PosixShellFsMixin, FlatWalkMixin, FsBackend):
    """Запоминает команды; ответы задаются словарём «подстрока → ExecResult»."""

    is_sandbox = True
    walk_output_max_chars = 1_000_000

    def __init__(self, answers=None):
        self._discovered_cwd = None
        self.commands: list[str] = []
        self.uploaded: list[tuple[str, bytes]] = []
        self.store: dict[str, bytes] = {}
        self._answers = answers or {}

    async def _afs_exec(self, command, *, timeout=None):
        self.commands.append(command)
        for needle, res in self._answers.items():
            if needle in command:
                return res
        if command.startswith("if test "):
            return ExecResult(
                return_code=0, stdout="1\n"
            )  # «есть» по умолчанию
        return ExecResult(return_code=0)

    async def _aupload_bytes(self, data, path):
        self.uploaded.append((path, data))
        self.store[path] = data

    async def _adownload_bytes(self, path):
        return self.store[path]

    async def aexec(
        self,
        command,
        *,
        cwd=None,
        timeout=None,
        env=None,
        capture_max_chars=None,
    ):
        return await self._afs_exec(command)

    async def aexec_raw(self, command, **kw):
        return await self._afs_exec(command)

    async def aexec_argv(self, argv, **kw):
        return await self._afs_exec(" ".join(argv))

    async def arun_python(self, code, **kw):
        return await self._afs_exec(code)


def test_mixin_completes_the_protocol():
    assert not FsBackend.__abstractmethods__ - set(dir(RecordingShell))
    RecordingShell()  # не абстрактный


async def test_exists_runs_test_e():
    # Ответ — явные 0/1 в stdout: код возврата транспорта и «нет файла»
    # иначе неразличимы (см. test_transport_failure_is_an_error_not_a_missing_file).
    be = RecordingShell(
        answers={"test -e": ExecResult(return_code=0, stdout="1\n")}
    )
    assert await be.exists("/w/a.txt") is True
    assert be.commands == ["if test -e /w/a.txt; then echo 1; else echo 0; fi"]


async def test_is_file_and_is_dir_default_to_shell_test():
    be = RecordingShell(
        answers={
            "test -f": ExecResult(return_code=0, stdout="0\n"),
            "test -d": ExecResult(return_code=0, stdout="1\n"),
        }
    )
    assert await be.is_file("/w/x") is False
    assert await be.is_dir("/w") is True
    assert be.commands == [
        "if test -f /w/x; then echo 1; else echo 0; fi",
        "if test -d /w; then echo 1; else echo 0; fi",
    ]


async def test_write_encodes_and_uploads():
    be = RecordingShell()
    await be.write("/w/ru.txt", "привет", encoding="cp1251")
    assert be.uploaded == [("/w/ru.txt", "привет".encode("cp1251"))]


async def test_read_downloads_and_decodes():
    be = RecordingShell()
    be.store["/w/a.txt"] = "hi\n".encode()
    assert await be.read("/w/a.txt") == "hi\n"


async def test_getsize_parses_stat():
    be = RecordingShell(
        answers={"stat -c %s": ExecResult(stdout="42\n", return_code=0)}
    )
    assert await be.getsize("/w/a") == 42


async def test_quoting_of_awkward_paths():
    be = RecordingShell(
        answers={"stat -c %s": ExecResult(stdout="1\n", return_code=0)}
    )
    p = "/w/it's here/a b.txt"
    await be.exists(p)
    await be.getsize(p)
    for cmd in be.commands:
        assert shlex.quote(p) in cmd


async def test_listdir_parses_find_output():
    out = "a\tf\nsub\td\n"
    be = RecordingShell(
        answers={"-maxdepth 1": ExecResult(stdout=out, return_code=0)}
    )
    assert await be.listdir("/w") == [("a", False), ("sub", True)]


async def test_listdir_raises_on_nonzero_rc():
    # «Каталога нет» — только когда так сказала та сторона; молчаливый отказ
    # (убитый find, пустой stderr) — OSError, иначе модель создаёт каталог заново.
    gone = RecordingShell(
        answers={
            "-maxdepth 1": ExecResult(
                return_code=1,
                stderr="find: '/nope': No such file or directory",
            )
        }
    )
    with pytest.raises(FileNotFoundError):
        await gone.listdir("/nope")
    silent = RecordingShell(answers={"-maxdepth 1": ExecResult(return_code=1)})
    with pytest.raises(OSError) as ei:
        await silent.listdir("/nope")
    assert not isinstance(ei.value, FileNotFoundError)


async def test_aprime_default_cwd_runs_pwd_once():
    be = RecordingShell(
        answers={"pwd": ExecResult(stdout="/work\n", return_code=0)}
    )
    assert await be.aprime_default_cwd() == "/work"
    assert await be.aprime_default_cwd() == "/work"
    assert be.commands.count("pwd") == 1
    assert be.default_cwd() == "/work"


async def test_transport_failure_is_an_error_not_a_missing_file():
    # Остановленный контейнер / мёртвый ssh: `test` не выполнился вовсе —
    # это OSError, а не «файла нет» (иначе модель пересоздаёт существующее).
    dead = RecordingShell(
        answers={
            "test -e": ExecResult(
                return_code=1,
                stderr="Error response from daemon: container is not running",
            )
        }
    )
    with pytest.raises(OSError, match="not running"):
        await dead.exists("/etc")
    alive = RecordingShell(
        answers={
            "test -e /missing": ExecResult(return_code=0, stdout="0\n"),
            "test -d /etc": ExecResult(return_code=0, stdout="1\n"),
        }
    )
    assert await alive.exists("/missing") is False
    assert await alive.is_dir("/etc") is True


async def test_busybox_find_is_reported_not_treated_as_empty_dir():
    busybox = RecordingShell(
        answers={
            "-printf": ExecResult(
                return_code=1, stderr="find: unrecognized: -printf"
            ),
        }
    )
    with pytest.raises(OSError, match="GNU find"):
        await busybox.listdir("/etc")
    with pytest.raises(OSError, match="GNU find"):
        await busybox.walk_entries("/etc")
    gone = RecordingShell(
        answers={
            "-printf": ExecResult(
                return_code=1,
                stderr="find: '/nope': No such file or directory",
            )
        }
    )
    with pytest.raises(FileNotFoundError):
        await gone.listdir("/nope")


async def test_stat_and_find_timeouts_are_errors_not_missing_files():
    hung = RecordingShell(
        answers={
            "stat -c": ExecResult(return_code=None, timed_out=True),
            "-printf": ExecResult(return_code=None, timed_out=True),
        }
    )
    with pytest.raises(OSError, match="timed out"):
        await hung.stat("/w/a.txt")
    with pytest.raises(OSError, match="timed out"):
        await hung.listdir("/w")
    denied = RecordingShell(
        answers={
            "stat -c": ExecResult(
                return_code=1,
                stderr="stat: cannot statx '/w/a': Permission denied",
            )
        }
    )
    with pytest.raises(PermissionError):
        await denied.stat("/w/a")
    gone = RecordingShell(
        answers={
            "stat -c": ExecResult(
                return_code=1,
                stderr="stat: cannot statx '/w/a': No such file or directory",
            )
        }
    )
    with pytest.raises(FileNotFoundError):
        await gone.stat("/w/a")
    silent = RecordingShell(
        answers={"stat -c": ExecResult(return_code=1, stderr="")}
    )
    with pytest.raises(OSError) as ei:
        await silent.stat("/w/a")
    assert not isinstance(ei.value, FileNotFoundError)


async def test_head_bytes_rejects_non_base64_noise():
    noisy = RecordingShell(
        answers={
            "| base64": ExecResult(return_code=0, stdout="Welcome!\naGk=\n")
        }
    )
    with pytest.raises(OSError, match="non-base64"):
        await noisy.read_head_bytes("/w/a", 10)
    clean = RecordingShell(
        answers={"| base64": ExecResult(return_code=0, stdout="aGlo\naQ==\n")}
    )
    assert await clean.read_head_bytes("/w/a", 10) == b"hihi"


async def test_readable_writable_ask_the_remote_side():
    be = RecordingShell(
        answers={
            "test -r": ExecResult(return_code=0, stdout="1\n"),
            "test -w": ExecResult(return_code=0, stdout="0\n"),
        }
    )
    assert await be.readable("/etc/shadow") is True
    assert await be.writable("/etc/shadow") is False


_HELP = "help " * 400


async def test_find_failure_shows_stdout_when_stderr_empty():
    out = "find: unrecognized: -printf\n" + _HELP
    be = RecordingShell(
        answers={"-printf": ExecResult(return_code=1, stdout=out, stderr="")}
    )
    for call in (be.listdir, be.walk_entries):
        with pytest.raises(OSError) as ei:
            await call("/etc")
        assert not isinstance(ei.value, FileNotFoundError)
        message = str(ei.value)
        prefix = "find failed (exit 1): /etc"
        assert message.startswith(prefix + ": find: unrecognized: -printf")
        assert len(message) <= len(prefix) + 2 + 500


async def test_huge_failure_output_is_bounded():
    huge_stdout = RecordingShell(
        answers={
            "-printf": ExecResult(
                return_code=1, stdout="x\tf\t1\n" * 200_000, stderr=""
            )
        }
    )
    with pytest.raises(OSError) as ei:
        await huge_stdout.walk_entries("/w")
    message = str(ei.value)
    prefix = "find failed (exit 1): /w"
    assert message.startswith(prefix + ": x\tf\t1")
    assert "[середина опущена]" in message
    assert len(message) <= len(prefix) + 2 + 500
    huge_stderr = RecordingShell(
        answers={
            "-printf": ExecResult(
                return_code=1, stderr="find: a: Permission denied\n" * 40_000
            )
        }
    )
    with pytest.raises(PermissionError) as ei:
        await huge_stderr.walk_entries("/w")
    assert str(ei.value).startswith("find: a: Permission denied")
    assert "[середина опущена]" in str(ei.value)
    assert len(str(ei.value)) <= 500


@pytest.mark.parametrize(
    ("call", "prefix"),
    [
        (lambda be: be.stat("/w/a"), "stat failed (exit 1): /w/a"),
        (lambda be: be.getsize("/w/a"), "stat failed (exit 1): /w/a"),
        (
            lambda be: be.copy("/w/a", "/w/b"),
            "cp failed (exit 1): /w/a -> /w/b",
        ),
        (lambda be: be.makedirs("/w/d"), "mkdir failed (exit 1): /w/d"),
        (
            lambda be: be.read_head_bytes("/w/a", 8),
            "head failed (exit 1): /w/a",
        ),
        (lambda be: be.sha256("/w/a"), "sha256sum failed (exit 1): /w/a"),
        (
            lambda be: be.append("/w/a", "x"),
            "append failed (exit 1): /w/a",
        ),
    ],
)
async def test_single_command_failures_show_stdout(call, prefix):
    be = RecordingShell(
        answers={
            "": ExecResult(return_code=1, stdout="boom: reason\n", stderr="")
        }
    )
    with pytest.raises(OSError) as ei:
        await call(be)
    assert str(ei.value) == prefix + ": boom: reason"


async def test_failure_without_output_keeps_generic_text():
    be = RecordingShell(
        answers={"-printf": ExecResult(return_code=1, stdout="", stderr="")}
    )
    with pytest.raises(OSError) as ei:
        await be.listdir("/w")
    assert str(ei.value) == "find failed (exit 1): /w"


async def test_failure_tolerates_missing_output_streams():
    be = RecordingShell(
        answers={
            "-printf": ExecResult(return_code=1, stdout=None, stderr=None)
        }
    )
    with pytest.raises(OSError) as ei:
        await be.listdir("/w")
    assert str(ei.value) == "find failed (exit 1): /w"
