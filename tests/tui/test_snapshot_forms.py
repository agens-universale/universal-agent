"""Эталоны форм ДО перехода на единый язык (спека 2026-08-03, §11.7).

Снимаются с нынешнего вида намеренно: следующие задачи меняют композицию, и
дельта на этих эталонах — единственный способ увидеть, что поехало не то.
Обновлять их можно только осознанно, объясняя каждое изменение.

Детерминизм — условие работоспособности эталона: сети, реального конфига машины
и времени в кадре быть не должно. Поэтому мастер и /settings монтируются с
инъекцией (список моделей, проба подключения, MCP, голосовой фасад), а состояние
воспроизводится теми же приёмами, что в обычных тестах соседних файлов.
"""
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from tests.tui.conftest import open_settings_section, wait_until
from tests.tui.test_onboarding_screen import _screen as _wizard_screen
from tests.tui.test_onboarding_voice_step import FakeVoice
from tests.tui.test_settings_screen import _cfg, _write_cfg
from tests.tui.test_settings_screen import _mk_screen as _mk_settings
from tests.tui.test_settings_voice import CLOUD, CLOUD_CONFIG
from tests.tui.test_settings_voice import _mk_screen as _mk_voice_settings

_SIZE = (120, 40)
# Шаги мастера длиннее одного экрана: над формой стоят лого, тэглайн, степпер и
# сводка, и в 40 строк форма целиком не влезает — в реальном терминале её листают
# (#onb-scroll). Эталон, обрезанный по низу, стерёг бы только верх формы, а
# следующие задачи меняют её всю, поэтому кадры мастера снимаем на высоте, где
# форма помещается целиком вместе с футером.
_WIZARD_SIZE = (120, 55)


def _models():
    """Список моделей — всегда фейком: сеть сделала бы эталон недетерминированным.
    Первая — та же, что в `_cfg()`/`_write_cfg()`, чтобы в /settings она встала
    строкой «текущая»; последняя не tool_capable — в кадр попадает и ⚠-строка."""
    from model_picker import ModelInfo
    return [ModelInfo(id="test-model", tool_capable=True),
            ModelInfo(id="glm-5.2-fp8", tool_capable=True),
            ModelInfo(id="openai/gpt-5.6-terra", tool_capable=False)]


def _ok_probe(*a, **k):
    """Проба подключения — фейком: у мастера она инъецируется, и без подмены
    авто-проба шага «Подключение» ушла бы в сеть."""
    from tui.services.settings_probe import ProbeResult
    return ProbeResult("ok", 200, None, 5, "доступен")


def _wizard(tmp_path, **over):
    """Экран мастера поверх общего `_screen` соседнего файла: конфиг — шаблонные
    заглушки (первичный запуск), MCP — фейк, сеть закрыта пробой и fetch'ем."""
    kw = dict(config_path=str(tmp_path / "config.yml"), probe=_ok_probe)
    kw.update(over)
    return _wizard_screen(**kw)


def _isolate_voice_state(monkeypatch, tmp_path):
    """Батарейка голоса — на tmp: `_voice_installed`/`current_model_short` читают
    реальный каталог данных агента, и кадр зависел бы от машины разработчика."""
    from voice_runtime import paths as vpaths
    monkeypatch.setattr(vpaths, "voice_data_dir", lambda: tmp_path / "voice_data")
    monkeypatch.setattr("voice_runtime.state.voice_state_path",
                        lambda: tmp_path / "voice_data" / "state.json")


async def _freeze_cursors(pilot):
    """Погасить мигание курсора во всех Input ПЕРЕД снимком.

    `Input._blink_timer` — это `set_interval(0.5, _toggle_cursor)`, а не анимация:
    ни `pilot.pause()`, ни `wait_for_scheduled_animations()` его не ждут и не
    гасят. Между фокусом и снимком проходит 150–250 мс, и под нагрузкой порог
    0.5 с переходится — курсор гаснет, кадр отличается на одну ячейку. `reruns=2`
    такой красный обычно замажет, а когда не замажет — он будет неотличим от
    настоящей регрессии композиции, и его просто «обновят», узаконив поломку.

    `cursor_blink = False` останавливает таймер и фиксирует `_cursor_visible=True`
    (см. `Input._watch_cursor_blink`) — курсор в кадре есть всегда, в одной фазе.

    Обходим именно `app.screen`, а НЕ `app`: `App.query` до виджетов запушенного
    экрана не достаёт и молча вернёт пустой набор — заморозка стала бы косметикой,
    а тест по-прежнему зависел бы от фазы таймера."""
    from textual.widgets import Input
    for inp in pilot.app.screen.query(Input):
        inp.cursor_blink = False
    await pilot.pause()


async def _wait_probe_settled(pilot, scr):
    """Дождаться ОСЕВШЕЙ строки вердикта секции «Подключение».

    Вход в секцию ставит `set_timer(0.05, _autorun_connection_probe)`, дальше —
    поток-воркер, и строка проходит три состояния: «— не проверено» → «проверяю…»
    → вердикт. Без гейта в кадр попадает то, что успело нарисоваться, а это
    зависит от нагрузки: разница на целую строку.

    Ждём `_draft_checked` (тот же признак, что у хелпера в conftest), а НЕ текст
    строки: секция монтируется с вердиктом уже отработавшей config-пробы, и её
    «✓ сервер доступен» — подстрока финального «✓ сервер доступен · авторизация
    не требуется». Гейт по тексту прошёл бы на нулевой итерации, до автопробы, и
    при пере-снятии эталона запечатал бы укороченную строку."""
    await wait_until(
        pilot, lambda: scr._draft_checked is not None, "автопроба секции осела")
    await pilot.pause()


def _isolate_config_layers(monkeypatch, tmp_path):
    """Слои поверх базового config.yml — на tmp (тот же приём, что автофикстура
    test_settings_voice). Нужны ВСЕМ секциям /settings, не только голосу:
    `SettingsScreen.__init__` → `_read_origins()` → `config_origin._overlay_paths`
    читает настоящие `~/.gc/config.local.yml` и `<cwd>/.gc/config.yml`, и стоит
    любому из них перекрыть адрес, токен, модель или прокси — в кадр домонтируется
    строка «⚠ действует … — перекрыто». Без подмены кадр чист лишь случайно."""
    import config as config_mod
    import mcp_runtime.paths as mcp_paths
    monkeypatch.setattr(mcp_paths, "managed_config_path",
                        lambda: tmp_path / "config.local.yml")
    monkeypatch.setattr(config_mod, "_project_local_config_path", lambda: None)


def _app(tmp_path, config=None):
    """Приложение под снимок. Конфиг тот же, что у экрана: вход в секцию /settings
    забирает конфиг у приложения (`_sync_config_from_app`), и с пустым `config={}`
    секция «Модель» осталась бы без адреса — список моделей не пошёл бы грузиться."""
    from tui.app import TuiApp
    return TuiApp(threads_dir=str(tmp_path), system="cli", config=config or {})


# --- мастер первого запуска ---

def test_wizard_connection_snapshot(snap_compare, tmp_path):
    """Шаг «Подключение» мастера: три поля, пресеты раскрыты под адресом."""
    app = _app(tmp_path)

    async def setup(pilot):
        await pilot.pause()
        scr = _wizard(tmp_path)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await _freeze_cursors(pilot)

    assert snap_compare(app, terminal_size=_WIZARD_SIZE, run_before=setup)


def test_wizard_model_snapshot(snap_compare, tmp_path):
    """Шаг «Модель» мастера: список моделей + строка фильтра."""
    from tui.editors.connection import ConnectionEditor
    from tui.editors.model import ModelEditor
    from textual.widgets import Input
    app = _app(tmp_path)

    async def setup(pilot):
        await pilot.pause()
        scr = _wizard(tmp_path, fetch=lambda *a, **k: _models())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        await pilot.press("pagedown")           # PgDn — «далее» на шаг «Модель»
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await _freeze_cursors(pilot)

    assert snap_compare(app, terminal_size=_WIZARD_SIZE, run_before=setup)


def test_wizard_voice_cloud_snapshot(snap_compare, tmp_path, monkeypatch):
    """Шаг «Голосовой ввод» мастера: выбран «Свой endpoint», под ним — его поля."""
    from tui.screens.onboarding import _VOICE_STEP
    _isolate_voice_state(monkeypatch, tmp_path)
    app = _app(tmp_path)

    async def setup(pilot):
        await pilot.pause()
        scr = _wizard(tmp_path, voice=FakeVoice())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        # Шаги 0–2 требуют валидного ввода; сюда важен вид шага голоса, поэтому
        # доводим состояние напрямую — тем же приёмом, что _to_voice_step соседей.
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._values["Authorization"] = ""
        scr._values["model"] = "test-model"
        scr._step = _VOICE_STEP
        scr._render_step()
        await pilot.pause()
        await pilot.press("down")               # курсор → «Свой endpoint»
        await pilot.press("space")              # …и пометка на нём
        await pilot.pause()
        await _freeze_cursors(pilot)

    assert snap_compare(app, terminal_size=_WIZARD_SIZE, run_before=setup)


# --- /settings ---

def test_settings_connection_snapshot(snap_compare, tmp_path, monkeypatch):
    """/settings → «Подключение»: три поля, галка «без авторизации», строка пробы.

    Пресет-лист здесь не раскрыт — в отличие от мастера: адрес уже настроен
    (канон §2.1 ConnectionEditor — список поднимается только на пустом/пресетном)."""
    _isolate_config_layers(monkeypatch, tmp_path)
    app = _app(tmp_path, _cfg())
    cfg_file = _write_cfg(tmp_path)

    async def setup(pilot):
        await pilot.pause()
        scr = _mk_settings(config_path=str(cfg_file))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await _wait_probe_settled(pilot, scr)
        await _freeze_cursors(pilot)

    assert snap_compare(app, terminal_size=_SIZE, run_before=setup)


def test_settings_model_snapshot(snap_compare, tmp_path, monkeypatch):
    """/settings → «Модель»: список раскрыт (подсунут фейком, не из сети)."""
    import model_picker
    from tui.editors.model import ModelEditor
    # Инъекции fetch у секции «Модель» нет (список берёт `model_picker.fetch_models`
    # прямо на монтировании) — подменяем сам модуль-источник.
    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **k: _models())
    _isolate_config_layers(monkeypatch, tmp_path)
    app = _app(tmp_path, _cfg())
    cfg_file = _write_cfg(tmp_path)

    async def setup(pilot):
        await pilot.pause()
        scr = _mk_settings(config_path=str(cfg_file))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Модель")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await _freeze_cursors(pilot)

    assert snap_compare(app, terminal_size=_SIZE, run_before=setup)


def test_settings_voice_cloud_snapshot(snap_compare, tmp_path, monkeypatch):
    """/settings → «Голос»: курсор на «Свой endpoint», под ним — четыре поля."""
    _isolate_config_layers(monkeypatch, tmp_path)
    _isolate_voice_state(monkeypatch, tmp_path)
    app = _app(tmp_path, {"voice": {}})

    async def setup(pilot):
        await pilot.pause()
        scr, _fake = _mk_voice_settings(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Голосовой ввод")
        await pilot.pause()

    assert snap_compare(app, terminal_size=_SIZE, run_before=setup)


# Адрес совпадает с пресетом Groq (спека 2026-08-05 §3) — тем же приёмом, что и
# юнит-тест `test_url_list_opens_when_the_cursor_enters_the_field` в
# test_settings_voice.py: только так вход курсора на поле раскрывает список сам,
# без Alt+↓, и подсвечивает СВОЙ пресет, а не первую строку.
_URL_PRESET_CONFIG = ("model: m\nvoice:\n"
                      "  url: https://api.groq.com/openai/v1/audio/transcriptions\n"
                      "  token: old-token\n  model: whisper-1\n")


def test_settings_voice_url_presets_snapshot(snap_compare, tmp_path, monkeypatch):
    """/settings → «Голос»: курсор на поле «URL» — раскрыт пресет-лист адреса,
    подсвечена строка действующего провайдера (Groq)."""
    _isolate_voice_state(monkeypatch, tmp_path)
    app = _app(tmp_path, {"voice": {}})

    async def setup(pilot):
        await pilot.pause()
        scr, _fake = _mk_voice_settings(tmp_path, CLOUD, config_text=_URL_PRESET_CONFIG)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Голосовой ввод")
        await pilot.pause()
        await pilot.press("tab")        # строка режима → группа полей, первое поле — «URL»
        await pilot.pause()
        await _freeze_cursors(pilot)

    assert snap_compare(app, terminal_size=_SIZE, run_before=setup)
