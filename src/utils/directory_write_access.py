"""Cross-platform check for writable directories."""

import os
import tempfile
from pathlib import Path


def directory_allows_probe_write(dir_path: str) -> bool:
    """
    Return True when a new ephemeral file can be created and deleted under ``dir_path``.

    Uses ``tempfile.NamedTemporaryFile`` inside the directory. This aligns with practical
    "can ordinary code write here?" behavior more reliably than ``os.access(W_OK)``, which
    can be unreliable for directories on Windows and some mount types on POSIX.

    Raises nothing: failures map to ``False``.
    """
    try:
        resolved = Path(os.path.abspath(os.path.expanduser(os.path.normpath(str(dir_path)))))
        if not resolved.is_dir():
            return False
    except OSError:
        return False
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".mini_cli_write_probe_",
            suffix=".tmp",
            dir=os.fspath(resolved),
            delete=True,
        ) as fh:
            fh.write(b"")
        return True
    except OSError:
        return False
