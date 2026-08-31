"""gc-testkit не должен ломать pytest в ЧУЖОМ проекте.

Пакет объявляет `[project.entry-points.pytest11]`, поэтому его плагин
подхватывается автоматически КАЖДЫМ pytest в окружении, куда попало колесо
gc-testkit — в том числе там, где рабочей копии монорепозитория нет вовсе.

Пока `gc_testkit.paths` считал `REPO` жадно на импорте, а `_find_repo` бросал
`RuntimeError`, загрузка плагина обрывала чужой прогон ДО сбора тестов. Одной
ленивости мало: даже с ленивым `REPO` плагин звал `package_dir`/`module_dir`
на импорте и вешал autouse-фикстуры, импортирующие `gc_core`, — чужой набор
краснел бы целиком. Поэтому здесь проверяется ИТОГ: тест-кит вне рабочей копии
полностью инертен.

Воспроизводим настоящим подпроцессом: копия `gc_testkit` кладётся в tmp_path,
рядом — тривиальный чужой тест, и pytest запускается оттуда. Внутри процесса
это не изобразить — модули тест-кита уже импортированы и корень уже найден.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from gc_testkit.paths import REPO

_SMOKE = """\
def test_someone_elses_project():
    assert 1 + 1 == 2
"""

#: Второй сценарий: у чужого проекта в окружении ТОЛЬКО gc-testkit (его
#: pyproject требует один pytest). Наши дистрибутивы и MCP SDK там не стоят.
#: Здесь мы их прячем: sitecustomize импортируется интерпретатором на старте,
#: до загрузки плагинов, и снимает их с импорта.
_HIDE = """\
import sys

_HIDDEN = ("gc_core", "gc_cli", "gc_tui", "gc_server", "mcp")


class _Blocker:
    def find_module(self, name, path=None):
        return None

    def find_spec(self, name, path=None, target=None):
        if name in _HIDDEN or name.split(".")[0] in _HIDDEN:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, _Blocker())
"""


def _offsite(tmp_path, *, hide_our_packages: bool):
    """Копия тест-кита ВНЕ рабочей копии + чужой тест рядом."""
    shutil.copytree(
        REPO / "packages" / "testkit" / "src" / "gc_testkit",
        tmp_path / "gc_testkit",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tmp_path / "test_smoke.py").write_text(_SMOKE, encoding="utf-8")
    if hide_our_packages:
        (tmp_path / "sitecustomize.py").write_text(_HIDE, encoding="utf-8")
    env = dict(os.environ)
    # PYTHONPATH идёт ВПЕРЕДИ site-packages, поэтому gc_testkit резолвится в
    # копию из tmp_path, а не в editable-исходники репозитория.
    env["PYTHONPATH"] = str(tmp_path)
    return env


@pytest.mark.parametrize(
    "hide_our_packages",
    [
        # Чужой проект, в чьём окружении наши колёса всё же стоят: падение было
        # бы на импорте плагина, ДО сбора тестов.
        False,
        # Чужой проект, поставивший один gc-testkit: сюда добавляется вторая
        # половина поломки — autouse-фикстуры, импортирующие gc_core.
        True,
    ],
    ids=["наши колёса в окружении есть", "кроме gc-testkit ничего нет"],
)
def test_plugin_is_inert_outside_working_copy(tmp_path, hide_our_packages):
    env = _offsite(tmp_path, hide_our_packages=hide_our_packages)
    res = subprocess.run(
        # Плагин НЕ подключается флагом: он приезжает сам, через pytest11 —
        # ровно так, как это происходит у чужого проекта.
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "test_smoke.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )

    combined = res.stdout + res.stderr
    assert "RuntimeError" not in combined, combined
    assert "ModuleNotFoundError" not in combined, combined
    assert res.returncode == 0, combined
    assert "1 passed" in combined, combined


def test_offsite_copy_really_sees_no_working_copy(tmp_path):
    """Страховка от вакуумности: если копия всё же находит корень, первый тест
    ничего не доказывает — он прогонится по обычному, «домашнему» пути."""
    shutil.copytree(
        REPO / "packages" / "testkit" / "src" / "gc_testkit",
        tmp_path / "gc_testkit",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    res = subprocess.run(
        [sys.executable, "-c",
         "import gc_testkit.paths as p; print(p.__file__); print(p.repo_root())"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    used_file, root = res.stdout.split()
    assert used_file.startswith(str(tmp_path)), used_file
    assert root == "None", root
