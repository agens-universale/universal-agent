"""Развернуть пути для lint.sh: pre-commit сам не обходит каталоги."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def expand_paths(paths: list[str]) -> list[str]:
    files: set[str] = set()
    for raw in paths:
        path = Path(raw)
        relative = Path(os.path.relpath(path))
        if (relative.parts and relative.parts[0] == "..") or not path.exists():
            raise ValueError(f"Path must exist inside the repository: {raw}")
        if path.is_file():
            files.add(relative.as_posix())
            continue
        result = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                str(relative),
            ],
            check=True,
            capture_output=True,
        )
        files.update(
            os.fsdecode(p)
            for p in result.stdout.split(b"\0")
            if p and Path(os.fsdecode(p)).is_file()
        )
    if not files:
        raise ValueError("No files to check in the supplied paths")
    return sorted(files)


def main(argv: list[str]) -> int:
    separator = argv.index("--")
    command, paths = argv[:separator], argv[separator + 1 :]
    try:
        files = expand_paths(paths)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"lint: {exc}", file=sys.stderr)
        return 1
    return subprocess.call(
        [*command, "--hook-stage", "manual", "--files", *files]
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
