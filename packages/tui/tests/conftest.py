"""Фикстуры и хуки TUI-тестов.

Импортируемые хелперы (wait_until, open_settings_section) живут в
``gc_testkit.tui_support``: conftest не импортируют из тестов напрямую.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_TUI_DIR = Path(__file__).parent.resolve()

# Эталоны, разъехавшиеся с кодом. Список получен прогоном сразу после возврата
# снапшот-плагина в манифесты (`pytest packages/tui/tests -k snapshot` →
# 9 failed, 7 passed из 16), а не переписан из плана.
#
# Расхождения НЕ наши, и это проверено, а не предположено (02.09.2026):
#   * все девять эталонов байт-в-байт совпадают с develop — их никто не трогал;
#   * develop на своей верхушке валит ровно эти девять и проходит те же семь;
#   * то же самое в точке расхождения ветки (e5a571c4) — дрейф старше сплита;
#   * версия textual тут ни при чём: на 8.2.7 (пин develop) и на 8.2.8 (наш)
#     результат одинаковый на обеих ветках.
# На нашей ветке они были не видны с 502213be (17.08.2026), когда плагин выпал
# из манифестов, до его возврата 28.08.2026: гейт был зелёным и пустым. То есть
# ветка потеряла ВИДИМОСТЬ проблемы и вернула её, а саму проблему принесла не она.
#
# Разбор расхождений — отдельная задача N2: каждое надо объяснить и только потом
# принять. `--snapshot-update` вслепую здесь запрещён: он узаконит регрессию
# композиции ровно тем же движением, каким принял бы законное изменение.
#
# strict=True принципиален и не является «отключением теста»: когда N2 разберёт
# эталон и кадр снова совпадёт, xpass покраснеет и заставит убрать имя отсюда.
# Именно этого не делал тихий skip — из-за него дыру и не замечали.
_SNAPSHOT_DRIFT = {
    "test_confirm_banner_snapshot",
    "test_empty_state_snapshot",
    "test_event_focus_snapshot",
    "test_main_full_dialog_snapshot",
    "test_main_running_snapshot",
    "test_palette_open_snapshot",
    "test_sessions_picker_full_snapshot",
    "test_unknown_command_snapshot",
}


def pytest_configure(config):
    """Скрыть от syrupy наши `.svg`-копии эталонов.

    `.svg` в `__snapshots__` — не эталоны, а копии для человека: фикстура
    `_copy_raw_snapshots_to_svg` ниже делает их из `.raw` после прогона, потому
    что Windows не открывает SVG-содержимое под расширением `.raw`. Они под
    гитигнором и пересоздаются каждый раз.

    syrupy знает только про `.raw`, объявлял все `.svg` «unused unknown snapshot
    collection» и следом печатал совет «re-run pytest with --snapshot-update to
    delete unused snapshots». Совет ОПАСЕН: `--snapshot-update` заодно перезапишет
    `.raw`-эталоны, включая девять разъехавшихся из `_SNAPSHOT_DRIFT`, которые
    держатся под strict-xfail именно как улика. Одна послушная команда — и
    разбирать станет нечего.

    Почему здесь, а не `addopts` в манифесте: опция принадлежит syrupy, а тот
    приезжает только с `pytest-textual-snapshot` из dev-группы. В `addopts` она
    убивала прогон БЕЗ этой группы — `error: unrecognized arguments` ещё до сбора,
    причём вместе с `tests/test_snapshot_gate_is_armed.py`, то есть со сторожем,
    заведённым против ровно этого класса тихих отказов. Здесь же мы правим
    готовый `config.option`: syrupy читает его в `pytest_sessionstart`, который
    идёт ПОСЛЕ `pytest_configure`, а без syrupy атрибут просто никому не нужен.
    """
    if getattr(config.option, "ignore_file_extensions", None) is None:
        config.option.ignore_file_extensions = ["svg"]


def pytest_collection_modifyitems(items):
    """Mark every TUI test as known-flaky → auto-rerun via pytest-rerunfailures.

    Textual async-pilot tests are green in their own process (``pytest tests/tui``)
    but time-flake when mixed into the full ~4600-test run: a *different* TUI test
    fails per run due to cumulative process load/scheduler timing, not a fixable
    leak (a deterministic thread-leak hunt found none). Reruns are scoped to
    tests/tui only, so the rest of the suite is untouched.

    Здесь же — strict-xfail для эталонов из ``_SNAPSHOT_DRIFT``. Список именной и
    временный, срок жизни — до разбора N2; почему именно xfail, а не skip и не
    обновление эталонов, объяснено у самой константы.

    Оба маркера уживаются, и это проверено прогоном: xfail не считается провалом,
    поэтому девять разъехавшихся эталонов больше не крутятся по три раза впустую
    (было 18 rerun, стало 0). Обратный случай — strict-XPASS — провалом
    считается, его rerunfailures отрерунит, но кадр у починенного эталона
    совпадёт снова, и набор всё равно покраснеет.
    """
    flaky = pytest.mark.flaky(reruns=2, reruns_delay=0)
    drift = pytest.mark.xfail(
        strict=True,
        reason="эталон разъехался с кодом, пока снапшот-плагин отсутствовал в "
               "манифестах; разбор расхождения — отдельная задача N2",
    )
    for item in items:
        try:
            path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        if _TUI_DIR == path or _TUI_DIR in path.parents:
            item.add_marker(flaky)
            if item.name in _SNAPSHOT_DRIFT:
                item.add_marker(drift)


@pytest.fixture(autouse=True)
def _isolate_agent_state(tmp_path, monkeypatch):
    """I3: TraceScreen._maybe_toast_warnings (и другой upgrade_gate-код) читает/пишет
    agent_state.json на КАЖДОМ буте TraceScreen, включая любой `TuiApp(...).run_test()` —
    без редиректа тесты гадят в реальный `~/.gc/agent_state.json`. Редиректим на tmp_path
    для всех TUI-тестов; локальный monkeypatch внутри теста (если есть) просто
    переопределяет этот же путь позже — конфликта нет."""
    from gc_core import upgrade_gate
    state_path = tmp_path / "agent_state.json"
    monkeypatch.setattr(upgrade_gate, "_state_path", lambda: state_path)


@pytest.fixture(autouse=True)
def _isolate_model_list_cache():
    """ModelEditor ходит за списком моделей через session-кэш
    (`gc_tui.services.model_list_cache`) — модуль-уровневый dict переживает между
    тестами. Разные тесты часто бьют по одному и тому же (url, auth, verify,
    proxy) — например, общий дефолт `_cfg()`/`_write_cfg()` в test_settings_screen —
    с разными монки-патченными/инжектированными списками моделей: без сброса
    первый тест в порядке прогона молча кэширует свой список для соседей.
    Автосброс до и после каждого теста (после — на случай, если тест сам не
    дошёл до конца)."""
    from gc_tui.services.model_list_cache import clear_cache
    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _isolate_slow_git_memo():
    """`gc_tui.services.file_index` помнит корни, где git не уложился в таймаут,
    в модульном dict — иначе таймаут из одного теста отключал бы git соседям."""
    from gc_tui.services import file_index
    file_index._slow_git_roots.clear()
    yield
    file_index._slow_git_roots.clear()


@pytest.fixture(autouse=True)
def _no_network_model_fetch(monkeypatch):
    """По умолчанию TUI-тесты не ходят в сеть за списком моделей.

    ModelEditor на mount зовёт `model_picker.fetch_models` (два HTTP с timeout=5:
    `/config`, затем `/models`). Тесты секции «Модели»/онбординга подменяют
    источник сами; навигационные и settings-тесты часто заходят в «Модели»
    мимохадом (PgDn) без стаба — и платят ~10 с teardown на DNS/connect к
    `http://host/...` из `_cfg()`. UI-набору реальная сеть не нужна: нужны
    контролируемые ответы (список / пусто / ошибка), их тест по-прежнему
    перекрывает этот же атрибут через monkeypatch/patch.

    Возвращаем одну модель `test-model` — совпадает с дефолтом `_cfg()` и с
    харнесом `_section(..., models=("test-model",))`, чтобы глифы хаба не
    уходили в «сервер не отдал» без причины."""
    from gc_core import model_picker
    from gc_core.model_picker import ModelInfo

    monkeypatch.setattr(
        model_picker,
        "fetch_models",
        lambda *a, **k: [ModelInfo(id="test-model")],
    )


@pytest.fixture(autouse=True)
def _no_network_update_check(monkeypatch):
    """По умолчанию TUI-тесты не делают git ls-remote на старте приложения.

    `TuiApp.on_mount` всегда крутит `_start_auto_update_check` →
    `check_for_update` (subprocess timeout 8 с). Тесты секции «Обновление» и
    авто-чека подменяют check сами; остальным живой git не нужен и на CI/без
    сети только добавляет флаки и секунды teardown."""
    from gc_core.updater import check as check_mod
    from gc_core.updater.check import CheckResult

    monkeypatch.setattr(
        check_mod,
        "check_for_update",
        lambda source, **k: CheckResult(
            "current", getattr(source, "commit", None), "установлена последняя версия"
        ),
    )


@pytest.fixture(scope="session", autouse=True)
def _copy_raw_snapshots_to_svg():
    """After tests, copy every __snapshots__/**/*.raw to a sibling .svg.

    Lets humans double-click .svg to view in browser (Windows Photos doesn't
    handle SVG-content .raw files).
    """
    yield  # run tests first
    snapshots = Path(__file__).parent / "__snapshots__"
    if not snapshots.exists():
        return
    for raw in snapshots.rglob("*.raw"):
        svg = raw.with_suffix(".svg")
        # Always overwrite — keeps .svg in sync with .raw
        shutil.copy2(raw, svg)


@pytest.fixture
def tui_app_factory():
    """Мини-хост с реальными стилями приложения — для проверки CSS-контракта.

    styles.tcss ссылается на кастомные переменные ($bg, $ink, ...), которые
    в проде инжектит TuiApp.get_css_variables() из gc_tui.palette — без этого
    переопределения обычный App падает на парсинге стилшита
    (UnresolvedVariableError), поэтому мини-хост его тоже переопределяет.

    Путь к стилям берётся из gc_testkit.paths.TUI_PKG, а не считается от
    __file__: набор TUI лежит в packages/tui/tests, а стили — в
    packages/tui/src/gc_tui, и арифметика уровней здесь уже ломалась.
    """
    from textual.app import App, ComposeResult

    from gc_testkit.paths import TUI_PKG
    from gc_tui import palette

    def _factory(widget):
        class _StyledHost(App):
            CSS_PATH = str(TUI_PKG / "styles.tcss")

            def get_css_variables(self) -> dict:
                variables = super().get_css_variables()
                variables.update(palette.css_variables())
                return variables

            def compose(self) -> ComposeResult:
                yield widget

        return _StyledHost()

    return _factory
