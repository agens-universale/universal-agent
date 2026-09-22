"""Прямой запуск `python .../install.py` и `.../run.py` находит gc_core.

Это документированный фолбэк, когда консольная команда `gc-mcp` недоступна.
Он был сломан: бутстрап брал корень как `Path(__file__).parents[1]`, что до
переезда рецептов означало корень репозитория, а после переезда — каталог
`packages/mcp/src`. Оттуда `glob("packages/*/src")` не находит ничего, gc_core
оставался ненаходимым, и запуск падал на `ModuleNotFoundError: gc_core` — при
том что комментарий рядом утверждал, что чинит ровно этот сценарий. Теперь
корень ищет `repo_path` по маркеру, а жёсткой арифметики осталась одна строка:
каталог, из которого виден сам пакет рецептов.

Проверять это в текущем процессе бессмысленно: в рабочем окружении все пакеты
уже стоят editable. Поэтому запускаем БАЗОВЫЙ интерпретатор (не .venv), которому
наши дистрибутивы не видны.

`yaml` подкладывается заглушкой, и это не жульничество: PyYAML — рантайм-
зависимость дистрибутива, бутстрап `sys.path` её не заменяет и заменять не
должен. Чинится именно поиск НАШИХ исходников, а сторонние зависимости
базовому интерпретатору обязан дать тот, кто его выбрал.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from gc_testkit.paths import REPO

_RECIPES = REPO / "packages" / "mcp" / "src" / "recommended_mcp"

_YAML_STUB = """\
def safe_load(*a, **k):
    return {}


def safe_dump(*a, **k):
    return ""


class YAMLError(Exception):
    pass
"""


def _bare_python() -> Path:
    """Поддерживаемый интерпретатор без нашего .venv: базовый префикс текущего."""
    too_old: list[str] = []
    for name in ("python3", "python", "python3.exe", "python.exe"):
        cand = Path(sys.base_prefix) / ("Scripts" if os.name == "nt" else "bin") / name
        if cand.is_file():
            version = subprocess.run(
                [str(cand), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                capture_output=True,
                text=True,
            )
            if version.returncode == 0:
                major, minor = (int(part) for part in version.stdout.strip().split(".", 1))
                if (major, minor) >= (3, 11):
                    return cand
                too_old.append(f"{cand} ({major}.{minor})")
    if too_old:
        pytest.skip("базовый интерпретатор не удовлетворяет requires-python >=3.11: " + ", ".join(too_old))
    pytest.skip(f"базовый интерпретатор не найден в {sys.base_prefix}")


def _env_without_repo(extra_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(extra_path)  # только заглушка yaml, репозитория тут нет
    env["PYTHONNOUSERSITE"] = "1"
    return env


@pytest.mark.parametrize("script", ["install.py", "run.py"])
def test_direct_run_finds_our_packages(script, tmp_path):
    python = _bare_python()
    (tmp_path / "yaml.py").write_text(_YAML_STUB, encoding="utf-8")
    env = _env_without_repo(tmp_path)

    probe = subprocess.run(
        [str(python), "-c", "import gc_core"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        pytest.skip("базовому интерпретатору gc_core виден и без бутстрапа — проверять нечего")

    res = subprocess.run(
        [str(python), str(_RECIPES / script), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert "No module named" not in res.stderr, f"{script}: бутстрап не нашёл наши исходники\n{res.stderr}"
    assert res.returncode == 0, res.stderr
    assert "usage" in res.stdout.lower(), res.stdout
