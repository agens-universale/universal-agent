"""Хелперы форм TUI-тестов: фейковые пробы и монтаж экранов.

Жили в conftest, но conftest нельзя импортировать из тестов: каталогов
``tests`` в репозитории шесть, модуль ``conftest`` у них общий по имени, и в
полном прогоне ``from conftest import ...`` цепляет чужой файл (наблюдалось
18.08.2026). В ``gc_testkit`` они тоже не годятся: монтаж собран ПОВЕРХ фабрик
соседних тест-файлов (``_cfg``/``_mk_screen``/``_write_cfg``/``_harness``), а
тест-кит — библиотека и о тестовых модулях знать не должен.

Поэтому — обычный модуль рядом с тестами, с уникальным именем: импортируется
как ``from form_helpers import settings_connection`` (каталог набора и так на
sys.path — тем же способом тесты ядра берут соседей).

Общие ``wait_until`` / ``open_settings_section`` остаются в
``gc_testkit.tui_support``: у них нет зависимости от тестовых модулей.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from unittest import mock

from gc_testkit.tui_support import open_settings_section, wait_until

def ok_probe():
    """Проба подключения, всегда отвечающая «сервер доступен».

    Сигнатура — как у `tui.services.settings_probe.probe`; годится и в `probe_fn`
    SettingsScreen, и в `probe=` OnboardingScreen."""
    def probe(endpoint, auth, verify, model, **kw):
        from gc_tui.services.settings_probe import ProbeResult
        return ProbeResult("ok", 200, None, 5, "доступен")
    return probe
def recording_probe(calls, result=None):
    """Та же проба, но пишет каждый вызов в `calls` как
    `(endpoint, auth, verify, model, config_proxy)`.

    Журнал — единственный способ проверить, что до запроса доехали действующие
    значения (прокси/verify), а не то, что показано в полях."""
    def probe(endpoint, auth, verify, model, *, config_proxy=None, **kw):
        from gc_tui.services.settings_probe import ProbeResult
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
        from gc_tui.services.settings_probe import ProbeResult
        if calls is not None:
            calls.append((endpoint, auth, verify, model, config_proxy))
        release.wait(timeout=10)
        return result or ProbeResult("ok", 200, None, 5, "доступен")
    return probe
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
    from gc_tui import app as app_mod
    from test_settings_screen import _cfg, _mk_screen, _write_cfg
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
async def settings_mcp_section(tmp_path, *, servers=()):
    """/settings → секция «MCP-серверы».

    `servers` — строки-имена (управляемые, не подключённые) или готовые `McpRow`,
    если нужны `wired`/`manual`/описание. Менеджер подменён целиком: настоящий
    ходит в манифест и в конфиг машины."""
    from gc_tui import app as app_mod
    from gc_tui.services import mcp_manager as mcp_manager_mod
    from test_settings_screen import _cfg, _mk_screen, _write_cfg
    from gc_tui.services.mcp_manager import McpRow, McpState
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
    from test_editors_model import _harness
    from gc_tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: list(models))
    app = _harness(ed)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: ed.models_loaded, "models loaded")
        yield pilot, ed
@asynccontextmanager
async def wizard_connection_step(tmp_path):
    """Мастер первого запуска на шаге «Подключение» (первый экран, как его видит
    новичок): пресет-лист адреса раскрыт сам — вход на поле его раскрывает."""
    from test_onboarding_screen import _screen
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen()
        await app.push_screen(screen)
        await pilot.pause()          # ConnectionEditor.focus_input идёт call_after_refresh
        await pilot.pause()
        yield pilot, screen
@contextmanager
def _isolated_config_layers(tmp_path):
    """Слои поверх базового config.yml — на tmp. Нужны любой секции /settings:
    `SettingsScreen.__init__` → `_read_origins()` читает настоящие
    `~/.gc/config.local.yml` и `<cwd>/.gc/config.yml`, и перекрытое там поле
    добавляет в секцию строку «⚠ действует … — перекрыто»."""
    # Дом функции — config_modules.env_vars; в mcp_runtime.paths остался лишь
    # реэкспорт, и патч по тому адресу молча не действует: _read_origins
    # (config_origin) импортирует её напрямую из env_vars.
    with mock.patch("gc_core.config_modules.env_vars.managed_config_path",
                    lambda: tmp_path / "config.local.yml"), \
         mock.patch("gc_core.config._project_local_config_path", lambda: None):
        yield
