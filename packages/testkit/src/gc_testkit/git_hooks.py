"""Хуки pre-commit ставятся сами — в начале прогона НАШЕГО набора.

``uv sync`` хук git поставить не может (git не даёт репозиторию ставить
исполняемые хуки), а про ``pre-commit install`` забывают: проверки впервые
гоняются в CI, и красное приходит уже после пуша. Тесты гоняет каждый, поэтому
ставим хуки отсюда: вызывает ``gc_testkit.plugin`` после гейта rootdir.

Ничего не делаем, если задан ``CI``, задан ``GC_SKIP_HOOKS_INSTALL=1``, это
не git-клон, нет конфига, не импортируется ``pre_commit`` или хуки уже стоят.
Молча пропускаем и клон с ``core.hooksPath`` (pre-commit туда ставиться
отказывается — предупреждение повторялось бы на каждом прогоне), и связанный
worktree: каталог хуков общий, а хук запомнил бы интерпретатор ``.venv``
этого worktree — удалят worktree, и хуки сломаются у всех клонов.
Ставит сам ``pre_commit`` текущим интерпретатором — типы хуков он берёт из
``default_install_hook_types`` конфига. Отказ установки прогон не роняет.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

#: Отказ от установки. Прогон ставит его в своё окружение: тесты, которые
#: гоняют pytest во временных репозиториях, хуков туда не ставят.
SKIP_ENV = "GC_SKIP_HOOKS_INSTALL"

_CONFIG = ".pre-commit-config.yaml"

#: Установка — доли секунды; дольше — что-то не так, прогон не ждёт.
_TIMEOUT_S = 30


def _git_lines(root: Path, *args: str) -> list[str] | None:
    """Строки вывода ``git <args>`` в ``root``; None при любой неудаче."""
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.splitlines() if res.returncode == 0 else None


def _hooks_dir(root: Path) -> Path | None:
    """Каталог хуков основного клона, иначе None.

    None — когда ``root`` не корень клона (копия нашего дерева внутри
    чужого репозитория не повод ставить хуки этому репозиторию), когда
    это связанный worktree (``--git-dir`` не равен ``--git-common-dir``)
    и когда задан ``core.hooksPath`` — где угодно, хоть глобально.
    """
    lines = _git_lines(
        root,
        "rev-parse",
        "--show-toplevel",
        "--git-path",
        "hooks",
        "--git-dir",
        "--git-common-dir",
    )
    if lines is None or len(lines) != 4:
        return None
    top, hooks, git_dir, common_dir = lines
    if Path(top).resolve() != root.resolve():
        return None
    if (root / git_dir).resolve() != (root / common_dir).resolve():
        return None
    if _git_lines(root, "config", "--get", "core.hooksPath"):
        return None
    return root / hooks


def _hook_types(config: Path) -> list[str]:
    """Типы хуков из ``default_install_hook_types``, как их ставит pre-commit."""
    import yaml  # зависимость pre_commit: есть, раз есть он

    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    types = data.get("default_install_hook_types")
    return list(types) if isinstance(types, list) and types else ["pre-commit"]


def _wanted(root: Path) -> bool:
    """Нужна ли установка: все отказы из шапки модуля."""
    if os.environ.get("CI") or os.environ.get(SKIP_ENV) == "1":
        return False
    config = root / _CONFIG
    if not config.is_file() or importlib.util.find_spec("pre_commit") is None:
        return False
    hooks = _hooks_dir(root)
    if hooks is None:
        return False
    return not all((hooks / t).is_file() for t in _hook_types(config))


def ensure_hooks_installed(root: Path) -> None:
    """Поставить хуки pre-commit в клон ``root``, если их там нет.

    Пишет одну строку в stderr: об установке или о её отказе.
    """
    try:
        wanted = _wanted(root)
    except ImportError:
        wanted = False
    # Вложенные прогоны pytest (временные репозитории тестов) — не наше дело.
    os.environ[SKIP_ENV] = "1"
    if not wanted:
        return
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pre_commit", "install"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        reason = str(exc)
    else:
        if res.returncode == 0:
            print(
                f"gc_testkit: хуки pre-commit поставлены в {root} "
                f"(отказ: {SKIP_ENV}=1).",
                file=sys.stderr,
            )
            return
        reason = (res.stderr or res.stdout).strip().replace("\n", " ")
    print(
        f"[warn] gc_testkit: хуки pre-commit не поставлены: {reason} "
        f"(отказ: {SKIP_ENV}=1).",
        file=sys.stderr,
    )
