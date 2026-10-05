"""Пакет ``gc_core.attachments`` грузится лёгким, а импорт ядра не грузит Pillow.

Два обещания, одна проба в отдельном процессе — в процессе pytest всё это давно
загрузили соседние тесты:

- Pillow — жёсткая зависимость gc-core (C-расширение), но импортируется внутри
  функций нормализации: старт ядра за него не платит. Входы ядра
  (``agent_executor``, SDK) тянут почти всё ядро — так ловится и тул, однажды
  импортировавший Pillow на уровне модуля;
- модули пакета при загрузке не тянут ``security``, ``thread_store``, ``tools`` и
  ``persistence``: сам пакет импортируют тул просмотра и
  ``security/media_roots.py``, и обратный импорт замкнул бы цикл.

Модули пакета перебираются обходом, а не списком: модуль, добавленный позже,
попадает под проверку сам.
"""

import json
import os
import subprocess
import sys

import pytest

from gc_testkit.paths import package_src_dirs

_HEAVY = (
    "gc_core.security",
    "gc_core.thread_store",
    "gc_core.tools",
    "gc_core.persistence",
)

_PROBE = """
import importlib, json, pkgutil, sys
HEAVY = {heavy!r}
import gc_core.attachments as pkg
names = [pkg.__name__] + sorted(
    m.name for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."))
for name in names:
    importlib.import_module(name)
heavy = sorted(m for m in sys.modules if any(m == h or m.startswith(h + ".") for h in HEAVY))
for name in ("gc_core.agent_executor", "gc_core.aio.agent"):
    importlib.import_module(name)
pil = sorted(m for m in sys.modules if m.split(".")[0] == "PIL")
print(json.dumps({{"imported": names, "heavy": heavy, "pil": pil}}))
"""


@pytest.fixture(scope="module")
def probe() -> dict:
    env = dict(os.environ)
    src_dirs = [str(d) for d in package_src_dirs()]
    env["PYTHONPATH"] = os.pathsep.join(
        src_dirs + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    out = subprocess.run(
        [sys.executable, "-c", _PROBE.format(heavy=_HEAVY)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_walk_finds_the_package_modules(probe):
    # Пустой обход прошёл бы на любом коде — обход обязан доказать, что нашёл.
    assert {
        "gc_core.attachments.model",
        "gc_core.attachments.texts",
        "gc_core.attachments.numbering",
    } <= set(probe["imported"])


def test_attachments_load_without_heavy_core_packages(probe):
    assert probe["heavy"] == []


def test_importing_the_core_does_not_load_pillow(probe):
    assert probe["pil"] == []
