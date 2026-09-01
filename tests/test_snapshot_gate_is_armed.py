"""Сторож самого снапшот-гейта: плагин обязан быть в манифесте и в окружении.

Дефект, ради которого файл заведён, был не в эталонах, а в НЕВИДИМОСТИ. 502213be
(17.08.2026) снёс `pytest-textual-snapshot` из всех манифестов вместе с секцией
`[project.optional-dependencies] dev-tui`. Десять модулей `test_snapshot_*`
открываются строкой `pytest.importorskip("pytest_textual_snapshot")`, поэтому
шестнадцать эталонов не покраснели, а МОЛЧА пропали из прогона: набор оставался
зелёным, а композиция уезжала одиннадцать дней подряд.

Сами `importorskip` не удаляются намеренно — они держат самодостаточность набора
TUI (`cd packages/tui && pytest` без dev-группы). Значит, единственная защита от
повтора — проверка снаружи.

ЧТО ИМЕННО ЛОВИТ КАЖДАЯ ПОЛОВИНА, и почему они НЕ подменяют друг друга.
`test_plugin_is_declared_in_test_group` читает манифест: поймает «строку снова
убрали», но на машине, где venv уже собран, плагин никуда не денется, и вторая
половина останется зелёной. `test_snapshot_modules_collect_every_test` считает
собранное: поймает «имя в importorskip разъехалось с модулем плагина» и любую
поломку install-стороны, но работает лишь там, где `uv sync` уже прошёл, и на
удаление строки из манифеста не реагирует вовсе.

То есть каждая слепа к тому, что видит другая, и обе нужны. Настоящий инцидент
(строку убрали И окружение пересобрали) поймали бы обе; половинчатые состояния —
только соответствующая. Проверено подменами обеих сторон 01.09.2026.
"""
import re
import subprocess
import sys
import tomllib
from importlib.util import find_spec
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT_FILES = sorted((REPO / "packages/tui/tests").glob("test_snapshot_*.py"))

#: Имя модуля плагина — ровно то, которым его зовёт importorskip.
PLUGIN_MODULE = "pytest_textual_snapshot"
#: Имя дистрибутива в манифесте. Отличается от имени модуля дефисами, и именно
#: поэтому обе проверки нужны по отдельности.
PLUGIN_DIST = "pytest-textual-snapshot"


def test_snapshot_files_exist():
    """Страховка от вакуумности: если модули переименуют, остальные тесты файла
    начнут проверять пустоту и останутся зелёными."""
    assert len(SNAPSHOT_FILES) >= 10, [p.name for p in SNAPSHOT_FILES]


def test_plugin_is_declared_in_test_group():
    """Подмена, которая до этого файла НЕ краснела: убрать строку из манифеста.

    Группа `test` — единственное место, откуда плагин приезжает и разработчику,
    и CI: у листовых манифестов dev-зависимостей нет вовсе.
    """
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    group = root["dependency-groups"]["test"]
    pinned = [d for d in group if isinstance(d, str) and d.startswith(PLUGIN_DIST)]
    assert pinned, f"{PLUGIN_DIST} пропал из [dependency-groups] test: {group}"


def test_plugin_module_name_matches_importorskip():
    """Плагин ставится — и зовут его тем же именем, каким открываются модули.

    Опечатка в `importorskip` вернула бы ровно исходную дыру: тесты снова
    пропускались бы молча, а манифест выглядел бы здоровым.
    """
    assert find_spec(PLUGIN_MODULE) is not None, (
        f"{PLUGIN_DIST} не установлен — снапшот-эталоны никто не проверяет"
    )
    called = set()
    for path in SNAPSHOT_FILES:
        called |= set(re.findall(r'importorskip\(\s*"([\w.]+)"', path.read_text(encoding="utf-8")))
    assert PLUGIN_MODULE in called, sorted(called)
    assert called <= {PLUGIN_MODULE, "textual"}, (
        f"неизвестное имя в importorskip — оно может тихо гасить весь модуль: {sorted(called)}"
    )


def test_snapshot_modules_collect_every_test():
    """Ноль пропущенных: снапшот-модули обязаны СОБРАТЬСЯ целиком.

    Проверяем сбором в отдельном процессе, а не импортом плагина: `importorskip`
    срабатывает на уровне МОДУЛЯ, и любая его причина (нет плагина, нет textual,
    опечатка в имени) выносит весь файл из прогона одинаково незаметно.
    """
    expected = sum(
        len(re.findall(r"^def (test_\w+)", p.read_text(encoding="utf-8"), re.M))
        for p in SNAPSHOT_FILES
    )
    assert expected >= 16, expected
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", *[str(p) for p in SNAPSHOT_FILES]],
        cwd=str(REPO), capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    # rc=5 — «no tests collected»: ровно то, что происходило одиннадцать дней,
    # только тогда снапшот-модули были частью большого прогона и их молчание
    # тонуло в чужих зелёных точках.
    assert proc.returncode == 0, f"сбор снапшот-модулей упал (rc={proc.returncode})\n{out[-3000:]}"
    collected = [line for line in proc.stdout.splitlines() if "::test_" in line]
    assert len(collected) == expected, (
        f"собрано {len(collected)} из {expected} — модули пропускаются молча\n{out[-3000:]}"
    )
    assert "skipped" not in out, out[-3000:]
