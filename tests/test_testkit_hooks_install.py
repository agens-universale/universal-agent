"""Прогон нашего набора сам ставит хуки pre-commit, если их в клоне нет.

``uv sync`` хук git поставить не может, и ``pre-commit install`` забывали:
проверки тогда впервые гонялись в CI. Поэтому тест-кит в начале прогона
ставит хуки сам — но только для НАШЕГО набора (гейт rootdir), и не трогает
клон в CI, по явному отказу, без git, без ``pre_commit`` и когда хуки уже есть.

Проверяем настоящим подпроцессом pytest на временном git-репозитории — как
``test_testkit_rootdir_gate``: настоящий ``.git/hooks`` клона тест не трогает.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
from gc_testkit.paths import REPO

_INI = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'

_CONFIG = "default_install_hook_types: [pre-commit, pre-push]\nrepos: []\n"

#: Зонд печатает, что прогон оставил вложенным pytest в окружении.
_PROBE = """\
import os


def test_probe():
    print("SKIP=" + os.environ.get("GC_SKIP_HOOKS_INSTALL", ""))
"""

#: Прячет ``pre_commit`` от прогона: импортируется до загрузки плагинов.
_HIDE_PRE_COMMIT = """\
import sys


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "pre_commit" or name.startswith("pre_commit."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, _Blocker())
"""

_INSTALLED = "хуки pre-commit поставлены"
_HOOK_TYPES = ("pre-commit", "pre-push")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _working_copy(tmp_path, *, git=True):
    """Наша рабочая копия в миниатюре: маркер gc-core, конфиг, тест-кит."""
    root = tmp_path / "repo"
    _write(
        root / "packages" / "core" / "pyproject.toml",
        '[project]\nname = "gc-core"\n\n' + _INI,
    )
    _write(root / "pyproject.toml", _INI)
    _write(root / ".pre-commit-config.yaml", _CONFIG)
    _write(root / "tests" / "test_probe.py", _PROBE)
    shutil.copytree(
        REPO / "packages" / "testkit" / "src" / "gc_testkit",
        root / "packages" / "testkit" / "src" / "gc_testkit",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if git:
        _git(root, "init", "-q", "--template=")
    return root


def _run(root, *, env=None, extra_path=()):
    base = dict(os.environ)
    # Внешний прогон сам ставит отказ для вложенных — снимаем его, и CI тоже:
    # здесь установка задумана, это предмет теста.
    base.pop("GC_SKIP_HOOKS_INSTALL", None)
    base.pop("CI", None)
    base.update(env or {})
    paths = [str(root / "packages" / "testkit" / "src"), *extra_path]
    base["PYTHONPATH"] = os.pathsep.join(paths)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider"],
        cwd=root,
        env=base,
        capture_output=True,
        text=True,
    )


def _hooks_dir(root):
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root / out


def _installed(root):
    return [t for t in _HOOK_TYPES if (_hooks_dir(root) / t).is_file()]


def _output(res):
    return res.stdout + res.stderr


def test_missing_hooks_are_installed_once(tmp_path):
    root = _working_copy(tmp_path)
    assert _installed(root) == []

    res = _run(root)

    assert res.returncode == 0, _output(res)
    # Оба типа из default_install_hook_types конфига, и хук именно pre-commit.
    assert _installed(root) == list(_HOOK_TYPES)
    hook = (_hooks_dir(root) / "pre-commit").read_text(encoding="utf-8")
    assert "pre-commit" in hook and sys.executable in hook
    assert [
        line for line in _output(res).splitlines() if _INSTALLED in line
    ] != []
    # Вложенный pytest (тесты, гоняющие pytest во временных репозиториях)
    # хуки уже не ставит: отказ уезжает в окружение.
    assert "SKIP=1" in res.stdout

    again = _run(root)
    assert again.returncode == 0, _output(again)
    assert _INSTALLED not in _output(again)


def test_existing_hooks_are_left_alone(tmp_path):
    root = _working_copy(tmp_path)
    hooks = _hooks_dir(root)
    hooks.mkdir(parents=True, exist_ok=True)
    for name in _HOOK_TYPES:
        (hooks / name).write_text("#!/bin/sh\n# свой\n", encoding="utf-8")

    res = _run(root)

    assert res.returncode == 0, _output(res)
    assert _INSTALLED not in _output(res)
    for name in _HOOK_TYPES:
        assert (hooks / name).read_text(encoding="utf-8") == (
            "#!/bin/sh\n# свой\n"
        )


@pytest.mark.parametrize(
    "env",
    [{"CI": "1"}, {"CI": "true"}, {"GC_SKIP_HOOKS_INSTALL": "1"}],
    ids=["ci-1", "ci-true", "opt-out"],
)
def test_ci_and_opt_out_leave_clone_untouched(tmp_path, env):
    root = _working_copy(tmp_path)

    res = _run(root, env=env)

    assert res.returncode == 0, _output(res)
    assert _installed(root) == []
    assert _INSTALLED not in _output(res)


def test_not_a_git_clone_is_silent(tmp_path):
    root = _working_copy(tmp_path, git=False)

    res = _run(root)

    assert res.returncode == 0, _output(res)
    assert "pre-commit" not in res.stderr
    assert not (root / ".git").exists()


def test_without_pre_commit_module_nothing_happens(tmp_path):
    root = _working_copy(tmp_path)
    hide = tmp_path / "hide"
    _write(hide / "sitecustomize.py", _HIDE_PRE_COMMIT)

    res = _run(root, extra_path=[str(hide)])

    assert res.returncode == 0, _output(res)
    assert _installed(root) == []
    assert "pre-commit" not in res.stderr


def test_install_failure_is_one_warning_not_a_failure(tmp_path):
    root = _working_copy(tmp_path)
    # Каталог хуков занят файлом: pre-commit поставить хук не сможет.
    _write(_hooks_dir(root), "не каталог\n")

    res = _run(root)

    assert res.returncode == 0, _output(res)
    warnings = [
        line for line in res.stderr.splitlines() if "pre-commit" in line
    ]
    assert len(warnings) == 1, res.stderr
    assert warnings[0].startswith("[warn] gc_testkit:")
    # Предупреждение повторится на каждом прогоне — пусть сразу говорит,
    # как его выключить.
    assert "GC_SKIP_HOOKS_INSTALL=1" in warnings[0]


@pytest.mark.parametrize("scope", ["repo", "global"])
def test_core_hooks_path_is_skipped_silently(tmp_path, scope):
    """При core.hooksPath pre-commit ставиться отказывается — и не пробуем.

    Иначе каждый прогон печатал бы одно и то же предупреждение вечно.
    """
    root = _working_copy(tmp_path)
    env = {}
    if scope == "repo":
        _git(root, "config", "core.hooksPath", "custom-hooks")
    else:
        gitconfig = tmp_path / "gitconfig"
        _write(gitconfig, "[core]\n\thooksPath = custom-hooks\n")
        env["GIT_CONFIG_GLOBAL"] = str(gitconfig)

    res = _run(root, env=env)

    assert res.returncode == 0, _output(res)
    assert not (root / "custom-hooks").exists()
    assert not (root / ".git" / "hooks").exists()
    assert "pre-commit" not in res.stderr


def test_linked_worktree_does_not_install(tmp_path):
    """Из связанного worktree не ставим: каталог хуков общий для всех.

    Хук запомнил бы интерпретатор ``.venv`` этого worktree — удалили
    worktree, и хуки сломаны у всех клонов, а «уже стоят» их не починит.
    """
    root = _working_copy(tmp_path)
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "init",
    )
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", str(linked))

    res = _run(linked)

    assert res.returncode == 0, _output(res)
    assert _installed(linked) == []
    assert _installed(root) == []
    assert "pre-commit" not in res.stderr


def test_foreign_suite_is_never_touched(tmp_path):
    """Гейт rootdir: чужой проект в git-репозитории хуков не получает."""
    root = tmp_path / "foreign"
    _write(root / "pyproject.toml", _INI)
    _write(root / ".pre-commit-config.yaml", _CONFIG)
    _write(root / "tests" / "test_probe.py", _PROBE)
    _git(root, "init", "-q", "--template=")

    res = _run(
        root,
        extra_path=[str(REPO / "packages" / "testkit" / "src")],
    )

    assert res.returncode == 0, _output(res)
    assert _installed(root) == []


def test_copy_nested_in_another_repo_is_not_a_clone(tmp_path):
    """Корень набора — не корень клона: хуки внешнему репозиторию не ставим."""
    outer = tmp_path
    _git(outer, "init", "-q", "--template=")
    root = _working_copy(tmp_path, git=False)

    res = _run(root)

    assert res.returncode == 0, _output(res)
    assert _installed(outer) == []
    assert "pre-commit" not in res.stderr
