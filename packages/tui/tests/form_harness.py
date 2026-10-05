"""Монтаж форм мастера и /settings, их фейки и изоляторы.

Каждый монтажник — асинхронный контекст-менеджер, отдающий `(pilot, экран)` уже в
нужном состоянии. Собраны ПОВЕРХ фабрик соседних тест-файлов (`_screen`, `_mk_screen`,
`_write_cfg`, `_harness`), а не рядом с ними: логика монтажа одного и того же экрана
не должна расходиться по четырём копиям.

Живёт в наборе TUI, а не в `gc_testkit`. При вливании develop блок уехал в тест-кит
по правилу «импортируемые хелперы — не в conftest», и правило было исполнено, а
результат вышел перевёрнутым: общий устанавливаемый дистрибутив стал лениво
импортировать `test_settings_screen`, `test_settings_voice`, `test_editors_model`,
`test_onboarding_voice_step` — резолвилось это лишь потому, что pytest в тот момент
держал `packages/tui/tests` на `sys.path`. Здесь соседний импорт естественен: это тот
же набор, и тем же приёмом пользуется ядро
(`packages/core/tests/test_state_backend_wiring.py:3`).

Правило при этом не нарушено: `form_harness` — не conftest, из него импортировать
можно. В `gc_testkit.tui_support` остались `wait_until`, `open_settings_section` и
`settings_section_index` — они про экраны вообще, а не про эти формы.
"""

from __future__ import annotations

from gc_testkit.tui_support import tui_settings
from contextlib import asynccontextmanager, contextmanager
from unittest import mock

from gc_testkit.tui_support import open_settings_section, wait_until

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
async def settings_connection(
    tmp_path, *, probe=None, wait_probe=True, url=None
):
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

    cfg_file = (
        _write_cfg(tmp_path)
        if url is None
        else _write_cfg(tmp_path, url=f'"{url}"')
    )
    # Конфиг приложения — тот же, что у экрана: вход в секцию забирает его у
    # приложения (SettingsScreen._sync_config_from_app), и с пустым конфигом
    # секция осталась бы без адреса.
    app = app_mod.TuiApp(settings=tui_settings(_cfg()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config_path=str(cfg_file))
        if probe is not None:
            scr._probe_fn = probe  # иначе остаётся ok-проба из _mk_screen
            # Вердикт «Подключения» идёт через отдельный слот: не подменив его,
            # тест с фейком полез бы в сеть настоящей probe_auth.
            scr._auth_probe_fn = probe
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        if wait_probe:
            await wait_until(
                pilot,
                lambda: scr._draft_checked is not None,
                "автопроба секции «Подключение» осела",
            )
        yield pilot, scr


@asynccontextmanager
async def settings_voice_section(
    tmp_path, *, mode="cloud", probe=None, list_models=None, installed=False
):
    """/settings → секция «Голосовой ввод».

    `mode` — действующий режим ("local" | "cloud" | "off"), `installed` — стоит ли
    локальная батарейка. `probe` — ProbeResult, который отдаёт `probe_endpoint`
    фасада (у голоса инъецируется результат, а не функция), `list_models` — что
    отдаёт загрузчик списка моделей (None = не достучались).

    Реальные слои конфига и каталог данных батарейки подменены на tmp: без этого
    вердикт секции зависел бы от машины (~/.gc/config.local.yml пишет наш же UI)."""
    from gc_tui import app as app_mod
    from gc_tui.services.voice_setup import VoiceStatus
    from test_settings_voice import CLOUD_CONFIG, _mk_screen

    status = VoiceStatus(
        installed=installed,
        service_alive=installed and mode == "local",
        port=8735 if installed else None,
        model="Systran/faster-whisper-small" if installed else None,
        mode=mode,
    )
    config_text = CLOUD_CONFIG if mode == "cloud" else "model: m\n"
    with _isolated_voice_data(tmp_path):
        app = app_mod.TuiApp(settings=tui_settings({"voice": {}}))
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
    from gc_tui import app as app_mod
    from gc_tui.services import mcp_manager as mcp_manager_mod
    from gc_tui.services.mcp_manager import McpRow, McpState
    from test_settings_screen import _cfg, _mk_screen, _write_cfg

    rows = [
        s if isinstance(s, McpRow) else McpRow(s, wired=False, manual=False)
        for s in servers
    ]

    class _FakeManager:
        def __init__(self, **kw):
            self.calls = []

        def rows(self):
            return list(rows)

        def state(self, name):
            return McpState(False, None, None)

        def wire(self, name):
            self.calls.append(("wire", name))
            return True

        def unwire(self, name):
            self.calls.append(("unwire", name))
            return True

        def install(self, name):
            self.calls.append(("install", name))
            return 0

        def remove(self, name):
            self.calls.append(("remove", name))
            return 0

    cfg_file = _write_cfg(tmp_path)
    app = app_mod.TuiApp(settings=tui_settings(_cfg()))
    with mock.patch.object(mcp_manager_mod, "McpManager", _FakeManager):
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
    from gc_tui.editors.model import ModelEditor
    from test_editors_model import _harness

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: list(models),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: ed.models_loaded, "models loaded")
        yield pilot, ed


@asynccontextmanager
async def wizard_voice_step(
    tmp_path, *, mode="cloud", installed=False, models=None
):
    """Мастер первого запуска на шаге «Голосовой ввод», курсор — на строке `mode`
    ("local" | "cloud" | "skip"). Пометку (Space) хелпер НЕ ставит: показ полей
    зависит от курсора, а выбор — отдельное действие, и тесты про него разные.

    `models` — что отдаст endpoint на запрос списка (None = не достучались)."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import _VOICE_OPTIONS, STEP_VOICE
    from test_onboarding_voice_step import FakeVoice, _screen, _to_voice_step

    keys = [k for k, _t, _d in _VOICE_OPTIONS]
    with _isolated_voice_data(tmp_path):
        app = TuiApp(settings=tui_settings({"agent.model": "M"}))
        async with app.run_test() as pilot:
            voice = FakeVoice(installed=installed)
            voice.models = models
            screen = _screen(app, voice)
            await app.push_screen(screen)
            await _to_voice_step(pilot, screen)
            # По кольцу: курсор может стоять не на нулевой строке (шаг открывается
            # на ДЕЙСТВУЮЩЕМ режиме, _prefill_voice_from_config), и простая разность
            # шагов молча дала бы ноль нажатий вместо доводки до нужной строки.
            steps = (keys.index(mode) - screen._idx.get(STEP_VOICE, 0)) % len(
                keys
            )
            for _ in range(steps):
                await pilot.press("down")
            await pilot.pause()
            yield pilot, screen


@asynccontextmanager
async def wizard_connection_step(tmp_path):
    """Мастер первого запуска на шаге «Подключение» (первый экран, как его видит
    новичок): пресет-лист адреса раскрыт сам — вход на поле его раскрывает.

    Работа идёт из `tmp_path`: снимок/сценарий не должен зависеть от того, из
    какого каталога запущен pytest."""
    import os

    from gc_tui.app import TuiApp
    from test_onboarding_screen import _screen
    from test_onboarding_voice_step import FakeVoice

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Голосовой фасад подменён и на этом шаге: конструктор мастера берёт
        # реальный `voice_setup`, когда его не передали, — снимок «Подключения»
        # зависел бы от того, стоит ли на машине батарейка.
        with _isolated_voice_data(tmp_path):
            app = TuiApp(settings=tui_settings({"agent.model": "M"}))
            async with app.run_test() as pilot:
                screen = _screen(voice=FakeVoice())
                await app.push_screen(screen)
                await (
                    pilot.pause()
                )  # ConnectionEditor.focus_input идёт call_after_refresh
                await pilot.pause()
                yield pilot, screen
    finally:
        os.chdir(cwd)


@contextmanager
def _isolated_voice_data(tmp_path):
    """Каталог данных голосовой батарейки — на tmp: `_voice_installed`,
    `current_model_short` и статус секции читают реальный ~/.gc иначе."""
    with (
        mock.patch(
            "gc_tui.voice_runtime.paths.voice_data_dir",
            lambda: tmp_path / "voice_data",
        ),
        mock.patch(
            "gc_tui.voice_runtime.state.voice_state_path",
            lambda: tmp_path / "voice_data" / "state.json",
        ),
    ):
        yield


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
        if not text.plain[span.start : span.end].strip():
            continue
        style = Style.parse(str(span.style))
        if style.color is not None:
            colors.add(style.color.get_truecolor())
    return colors


# Фикстуры `tui_app_factory` и `_copy_raw_snapshots_to_svg` НЕ здесь: их дом —
# `packages/tui/tests/conftest.py`. На develop и они, и монтажники лежали в
# одном `tests/tui/conftest.py`, поэтому вливание принесло их сюда копией — с
# путём к стилям, посчитанным от старой глубины (`parents[2]/src/tui`). В
# conftest они уже есть и спрашивают раскладку у `gc_testkit.paths.TUI_PKG`.
