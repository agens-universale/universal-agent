"""Build bubblewrap commands in the task container's path namespace."""

from __future__ import annotations

import posixpath
import re
import shlex
import uuid
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

_LOADER_ENV_NAMES = ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT")
IMAGE_EXEC_ENV_NAMES = ("PATH", *_LOADER_ENV_NAMES)
TRUSTED_EXEC_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "BASH_ENV": "",
    "ENV": "",
    **dict.fromkeys(_LOADER_ENV_NAMES, ""),
}
_PROTECTED_ROOT_ENTRIES = (".gc", ".agents", "AGENTS.md")
_GIT = ".git"
# Внутри настоящего репозитория пишутся index и objects (add/commit/checkout),
# но не hooks: переписанный hook исполнился бы при следующей git-операции
# вне песочницы — например, в verifier. Так же устроен профиль GigaCode.
_GIT_HOOKS = ".git/hooks"
_BWRAP = "/usr/bin/bwrap"
# `--disable-userns` появился в bubblewrap 0.8.0 (Ubuntu 22.04 — 0.6.1).
_DISABLE_USERNS_SINCE = (0, 8)
_BWRAP_VERSION = re.compile(r"bubblewrap (\d+)\.(\d+)")
_BASH = "/bin/bash"
_PROC_ROOT = "/proc"
_TMP_ROOT = "/tmp"
_SPECIAL_DIRS = (_PROC_ROOT, "/sys", "/dev")
_TRANSPORT_DIRS = ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/etc", _TMP_ROOT)
# Let the in-container deadline finish and bwrap reap its namespace before
# Harbor can time out the transport. Transport failures still fail closed.
_COMMAND_EXIT_SLACK_S = 5

_REPORT_SANDBOX_EXIT = r"""
receipt=$1
shift
code=0
"$@" || code=$?
printf '\n%s %s\n' "$receipt" "$code"
"""

_SUPERVISE_COMMAND = r"""
deadline=$1
running_file=$2
receipt=$3
shift 3

# This shell is PID 1. Its exit shuts down the entire sandbox PID namespace,
# and the outer bwrap waits for it before Harbor reports command completion.
report() {
    trap '' USR1 USR2
    printf '\n%s %s\n' "$receipt" "$1"
    exit 0
}
trap 'report timeout' USR1
trap 'if [ ! -e "$running_file" ]; then report stopped; fi' USR2
if [ ! -e "$running_file" ]; then report stopped; fi

if [ "$deadline" -gt 0 ]; then
    (/usr/bin/sleep "$deadline" && kill -USR1 "$$") >/dev/null 2>&1 &
fi
(while [ -e "$running_file" ]; do /usr/bin/sleep 0.1; done
 kill -USR2 "$$") >/dev/null 2>&1 &

# An asynchronous shell list would otherwise give the model /dev/null as stdin.
exec 3<&0
"$@" <&3 3<&- &
child=$!
exec 3<&-
wait "$child"
report "$?"
"""

_RESERVE_METADATA = r"""
set -e
validate_mount_path() {
    canonical=$(realpath -e -- "$1") || return 1
    if [ "$canonical" != "$1" ] || { [ ! -f "$1" ] && [ ! -d "$1" ]; }; then
        printf 'Expected a canonical file or directory: %s\n' "$1" >&2
        return 1
    fi
}
for metadata_path in "${extra_paths[@]}"; do
    validate_mount_path "$metadata_path"
done
for metadata_path; do
    if [ -L "$metadata_path" ]; then
        canonical=$(realpath -e -- "$metadata_path")
        validate_mount_path "$canonical"
        printf '%s\0%s\0' "$metadata_path" "$canonical"
    elif [ -e "$metadata_path" ]; then
        validate_mount_path "$metadata_path"
    fi
done

mkdir -m 700 -- "$lease"
created=()
rollback() {
    if [ "$?" -eq 0 ]; then return; fi
    set +e
    for metadata_path in "${created[@]}"; do
        rmdir -- "$metadata_path" && rm -f -- "$lease/${metadata_path##*/}"
    done
    rm -f -- "$lease/running"
    rmdir -- "$lease"
}
trap rollback EXIT
trap 'exit 1' HUP INT TERM
for metadata_path; do
    if [ -e "$metadata_path" ]; then continue; fi
    mkdir -m 700 -- "$metadata_path"
    created+=("$metadata_path")
    stat -c '%d:%i' -- "$metadata_path" > "$lease/${metadata_path##*/}"
done
: > "$lease/running"
"""

_RELEASE_METADATA = r"""
if [ ! -e "$lease" ] && [ ! -L "$lease" ]; then exit 0; fi
if [ ! -d "$lease" ] || [ -L "$lease" ]; then exit 1; fi
failed=0
rm -f -- "$lease/running" || failed=1
for metadata_path; do
    record="$lease/${metadata_path##*/}"
    if [ ! -f "$record" ] || [ -L "$record" ]; then continue; fi
    if [ -e "$metadata_path" ] || [ -L "$metadata_path" ]; then
        if [ "$(stat -c '%d:%i' -- "$metadata_path")" != "$(<"$record")" ] ||
            ! rmdir -- "$metadata_path"; then
            printf 'Cannot release Harbor metadata reservation: %s\n' "$metadata_path" >&2
            failed=1
            continue
        fi
    fi
    rm -- "$record" || failed=1
done
rmdir -- "$lease" || failed=1
exit "$failed"
"""


def environment_probe_command(root: str) -> str:
    """Что нужно знать о контейнере до первого вызова bwrap.

    Строка ``git=dir`` — в корне настоящий репозиторий (каталог ``.git``
    с ``HEAD``, не символьная ссылка); затем вывод ``bwrap --version``.
    """
    git = shlex.quote(posixpath.join(root, _GIT))
    return (
        f"if [ -d {git} ] && [ ! -L {git} ] && [ -f {git}/HEAD ]; "
        "then echo git=dir; else echo git=none; fi\n"
        f"{_BWRAP} --version 2>&1 || true\n"
    )


def parse_environment_probe(stdout: str) -> tuple[bool, bool]:
    """``(git_repository, disable_userns)`` из вывода пробы.

    Версию не распознали — флаг остаётся: отказ старого bwrap виден на
    preflight, а молча ослаблять запуск по догадке нельзя.
    """
    lines = (stdout or "").splitlines()
    git_repository = "git=dir" in lines
    match = _BWRAP_VERSION.search(stdout or "")
    disable_userns = (
        match is None
        or (int(match[1]), int(match[2])) >= _DISABLE_USERNS_SINCE
    )
    return git_repository, disable_userns


def is_proc_mount_denied(detail: str) -> bool:
    """Отказ bwrap смонтировать procfs (Docker без systempaths=unconfined)."""
    return "Can't mount proc" in detail and (
        "Operation not permitted" in detail or "Permission denied" in detail
    )


@dataclass(frozen=True)
class WorkspaceCommand:
    timeout: int | None
    running_file: str
    receipt: str = field(
        default_factory=lambda: f"__gc_workspace_result_{uuid.uuid4().hex}"
    )
    sandbox_receipt: str = field(
        default_factory=lambda: f"__gc_workspace_exit_{uuid.uuid4().hex}"
    )

    @property
    def transport_timeout(self) -> int | None:
        if self.timeout and self.timeout > 0:
            return self.timeout + _COMMAND_EXIT_SLACK_S
        return self.timeout

    def completion(self, stdout: str) -> tuple[str, str]:
        stdout, sandbox_status = self._take_receipt(
            stdout, self.sandbox_receipt
        )
        if sandbox_status != "0":
            return stdout, sandbox_status
        return self._take_receipt(stdout, self.receipt)

    @staticmethod
    def _take_receipt(stdout: str, receipt: str) -> tuple[str, str]:
        before, marker, after = stdout.rpartition(f"\n{receipt} ")
        status, newline, rest = after.partition("\n")
        if (
            not marker
            or not newline
            or not (
                status in ("stopped", "timeout")
                or (
                    status.isascii()
                    and status.isdecimal()
                    and len(status) <= 3
                )
            )
        ):
            raise RuntimeError(
                "Harbor workspace command has no completion receipt"
            )
        return before + rest, status


@dataclass(frozen=True)
class HarborWorkspace:
    root: str
    image_env: tuple[tuple[str, str], ...]
    protected: tuple[str, ...]
    metadata_lease: str
    metadata_targets: tuple[tuple[str, str], ...] = ()
    git_repository: bool = False
    disable_userns: bool = True
    # False — procfs смонтировать нельзя: пустой tmpfs на /proc, как у
    # GigaCode. Не работают `ps` и `/proc/self`, зато трайл не падает.
    private_proc: bool = True

    def __post_init__(self) -> None:
        root = PurePosixPath(self.root)
        if (
            not root.is_absolute()
            or any(
                PurePosixPath(path).is_relative_to(root)
                for path in (*_TRANSPORT_DIRS, *_SPECIAL_DIRS)
            )
            or any(root.is_relative_to(path) for path in _SPECIAL_DIRS)
        ):
            raise ValueError(f"Invalid Harbor workspace root: {self.root!r}")
        for raw in (
            *self.protected,
            *(target for _, target in self.metadata_targets),
        ):
            if not raw.startswith("/") or posixpath.normpath(raw) != raw:
                raise ValueError(
                    "Harbor workspace-write requires absolute, normalized "
                    f"security.protected_paths.extra entries: {raw!r}"
                )
            if root.is_relative_to(raw) or PurePosixPath(
                _TMP_ROOT
            ).is_relative_to(raw):
                raise ValueError(
                    f"Protected path covers a Harbor writable root: {raw!r}"
                )
            if any(
                PurePosixPath(raw).is_relative_to(path)
                for path in _SPECIAL_DIRS
            ):
                raise ValueError(
                    f"Protected path overlaps a sandbox mount: {raw!r}"
                )

    def _metadata_paths(self) -> tuple[str, ...]:
        git = _GIT_HOOKS if self.git_repository else _GIT
        return tuple(
            posixpath.join(self.root, entry)
            for entry in (git, *_PROTECTED_ROOT_ENTRIES)
        )

    def with_metadata_targets(self, records: str) -> HarborWorkspace:
        if not records:
            return self
        fields = records.removesuffix("\0").split("\0")
        names = fields[::2]
        if len(fields) % 2 or not set(names).issubset(self._metadata_paths()):
            raise RuntimeError("Invalid Harbor workspace metadata targets")
        return replace(
            self, metadata_targets=tuple(zip(names, fields[1::2], strict=True))
        )

    def _protected_mounts(self) -> tuple[str, ...]:
        targets = dict(self.metadata_targets)
        metadata = (targets.get(path, path) for path in self._metadata_paths())
        return tuple(dict.fromkeys((*metadata, *self.protected)))

    @property
    def running_file(self) -> str:
        return f"{self.metadata_lease}/running"

    def stop_command(self) -> str:
        # Removing the marker is safe even after the worker releases the lease.
        return f"rm -f -- {shlex.quote(self.running_file)}"

    def reserve_metadata_command(self) -> str:
        # An empty directory reserves a name without adding an untracked file
        # or a bogus Git worktree pointer. It lasts only for the agent phase.
        extra = shlex.join(self.protected)
        return (
            self._metadata_args()
            + f"extra_paths=({extra})\n{_RESERVE_METADATA}"
        )

    def release_metadata_command(self) -> str:
        return self._metadata_args() + _RELEASE_METADATA

    def _metadata_args(self) -> str:
        return (
            f"lease={shlex.quote(self.metadata_lease)}\n"
            f"set -- {shlex.join(self._metadata_paths())}\n"
        )

    def _writable_mounts(self) -> list[PurePosixPath]:
        temp = PurePosixPath(_TMP_ROOT)
        roots = (temp, PurePosixPath(self.root))
        mounts = set(roots)
        # Pin ancestors as writable mounts so their children remain editable
        # while the directories holding protection cannot be relocated.
        for path in (self.root, *self._protected_mounts()):
            mounts.update(
                parent
                for parent in PurePosixPath(path).parents
                if any(parent.is_relative_to(root) for root in roots)
            )
        return sorted(
            mounts, key=lambda path: (len(path.parts), path != temp, path)
        )

    def wrap(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict | None = None,
        model_command: bool = False,
        supervised: WorkspaceCommand | None = None,
    ) -> str:
        argv = [
            _BWRAP,
            "--unshare-all",
            "--unshare-user",
            "--share-net",
            *(["--disable-userns"] if self.disable_userns else []),
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/",
            "/",
            "--proc" if self.private_proc else "--tmpfs",
            _PROC_ROOT,
            "--remount-ro",
            _PROC_ROOT,
            "--dev",
            "/dev",
        ]
        for path in self._writable_mounts():
            argv += ["--bind", str(path), str(path)]
        # Later mounts keep protected entries read-only inside the writable root.
        for path in (*self._protected_mounts(), self.metadata_lease):
            argv += ["--ro-bind", path, path]
        child_env = dict(self.image_env) if model_command else {}
        child_env.update(env or {})
        payload = [_BASH, "-c", command]
        if supervised is None:
            for name, value in child_env.items():
                argv += ["--setenv", name, str(value)]
        else:
            # Keep model-provided startup/loader variables away from PID 1.
            argv.append("--as-pid-1")
            payload = [
                _BASH,
                "-p",
                "-c",
                _SUPERVISE_COMMAND,
                "bash",
                str(supervised.timeout or 0),
                supervised.running_file,
                supervised.receipt,
                "/usr/bin/env",
                "--default-signal=INT,QUIT",
                "--",
                *(f"{name}={value}" for name, value in child_env.items()),
                *payload,
            ]
        argv += [
            "--chdir",
            posixpath.join(self.root, cwd) if cwd else self.root,
            "--",
            *payload,
        ]
        if supervised is not None:
            # A confirmed bwrap failure (for example a missing cwd) is a
            # command error. A Docker client failure can have the same code.
            argv = [
                _BASH,
                "-p",
                "-c",
                _REPORT_SANDBOX_EXIT,
                "bash",
                supervised.sandbox_receipt,
                *argv,
            ]
        return "exec " + shlex.join(argv)
