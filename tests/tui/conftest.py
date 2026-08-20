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


def recording_loader(loads, models=()):
    """Загрузчик списка моделей голосового фасада (`voice_setup.list_models`):
    пишет `(url, token, verify, config_proxy)` в `loads` и отдаёт `models`.

    `models=None` — «не достучались» (это НЕ то же самое, что пустой список:
    пустой значит «сервер ответил, но моделей не отдал», см. voice_setup)."""
    def list_models(url, token, verify=True, config_proxy=None):
        loads.append((url, token, verify, config_proxy))
        return models
    return list_models


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

    `url` — адрес в файле конфига. Список раскрывается только над ПУСТЫМ полем
    (`can_open`) — поверх любого заданного адреса, хоть пресетного, он свёрнут:
    тесту про раскрытый список нужен пустой адрес либо форсированное раскрытие."""
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
                # Вердикт «Подключения» идёт через отдельный слот: не подменив его,
                # тест с фейком полез бы в сеть настоящей probe_auth.
                scr._auth_probe_fn = probe
            await pilot.app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, "Подключение")
            await pilot.pause()
            if wait_probe:
                await wait_until(pilot, lambda: scr._draft_checked is not None,
                                 "автопроба секции «Подключение» осела")
            yield pilot, scr


@asynccontextmanager
async def settings_voice_section(tmp_path, *, mode="cloud", probe=None,
                                 list_models=None, installed=False):
    """/settings → секция «Голосовой ввод».

    `mode` — действующий режим ("local" | "cloud" | "off"), `installed` — стоит ли
    локальная батарейка. `probe` — ProbeResult, который отдаёт `probe_endpoint`
    фасада (у голоса инъецируется результат, а не функция), `list_models` — что
    отдаёт загрузчик списка моделей (None = не достучались).

    Реальные слои конфига и каталог данных батарейки подменены на tmp: без этого
    вердикт секции зависел бы от машины (~/.gc/config.local.yml пишет наш же UI)."""
    import tui.app as app_mod
    from tests.tui.test_settings_voice import CLOUD_CONFIG, _mk_screen
    from tui.services.voice_setup import VoiceStatus
    status = VoiceStatus(
        installed=installed,
        service_alive=installed and mode == "local",
        port=8735 if installed else None,
        model="Systran/faster-whisper-small" if installed else None,
        mode=mode)
    config_text = CLOUD_CONFIG if mode == "cloud" else "model: m\n"
    with _isolated_config_layers(tmp_path), _isolated_voice_data(tmp_path):
        app = app_mod.TuiApp(config={"voice": {}})
        async with app.run_test() as pilot:
            scr, fake = _mk_screen(tmp_path, status, config_text=config_text)
            fake.probe_result = probe
            fake.models = list_models
            await pilot.app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, "Голосовой ввод")
            await pilot.pause()
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
async def wizard_voice_step(tmp_path, *, mode="cloud", installed=False, models=None):
    """Мастер первого запуска на шаге «Голосовой ввод», курсор — на строке `mode`
    ("local" | "cloud" | "skip"). Пометку (Space) хелпер НЕ ставит: показ полей
    зависит от курсора, а выбор — отдельное действие, и тесты про него разные.

    `models` — что отдаст endpoint на запрос списка (None = не достучались)."""
    from tests.tui.test_onboarding_voice_step import FakeVoice, _screen, _to_voice_step
    from tui.app import TuiApp
    from tui.screens.onboarding import _VOICE_OPTIONS, _VOICE_STEP
    keys = [k for k, _t, _d in _VOICE_OPTIONS]
    with _isolated_voice_data(tmp_path):
        app = TuiApp(config={"model": "M"})
        async with app.run_test() as pilot:
            voice = FakeVoice(installed=installed)
            voice.models = models
            screen = _screen(app, voice)
            await app.push_screen(screen)
            await _to_voice_step(pilot, screen)
            # По кольцу: курсор может стоять не на нулевой строке (шаг открывается
            # на ДЕЙСТВУЮЩЕМ режиме, _prefill_voice_from_config), и простая разность
            # шагов молча дала бы ноль нажатий вместо доводки до нужной строки.
            steps = (keys.index(mode) - screen._idx.get(_VOICE_STEP, 0)) % len(keys)
            for _ in range(steps):
                await pilot.press("down")
            await pilot.pause()
            yield pilot, screen


@asynccontextmanager
async def wizard_connection_step(tmp_path):
    """Мастер первого запуска на шаге «Подключение» (первый экран, как его видит
    новичок): пресет-лист адреса раскрыт сам — вход на поле его раскрывает."""
    from tests.tui.test_onboarding_voice_step import FakeVoice, _screen
    from tui.app import TuiApp
    with _isolated_voice_data(tmp_path):
        app = TuiApp(config={"model": "M"})
        async with app.run_test() as pilot:
            screen = _screen(app, FakeVoice())
            await app.push_screen(screen)
            await pilot.pause()          # ConnectionEditor.focus_input идёт call_after_refresh
            await pilot.pause()
            yield pilot, screen


@contextmanager
def _isolated_voice_data(tmp_path):
    """Каталог данных голосовой батарейки — на tmp: `_voice_installed`,
    `current_model_short` и статус секции читают реальный ~/.gc иначе."""
    with mock.patch("voice_runtime.paths.voice_data_dir",
                    lambda: tmp_path / "voice_data"), \
         mock.patch("voice_runtime.state.voice_state_path",
                    lambda: tmp_path / "voice_data" / "state.json"):
        yield


@contextmanager
def _isolated_config_layers(tmp_path):
    """Слои поверх базового config.yml — на tmp (тот же приём, что автофикстура
    _isolate_config_layers в test_settings_voice). Нужны любой секции /settings:
    `SettingsScreen.__init__` → `_read_origins()` читает настоящие
    `~/.gc/config.local.yml` и `<cwd>/.gc/config.yml`, и перекрытое там поле
    добавляет в секцию строку «⚠ действует … — перекрыто»."""
    with mock.patch("mcp_runtime.paths.managed_config_path",
                    lambda: tmp_path / "config.local.yml"), \
         mock.patch("config._project_local_config_path", lambda: None):
        yield
@pytest.fixture
def tui_app_factory():
    """Мини-хост с реальными стилями приложения — для проверки CSS-контракта.

    styles.tcss ссылается на кастомные переменные ($bg, $ink, ...), которые
    в проде инжектит TuiApp.get_css_variables() из tui.palette — без этого
    переопределения обычный App падает на парсинге стилшита
    (UnresolvedVariableError), поэтому мини-хост его тоже переопределяет.
    """
    from textual.app import App, ComposeResult

    from tui import palette

    def _factory(widget):
        class _StyledHost(App):
            CSS_PATH = str(Path(__file__).resolve().parents[2] / "src" / "tui" / "styles.tcss")

            def get_css_variables(self) -> dict:
                variables = super().get_css_variables()
                variables.update(palette.css_variables())
                return variables

            def compose(self) -> ComposeResult:
                yield widget

        return _StyledHost()

    return _factory


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
