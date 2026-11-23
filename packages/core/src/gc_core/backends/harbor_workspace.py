"""Build bubblewrap commands in the task container's path namespace."""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

_LOADER_ENV_NAMES = ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT")
IMAGE_EXEC_ENV_NAMES = ("PATH", *_LOADER_ENV_NAMES)
TRUSTED_EXEC_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "BASH_ENV": "",
    "ENV": "",
    **dict.fromkeys(_LOADER_ENV_NAMES, ""),
}
_PROTECTED_ROOT_ENTRIES = (".git", ".gc", ".agents", ".codex", "AGENTS.md")
_BWRAP = "/usr/bin/bwrap"
_BASH = "/bin/bash"
_PROC_ROOT = "/proc"
_TMP_ROOT = "/tmp"
_SPECIAL_DIRS = (_PROC_ROOT, "/sys", "/dev")
_TRANSPORT_DIRS = ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/etc", _TMP_ROOT)

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
    if [ -e "$metadata_path" ] || [ -L "$metadata_path" ]; then
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
"""

_RELEASE_METADATA = r"""
if [ ! -e "$lease" ] && [ ! -L "$lease" ]; then exit 0; fi
if [ ! -d "$lease" ] || [ -L "$lease" ]; then exit 1; fi
failed=0
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


@dataclass(frozen=True)
class HarborWorkspace:
    root: str
    image_env: tuple[tuple[str, str], ...]
    protected: tuple[str, ...]
    metadata_lease: str

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
        for raw in self.protected:
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
        return tuple(
            posixpath.join(self.root, entry)
            for entry in _PROTECTED_ROOT_ENTRIES
        )

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
        for path in (self.root, *self.protected):
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
    ) -> str:
        argv = [
            _BWRAP,
            "--unshare-all",
            "--unshare-user",
            "--share-net",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            _PROC_ROOT,
            "--remount-ro",
            _PROC_ROOT,
            "--dev",
            "/dev",
        ]
        for path in self._writable_mounts():
            argv += ["--bind", str(path), str(path)]
        # Later mounts keep protected entries read-only inside the writable root.
        for path in (
            *self._metadata_paths(),
            *self.protected,
            self.metadata_lease,
        ):
            argv += ["--ro-bind", path, path]
        child_env = dict(self.image_env) if model_command else {}
        child_env.update(env or {})
        for name, value in child_env.items():
            argv += ["--setenv", name, str(value)]
        argv += [
            "--chdir",
            posixpath.join(self.root, cwd) if cwd else self.root,
            "--",
            _BASH,
            "-c",
            command,
        ]
        return "exec " + shlex.join(argv)
