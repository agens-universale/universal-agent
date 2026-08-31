"""Бутстрап тест-кита включается только для НАШИХ наборов.

Плагин приезжает через entry point ``pytest11``, а тот грузится НЕЗАВИСИМО от
rootdir. Пока гейт спрашивал ``repo_root() is not None`` — то есть лежит ли в
рабочей копии сам плагин, — он ничего не знал о том, чей набор собирают. До
разбиения на пакеты вопрос не стоял: бутстрап жил в корневом ``conftest.py``, а
conftest подхватывается только для тестов НИЖЕ своего каталога, и у вендоренных
серверов (``external/vendor/*-mcp``, свои ``pyproject.toml``, свой rootdir)
корневого conftest не было видно — отсечка была бесплатной, от устройства
pytest. Своя секция ``[tool.pytest.ini_options]`` у вендора при этом
обязательна: без неё pytest поднимает rootdir до корня монорепозитория, и
отличить чужой прогон от нашего уже нечем. Это стережёт
``test_every_vendored_server_declares_its_own_rootdir`` ниже — у ``ddgs-mcp``
секции не было, и на нём утечка пережила починку самого гейта. С ``pytest11`` она пропала, и прогон вендоренного сервера получал на
``sys.path`` все наши ``packages/*/src``, а без ``mcp`` в окружении — ещё и
заглушки ``trafilatura``/``readability``, у которых извлекать перестаёт что бы
то ни было.

Живёт в корневом наборе, а не при пакете, по той же причине, что
``test_testkit_sys_path``: у ``gc-testkit`` собственного набора нет вовсе, а его
плагин участвует в прогоне ВСЕХ пакетов.

Проверяем настоящим подпроцессом на синтетической рабочей копии: внутри
процесса это не изобразить — модули тест-кита уже импортированы, а rootdir
у прогона ровно один. Копия нужна и по второй причине: в общем ``.venv`` все
``packages/*/src`` лежат на ``sys.path`` сами, от editable-установок, поэтому
по НАСТОЯЩЕМУ дереву работу бутстрапа от работы ``.pth`` не отличить. В копии
маркером служит модуль, которого нет нигде, кроме неё.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from gc_testkit.paths import REPO

#: Тест-зонд: печатает то, что снаружи не видно, — какой rootdir выбрал pytest
#: и виден ли модуль из ``packages/*/src`` синтетической копии.
_PROBE = """\
import importlib.util
import json


def test_probe(request):
    print("GATE " + json.dumps({
        "rootdir": str(request.config.rootpath),
        "sentinel_importable": importlib.util.find_spec("sentinel_gc_pkg") is not None,
    }))
"""

#: Прячем ``mcp`` от прогона: без него плагин собирает минимальную заглушку
#: пакета ``tools``. sitecustomize импортируется интерпретатором на старте, до
#: загрузки плагинов.
_HIDE_MCP = """\
import sys


class _Blocker:
    def find_module(self, name, path=None):
        return None

    def find_spec(self, name, path=None, target=None):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, _Blocker())
"""

_INI = "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _working_copy(tmp_path):
    """Синтетическая рабочая копия: наш корень, наш пакет, чужой проект внутри.

    Дерево НЕПОЛНОЕ — из ``packages/core/src`` есть только модуль-маркер. Это не
    экономия: ровно так выглядит sparse-чекаут или bisect на коммит до
    появления ``gc_core/tools``, и на этом же дереве проверяется отказ заглушки.
    """
    root = tmp_path / "repo"
    # Маркер рабочей копии — тот самый, по которому её ищет gc_testkit.paths.
    _write(root / "packages" / "core" / "pyproject.toml",
           '[project]\nname = "gc-core"\n\n' + _INI)
    _write(root / "pyproject.toml", _INI)
    # Модуль, которого нет ни в репозитории, ни в site-packages: виден, только
    # если бутстрап положил на sys.path packages/*/src ЭТОЙ копии.
    _write(root / "packages" / "core" / "src" / "sentinel_gc_pkg.py", "")
    shutil.copytree(
        REPO / "packages" / "testkit" / "src" / "gc_testkit",
        root / "packages" / "testkit" / "src" / "gc_testkit",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # Чужой проект ВНУТРИ нашего дерева — как вендоренные MCP-серверы.
    _write(root / "external" / "vendor" / "thing-mcp" / "pyproject.toml", _INI)
    for suite in (root, root / "packages" / "core",
                  root / "external" / "vendor" / "thing-mcp"):
        _write(suite / "tests" / "test_probe.py", _PROBE)
    return root


def _run(root, rel, *, hide_mcp=False, tmp_path=None):
    env = dict(os.environ)
    # PYTHONPATH идёт ВПЕРЕДИ site-packages, поэтому gc_testkit резолвится в
    # копию, а не в editable-исходники репозитория.
    entries = [str(root / "packages" / "testkit" / "src")]
    if hide_mcp:
        hide = tmp_path / "hide"
        _write(hide / "sitecustomize.py", _HIDE_MCP)
        entries.append(str(hide))
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return subprocess.run(
        # Плагин НЕ подключается флагом: он приезжает сам, через pytest11.
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider"],
        cwd=root / rel if rel else root,
        env=env, capture_output=True, text=True,
    )


def _gate(res):
    line = next(ln for ln in res.stdout.splitlines() if ln.startswith("GATE "))
    return json.loads(line[len("GATE "):])


@pytest.mark.parametrize(
    "rel, bootstrap_expected",
    [
        # Наши прогоны: из корня репозитория и из каталога пакета. Обязаны
        # работать ровно как раньше — иначе гейт не почин, а замена поломки.
        ("", True),
        ("packages/core", True),
        # Чужой набор внутри нашего дерева: свой pyproject.toml, свой rootdir.
        ("external/vendor/thing-mcp", False),
    ],
    ids=["корень репозитория", "packages/<имя>", "вендоренный сервер"],
)
def test_bootstrap_follows_the_collected_suite(tmp_path, rel, bootstrap_expected):
    root = _working_copy(tmp_path)
    res = _run(root, rel)
    assert res.returncode == 0, res.stdout + res.stderr

    probe = _gate(res)
    # Страховка от вакуумности: убеждаемся, что pytest действительно объявил
    # корнем тот каталог, ради которого случай и заведён.
    assert probe["rootdir"] == str(root / rel if rel else root), probe
    assert probe["sentinel_importable"] is bootstrap_expected, probe


def test_incomplete_working_copy_does_not_break_the_run(tmp_path):
    """Неполное дерево: заглушка ``tools`` не собирается, но прогон живёт.

    Гейт рабочей копии проверяет ОДИН файл-маркер, а заглушке нужно целое
    дерево (``tools/session/stored_outputs/...``, ``tools/simple_tool_base.py``,
    ``tools/turn/submit_solution.py``). Раньше на неполном чекауте pytest падал
    голым ``FileNotFoundError`` из pluggy ДО сбора тестов, и в тексте ошибки не
    было ни слова про gc_testkit.
    """
    root = _working_copy(tmp_path)
    res = _run(root, "", hide_mcp=True, tmp_path=tmp_path)
    combined = res.stdout + res.stderr

    assert res.returncode == 0, combined
    assert "1 passed" in combined, combined
    assert "FileNotFoundError" not in combined, combined
    # Молчать тоже нельзя: отказ обязан назвать себя и недостающие файлы.
    assert "gc_testkit" in res.stderr, combined
    assert "simple_tool_base.py" in res.stderr, combined


def test_every_vendored_server_declares_its_own_rootdir():
    """У каждого вендоренного сервера обязана быть своя секция pytest.

    Гейт различает наборы по ``rootdir``, а pytest объявляет rootdir каталогом
    ближайшего манифеста, где ЕСТЬ ``[tool.pytest.ini_options]``. Без секции
    поиск поднимается до корневого ``pyproject.toml`` монорепозитория, rootdir
    становится нашим корнем — и гейт честно считает прогон своим, потому что он
    и правда неотличим от нашего.

    Так и было у ``ddgs-mcp``: три соседа секцию имели, он один — нет, и на нём
    утечка сохранялась после починки гейта. Проверка на манифесты, а не на
    прогон: она обязана падать на машине, где venv вендора ещё не собран.
    """
    from gc_testkit.paths import VENDOR

    manifests = sorted(VENDOR.glob("*/pyproject.toml"))
    assert manifests, f"в {VENDOR} не найдено ни одного манифеста"

    without = [
        m.parent.name
        for m in manifests
        if "[tool.pytest.ini_options]" not in m.read_text(encoding="utf-8")
    ]
    assert not without, (
        f"без секции [tool.pytest.ini_options]: {', '.join(without)}. "
        "pytest объявит rootdir корнем монорепозитория, и бутстрап тест-кита "
        "положит наши packages/*/src в чужой прогон вместе с заглушками "
        "trafilatura/readability — набор останется зелёным, потому что "
        "извлекать перестанет что бы то ни было."
    )
