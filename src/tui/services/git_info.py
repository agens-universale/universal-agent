"""Git-инфо для списка сессий: текущая ветка cwd. Без бизнес-логики."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Union


def current_branch(cwd: Union[str, Path]) -> Optional[str]:
    """Имя текущей git-ветки в cwd. Не-git / detached HEAD / нет git → None."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd), capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if not branch or branch == "HEAD":  # detached
        return None
    return branch
