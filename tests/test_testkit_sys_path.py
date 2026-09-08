"""Бутстрап тест-кита кладёт на ``sys.path`` только ``packages/*/src``.

Корень репозитория раньше клался тоже — «ради ``recommended_mcp``, он лежит в
корне». Обоснование протухло: рецепты уехали в ``packages/mcp/src`` и приезжают
вместе с остальными. Вред остался: корень на ``sys.path`` делает
namespace-пакетами ``docs``, ``external``, ``integrations``, ``packages`` и
``tests``, и наш ``external/`` СМЕШИВАЕТСЯ со сторонним дистрибутивом того же
имени в одном ``_NamespacePath``.

Живёт в корневом наборе, а не при пакете: у ``gc-testkit`` собственного набора
нет вовсе (``packages/testkit/tests`` не существует), а его плагин через
``pytest11`` участвует в прогоне ВСЕХ пакетов — это инвариант репозитория
целиком, ровно то, ради чего корневой набор и оставлен.

Проверяем подпроцессом с ``-S``. Без него утверждение проверяло бы не наш
бутстрап: editable-установки кладут в ``.venv`` свои ``.pth``, и все
``packages/*/src`` оказываются на ``sys.path`` сами по себе, а имя ``external``
в site-packages занимает каталог из колеса gc-mcp. С ``-S`` site-packages не
подключается вовсе, ``gc_testkit`` приезжает одним PYTHONPATH, и в
``sys.path`` видно ровно то, что положила ``ensure_sources_on_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from gc_testkit.paths import REPO, package_src_dirs

_PROBE = """\
import importlib.util, json, sys
from gc_testkit.sources import ensure_sources_on_path

before = list(sys.path)
ensure_sources_on_path()
print(json.dumps({
    "path": sys.path,
    "added": [p for p in sys.path if p not in before],
    "external_is_importable": importlib.util.find_spec("external") is not None,
}))
"""


def _probe(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "packages" / "testkit" / "src")
    # cwd — не репозиторий: иначе '' в sys.path сделало бы external импортируемым
    # мимо нашего бутстрапа, и проверка потеряла бы смысл.
    res = subprocess.run(
        [sys.executable, "-S", "-c", _PROBE],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_repo_root_is_not_put_on_sys_path(tmp_path):
    out = _probe(tmp_path)
    assert str(REPO) not in out["path"], (
        "корень репозитория снова кладётся на sys.path — вместе с ним "
        "namespace-пакетами становятся external/, docs/, integrations/"
    )


def test_external_does_not_become_a_namespace_package(tmp_path):
    out = _probe(tmp_path)
    assert not out["external_is_importable"], (
        "каталог external/ репозитория виден как пакет — наш external/ "
        "смешается со сторонним дистрибутивом того же имени"
    )


def test_bootstrap_puts_exactly_the_package_sources(tmp_path):
    """Страховка от вакуумности: бутстрап кладёт все ``packages/*/src`` и только их."""
    out = _probe(tmp_path)
    expected = {str(p) for p in package_src_dirs()}
    assert expected, "package_src_dirs() пуст — проверять нечего"
    # Сам testkit приезжает в подпроцесс через PYTHONPATH, поэтому в «добавленных»
    # его нет — он уже был. Всё остальное обязан положить бутстрап.
    expected_added = expected - {str(REPO / "packages" / "testkit" / "src")}
    assert set(out["added"]) == expected_added, sorted(set(out["added"]) ^ expected_added)
    assert expected <= set(out["path"])
