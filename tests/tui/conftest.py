"""TUI test fixtures."""
from __future__ import annotations

import shutil
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Callable
from unittest import mock

import pytest

_TUI_DIR = Path(__file__).parent.resolve()


def pytest_collection_modifyitems(items):
    """Mark every TUI test as known-flaky → auto-rerun via pytest-rerunfailures.

    Textual async-pilot tests are green in their own process (``pytest tests/tui``)
    but time-flake when mixed into the full ~4600-test run: a *different* TUI test
    fails per run due to cumulative process load/scheduler timing, not a fixable
    leak (a deterministic thread-leak hunt found none). Reruns are scoped to
    tests/tui only, so the rest of the suite is untouched.
    """
    flaky = pytest.mark.flaky(reruns=2, reruns_delay=0)
    for item in items:
        try:
            path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        if _TUI_DIR == path or _TUI_DIR in path.parents:
            item.add_marker(flaky)


@pytest.fixture(autouse=True)
def _isolate_agent_state(tmp_path, monkeypatch):
    """I3: TraceScreen._maybe_toast_warnings (и другой upgrade_gate-код) читает/пишет
    agent_state.json на КАЖДОМ буте TraceScreen, включая любой `TuiApp(...).run_test()` —
    без редиректа тесты гадят в реальный `~/.gc/agent_state.json`. Редиректим на tmp_path
    для всех TUI-тестов; локальный monkeypatch внутри теста (если есть) просто
    переопределяет этот же путь позже — конфликта нет."""
    import upgrade_gate
    state_path = tmp_path / "agent_state.json"
    monkeypatch.setattr(upgrade_gate, "_state_path", lambda: state_path)


@pytest.fixture(autouse=True)
def _isolate_model_list_cache():
    """ModelEditor ходит за списком моделей через session-кэш
    (`tui.services.model_list_cache`) — модуль-уровневый dict переживает между
    тестами. Разные тесты часто бьют по одному и тому же (url, auth, verify,
    proxy) — например, общий дефолт `_cfg()`/`_write_cfg()` в test_settings_screen —
    с разными монки-патченными/инжектированными списками моделей: без сброса
    первый тест в порядке прогона молча кэширует свой список для соседей.
    Автосброс до и после каждого теста (после — на случай, если тест сам не
    дошёл до конца)."""
    from tui.services.model_list_cache import clear_cache
    clear_cache()
    yield
    clear_cache()


async def wait_until(
    pilot,
    cond: Callable[[], bool],
    what: str,
    *,
    steps: int = 200,
    step_s: float = 0.05,
) -> None:
    """Ждать наблюдаемое условие (до ~10 с при дефолтах), не слепой pause."""
    for _ in range(steps):
        if cond():
            return
        await pilot.pause(step_s)
    raise AssertionError(f"timeout: {what}")


def settings_section_index(name: str) -> int:
    """Индекс секции SettingsScreen по отображаемому имени (SECTIONS), а не
    хардкод-номером: вставка новой секции в SECTIONS сдвигает номера соседей,
    но не имена — тест, прибитый к номеру, тихо открывает не ту секцию."""
    from tui.screens.settings import SECTIONS
    return SECTIONS.index(name)


def open_settings_section(scr, name: str) -> None:
    """Открыть секцию SettingsScreen хаба настроек ПО ИМЕНИ вместо хардкод-индекса
    (см. settings_section_index) — драйвер для scr._menu_idx/_open_section."""
    idx = settings_section_index(name)
    scr._menu_idx = idx
    scr._open_section(idx)


# --- фейки для форм (мастер + /settings) --------------------------------------
#
# Тесты форм раз за разом подсовывают одни и те же три подмены: пробу подключения,
# её же с журналом вызовов и загрузчик списка моделей. Держим их в одном месте —
# иначе у каждого файла своя копия с чуть иной сигнатурой, и «проверено ли, что
# прокси доехал до запроса» приходится выяснять заново в каждом.


def ok_probe():
    """Проба подключения, всегда отвечающая «сервер доступен».

    Сигнатура — как у `tui.services.settings_probe.probe`; годится и в `probe_fn`
    SettingsScreen, и в `probe=` OnboardingScreen."""
    def probe(endpoint, auth, verify, model, **kw):
        from tui.services.settings_probe import ProbeResult
        return ProbeResult("ok", 200, None, 5, "доступен")
    return probe


def recording_probe(calls, result=None):
    """Та же проба, но пишет каждый вызов в `calls` как
    `(endpoint, auth, verify, model, config_proxy)`.

    Журнал — единственный способ проверить, что до запроса доехали действующие
    значения (прокси/verify), а не то, что показано в полях."""
    def probe(endpoint, auth, verify, model, *, config_proxy=None, **kw):
        from tui.services.settings_probe import ProbeResult
        calls.append((endpoint, auth, verify, model, config_proxy))
        return result or ProbeResult("ok", 200, None, 5, "доступен")
    return probe


def blocking_probe(release, calls=None, result=None):
    """Проба, которая ВИСИТ, пока тест не отпустит `release` (threading.Event).

    Обычные фейки отвечают мгновенно и синхронно — окно «результат в полёте»
    у них практически нулевое, и гонки вокруг него (стрелка/Enter поверх летящей
    пробы) не выражаются вовсе: тест либо зелёный по удаче, либо флапает. Здесь
    длительность запроса задаёт сам тест — сценарий становится детерминированным,
    а не вероятностным, и повторные прогоны ничего не «доказывают» дополнительно.

        gate = threading.Event()
        async with settings_connection(tmp_path, probe=blocking_probe(gate),
                                       wait_probe=False) as (pilot, scr):
            gate.set()                      # отпустить автопробу входа в секцию
            await wait_until(pilot, lambda: scr._draft_checked is not None, "…")
            gate.clear()                    # дальше пробы висят, пока тест не разрешит

    Проба крутится в потоке-воркере (`run_worker(thread=True)`), поэтому блокировка
    интерфейс не морозит. `timeout` у ожидания — страховка от повисшего потока,
    когда тест упал, не дойдя до `set()`. `calls` — тот же журнал, что у
    `recording_probe` (пишется ДО ожидания: тесту нужен факт вылета запроса)."""
    def probe(endpoint, auth, verify, model, *, config_proxy=None, **kw):
        from tui.services.settings_probe import ProbeResult
        if calls is not None:
            calls.append((endpoint, auth, verify, model, config_proxy))
        release.wait(timeout=10)
        return result or ProbeResult("ok", 200, None, 5, "доступен")
    return probe


# --- монтаж экранов форм ------------------------------------------------------
#
# Каждый хелпер — асинхронный контекст-менеджер, отдающий `(pilot, экран)` уже в
# нужном состоянии. Собраны ПОВЕРХ фабрик соседних тест-файлов (`_screen`,
# `_mk_screen`, `_write_cfg`, `_harness`), а не рядом с ними: логика монтажа
# одного и того же экрана не должна расходиться по четырём копиям.


@asynccontextmanager
async def settings_connection(tmp_path, *, probe=None, wait_probe=True, url=None):
    """/settings → секция «Подключение» на настоящем config.yml во временном каталоге.

    **Отдаёт экран, когда автопроба входа в секцию уже осела.** Вход ставит
    `set_timer(0.05, _autorun_connection_probe)`, дальше поток-воркер, — то есть
    без ожидания проба срабатывает ПОСЛЕ `yield`. Тест вида «проверить, что прокси
    доехал до запроса» (ровно тот, ради которого заведён `recording_probe`) на
    быстром теле прочитал бы пустой `calls` и был бы зелёным, ничего не проверив.
    Наблюдаемое — `scr._draft_checked`: его ставит `_on_draft_probe`, а вход в
    секцию сбрасывает в None (`_reset_draft_state`).

        calls = []
        async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
            assert calls[0][4] == "http://corp-proxy:8080"   # config_proxy доехал

    `wait_probe=False` — для тестов, которым нужен вид секции ДО вердикта
    («— не проверено» / «проверяю…»); тогда ждать осадки должен сам тест.

    `url` — адрес в файле конфига. Дефолтный («http://host/…») в пресетах не
    значится, и пресет-лист на нём не раскрывается вовсе (`can_open`): тесту про
    раскрытый список нужен адрес пресета либо пустой."""
    import tui.app as app_mod
    from tests.tui.test_settings_screen import _cfg, _mk_screen, _write_cfg
    cfg_file = (_write_cfg(tmp_path) if url is None
                else _write_cfg(tmp_path, url=f'"{url}"'))
    # Конфиг приложения — тот же, что у экрана: вход в секцию забирает его у
    # приложения (SettingsScreen._sync_config_from_app), и с пустым конфигом
    # секция осталась бы без адреса.
    app = app_mod.TuiApp(config=_cfg())
    with _isolated_config_layers(tmp_path):
        async with app.run_test() as pilot:
            scr = _mk_screen(config_path=str(cfg_file))
            if probe is not None:
                scr._probe_fn = probe      # иначе остаётся ok-проба из _mk_screen
            await pilot.app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, "Подключение")
            await pilot.pause()
            if wait_probe:
                await wait_until(pilot, lambda: scr._draft_checked is not None,
                                 "автопроба секции «Подключение» осела")
            yield pilot, scr


@asynccontextmanager
async def settings_mcp_section(tmp_path, *, servers=()):
    """/settings → секция «MCP-серверы».

    `servers` — строки-имена (управляемые, не подключённые) или готовые `McpRow`,
    если нужны `wired`/`manual`/описание. Менеджер подменён целиком: настоящий
    ходит в манифест и в конфиг машины."""
    import tui.app as app_mod
    import tui.services.mcp_manager as mcp_manager_mod
    from tests.tui.test_settings_screen import _cfg, _mk_screen, _write_cfg
    from tui.services.mcp_manager import McpRow, McpState
    rows = [s if isinstance(s, McpRow) else McpRow(s, wired=False, manual=False)
            for s in servers]

    class _FakeManager:
        def __init__(self, **kw):
            self.calls = []
        def rows(self):
            return list(rows)
        def state(self, name):
            return McpState(False, None, None)
        def wire(self, name):
            self.calls.append(("wire", name)); return True
        def unwire(self, name):
            self.calls.append(("unwire", name)); return True
        def install(self, name):
            self.calls.append(("install", name)); return 0
        def remove(self, name):
            self.calls.append(("remove", name)); return 0

    cfg_file = _write_cfg(tmp_path)
    app = app_mod.TuiApp(config=_cfg())
    with _isolated_config_layers(tmp_path), \
         mock.patch.object(mcp_manager_mod, "McpManager", _FakeManager):
        async with app.run_test() as pilot:
            scr = _mk_screen(config_path=str(cfg_file))
            await pilot.app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, "MCP-серверы")
            await pilot.pause()
            yield pilot, scr


@asynccontextmanager
async def mounted_model_editor(tmp_path, *, models):
    """ModelEditor в одиночном харнесе со списком `models` (ModelInfo), уже
    загруженным воркером.

    `tmp_path` — ради единой формы вызова с соседними хелперами; сам харнес
    (`_harness` из test_editors_model) файлов не трогает."""
    from tests.tui.test_editors_model import _harness
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: list(models))
    app = _harness(ed)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: ed.models_loaded, "models loaded")
        yield pilot, ed


@asynccontextmanager
async def wizard_connection_step(tmp_path):
    """Мастер первого запуска на шаге «Подключение» (первый экран, как его видит
    новичок): пресет-лист адреса раскрыт сам — вход на поле его раскрывает.

    Работа идёт из `tmp_path`: снимок/сценарий не должен зависеть от того, из
    какого каталога запущен pytest."""
    import os
    from tests.tui.test_onboarding_screen import _screen
    from tui.app import TuiApp
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        app = TuiApp(config={"model": "M"})
        async with app.run_test() as pilot:
            screen = _screen()
            await app.push_screen(screen)
            await pilot.pause()      # ConnectionEditor.focus_input идёт call_after_refresh
            await pilot.pause()
            yield pilot, screen
    finally:
        os.chdir(cwd)


# --- цвет строк списков (§12.4: смысл несут слова, цвет — вспомогательный канал) ---
#
# Стражи грамматики строк спрашивают цвет КОНКРЕТНОЙ ячейки, а не «строки вообще»:
# один и тот же уровень обязан выглядеть одинаково всюду, где он назван. Живут
# хелперы здесь, а не в файле одной секции: тот же вопрос задают стражи секции
# «Модели», под-вида профиля и списка профилей.


def _value_color(text, value: str):
    """Цвет колонки значения строки — span, начинающийся ровно на значении (канон
    стража грамматики строки уровня в `test_settings_profiles_showcase.py`).

    Цвет нормализуется до RGB: у СМОНТИРОВАННОГО виджета `render()` отдаёт стиль,
    уже прошедший через консоль («rgb(122,130,144)»), а константа палитры — «#hex»;
    сравнение строк развело бы одинаковые цвета."""
    from rich.style import Style
    i = text.plain.index(value)
    span = next(s for s in text.spans if s.start == i)
    return Style.parse(str(span.style)).color.get_truecolor()


def _rgb(token: str):
    from rich.style import Style
    return Style.parse(token).color.get_truecolor()


def _row_style_set(text) -> set:
    """Цвета ВСЕЙ строки — множеством: «строка приглушена целиком» проверяется
    сравнением с одноэлементным множеством, а не перебором ячеек по одной.

    Пробельные куски в счёт не идут: курсор-маркер («▸ »/«  ») и выравнивание
    колонок цвета не несут, а в множество добавили бы посторонний ACCENT."""
    from rich.style import Style
    colors = set()
    for span in text.spans:
        if not text.plain[span.start:span.end].strip():
            continue
        style = Style.parse(str(span.style))
        if style.color is not None:
            colors.add(style.color.get_truecolor())
    return colors


@contextmanager
def _isolated_config_layers(tmp_path):
    """Слои поверх базового config.yml — на tmp. Нужны любой секции /settings:
    `SettingsScreen.__init__` → `_read_origins()` читает настоящие
    `~/.gc/config.local.yml` и `<cwd>/.gc/config.yml`, и перекрытое там поле
    добавляет в секцию строку «⚠ действует … — перекрыто»."""
    with mock.patch("mcp_runtime.paths.managed_config_path",
                    lambda: tmp_path / "config.local.yml"), \
         mock.patch("config._project_local_config_path", lambda: None):
        yield


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
