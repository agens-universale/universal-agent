"""SettingsScreen: команда, меню секций, навигация (§2 визуального контракта)."""
import pytest

pytest.importorskip("textual")

from textual.widgets import Static
from tests.tui.conftest import (open_settings_section, recording_probe,
                                settings_connection, settings_mcp_section,
                                settings_section_index, settings_voice_section,
                                wait_until)


def _cfg():
    return {
        "completions_url": "http://host/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer secret-token"},
        "model": "test-model",
    }


def _fake_probe(result):
    def probe_fn(endpoint, auth, verify, model, **kw):
        return result
    return probe_fn


def _probe_ok(models_ids=("test-model",)):
    from model_picker import ModelInfo
    from tui.services.settings_probe import ProbeResult
    return ProbeResult("ok", 200, [ModelInfo(id=i) for i in models_ids], 118, "доступен")


def _mk_screen(config=None, config_path="C:/tmp/config.yml", on_apply=None):
    from tui.screens.settings import SettingsScreen
    return SettingsScreen(config=config or _cfg(), config_path=config_path,
                          on_apply=on_apply or (lambda: None),
                          probe_fn=_fake_probe(_probe_ok()))


def _write_cfg(tmp_path, *, url='"http://host/v1/chat/completions"',
               auth='"Bearer secret-token"', model='"test-model"'):
    """Настоящий config.yml: /settings берёт значения полей ИЗ ФАЙЛА (сырыми), а не из
    загруженного конфига — на моке-пути дыру с ${ENV:VAR} было не увидеть."""
    p = tmp_path / "config.yml"
    p.write_text(
        "backend:\n"
        f"  completions_url: {url}\n"
        "  chat_completion_headers:\n"
        f"    Authorization: {auth}\n"
        "agent:\n"
        f"  model: {model}\n",
        encoding="utf-8")
    return p


def _loaded(cfg_file):
    """Конфиг, как его видит приложение: YAML файла + резолв ${ENV:VAR} — тот же
    resolve_env_placeholders, что и в config.load_config (секции backend/agent — плоско)."""
    import yaml
    from config_modules.env_interpolation import resolve_env_placeholders
    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    flat = {**(raw.get("backend") or {}), **(raw.get("agent") or {})}
    return resolve_env_placeholders(flat)


async def _to_update_section(pilot):
    """Меню → секция «Обновление» стрелками, как это делает человек.

    Число нажатий берём из индекса секции, а не из литерала: состав меню растёт
    («Профили» из develop, «Голосовой ввод» из этой ветки), и захардкоженные пять
    «down» после каждой новой секции молча открывали бы соседнюю."""
    from tui.screens.settings import _UPDATE
    await pilot.press(*(["down"] * _UPDATE), "enter")
    await pilot.pause()


def _menu_lines(screen):
    """Текст строк меню: каждая строка — Horizontal(.set-menurow) с двумя Static
    (.set-rowtext + .set-glyph); собираем их рендер вместе."""
    from textual.widgets import Static
    out = []
    for row in screen.query(".set-menurow"):
        out.append(" ".join(str(s.render()) for s in row.query(Static)))
    return out


async def test_settings_menu_lists_sections_with_values(tmp_path):
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path)          # значения строк меню — из файла
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen(config_path=str(cfg_file)))
        await pilot.pause()
        from tui.screens.settings import SECTIONS
        rows = _menu_lines(app.screen)
        assert len(rows) == len(SECTIONS)      # 7: + «Профили» (develop) и «Голосовой ввод»
        assert any("Подключение" in r and "http://host" in r and "Bearer" in r for r in rows)
        assert not any("secret-token" in r for r in rows)  # маска!
        assert any("Модели" in r and "test-model" in r for r in rows)


async def test_connection_editor_lookup_takes_the_fresh_one():
    """Сохранение секции обязано читать поля СВЕЖЕГО редактора.

    `remove()` в Textual отложенный: после перерисовки секции старый редактор ещё в
    дереве. `query_one` на двух матчах не падает, а молча отдаёт ПЕРВЫЙ, обречённый, —
    и в конфиг уехали бы значения прошлого рендера. Канон файла (`_voice_editor`) —
    брать последний; для «Подключения» его не соблюдали (находка 2026-08-07)."""
    import tui.app as app_mod
    from tui.editors.connection import ConnectionEditor
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                    # секция «Подключение»
        await pilot.pause()

        doomed = scr._connection_editor()
        doomed.remove()                               # отложенное удаление: пока в дереве
        fresh = ConnectionEditor(url_current="http://fresh.example/v1/chat/completions",
                                 auth_header=None)
        doomed.parent.mount(fresh, after=doomed)
        assert len(list(scr.query(ConnectionEditor))) == 2, \
            "не удалось воспроизвести два матча — тест перестал проверять то, что должен"

        assert scr._connection_editor() is fresh, "взят обречённый редактор вместо свежего"
        assert scr.query_one(ConnectionEditor) is doomed   # чем и был плох голый query_one


async def test_settings_enter_opens_section_and_esc_returns():
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                    # секция «Подключение»
        await pilot.pause()
        assert scr._view == 0
        header = str(scr.query_one("#header-left", Static).render())
        assert "Настройки · Подключение" in header
        assert "(1/5)" not in header                  # без счётчика
        assert not scr.query(".set-current")          # v2: строка «Текущая» удалена
        await pilot.press("escape")                   # пресет-лист адреса раскрыт сразу (канон §2.1) — свернуть его
        assert scr._view == 0                          # секция ещё открыта
        await pilot.press("escape")                   # назад в меню
        assert scr._view == "menu"
        await pilot.press("escape")                   # из меню — закрыть экран
        await pilot.pause()
        assert app.screen is not scr


async def test_settings_ctrl_pgup_pgdn_switch_sections():
    from tui.screens.settings import _MCP, _MODES, _UPDATE, _VOICE
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        scr = app.screen
        await pilot.press("down", "down", "down", "enter")   # секция MCP
        assert scr._view == _MCP
        await pilot.press("ctrl+pagedown")                   # → Голосовой ввод
        assert scr._view == _VOICE
        await pilot.press("ctrl+pagedown")                   # → Режимы работы
        assert scr._view == _MODES
        await pilot.press("ctrl+pagedown")                   # → Обновление (6/6)
        assert scr._view == _UPDATE
        await pilot.press("ctrl+pagedown")                   # край: остаёмся (без wrap)
        assert scr._view == _UPDATE
        await pilot.press("ctrl+pageup")
        assert scr._view == _MODES


async def test_ctrl_pgdn_works_with_focus_in_input():
    """C1/C2: Ctrl+PgDn из секции 0/1 (фокус в Input) не должен ронять приложение
    (DuplicateIds: id="set-current" при повторном монтировании секции)."""
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                     # секция 0 (Подключение); фокус → Input
        await pilot.pause()
        assert scr._view == 0
        await pilot.press("ctrl+pagedown")              # секция 0 → 1 (Модель)
        await pilot.pause()
        assert scr._view == 1
        assert app.screen is scr                        # приложение живо
        await pilot.press("ctrl+pagedown")               # секция 1 → 2 (Профили)
        await pilot.pause()
        assert scr._view == 2
        assert app.screen is scr


async def test_plain_pgup_pgdn_switch_sections():
    """PgUp/PgDn (без Ctrl) листают секции; Ctrl+ остаётся тихим алиасом."""
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                            # секция 0 (Подключение); фокус → Input
        await pilot.pause()
        assert scr._view == 0
        await pilot.press("pagedown")                         # 0 → 1 (из Input — событие всплывает)
        assert scr._view == 1
        await pilot.press("pagedown")                         # 1 → 2
        assert scr._view == 2
        await pilot.press("pageup")                           # 2 → 1
        assert scr._view == 1


async def test_plain_arrows_do_not_switch_sections():
    """Обычные ←/→ стрелки НЕ переключают секции (только PgUp/PgDn);
    курсор перемещается в Input, фокус остаётся в секции 0."""
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        scr = app.screen
        # Открыть секцию 0 (Подключение) — фокус уходит в Input автоматически
        await pilot.press("enter")
        await pilot.pause()
        assert scr._view == 0
        # Обычные стрелки не переключают секции (двигают курсор в Input)
        await pilot.press("right")                    # обычная стрелка вправо
        assert scr._view == 0                         # остаётся в секции 0
        await pilot.press("left")                     # обычная стрелка влево
        assert scr._view == 0                         # остаётся в секции 0


async def test_open_section_picks_up_default_changed_by_other_window(tmp_path):
    """Дыра 2: пока /settings уже открыт, другое окно сохраняет новое умолчание в
    config.yml — вход в секцию обязан увидеть свежее значение БЕЗ выхода из
    экрана и повторного /settings."""
    import os
    import time
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path, model='"old-model"')
    app = app_mod.TuiApp(config={"model": "placeholder"})
    app._config_path = str(cfg_file)
    app.reload_config()                              # база + снимок mtime — как в проде
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen(config=app._config, config_path=str(cfg_file)))
        await pilot.pause()
        scr = app.screen

        _write_cfg(tmp_path, model='"new-model"')     # «другое окно» сохранило умолчание
        t = time.time() + 5.0
        os.utime(cfg_file, (t, t))

        open_settings_section(scr, "Модели")
        await pilot.pause()

        assert scr._raw_value("model") == "new-model"


async def test_open_section_noop_when_file_unchanged(tmp_path, monkeypatch):
    """Файл не менялся — вход в секцию не перечитывает конфиг и не инвалидирует
    раннер (дефект, который на этой ветке уже ловили дважды: лишняя пересборка
    executor тянет MCP-дискавери и держит пользователя на старте хода)."""
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path)
    app = app_mod.TuiApp(config={"model": "placeholder"})
    app._config_path = str(cfg_file)
    app.reload_config()

    calls = {"load": 0, "invalidate": 0}
    monkeypatch.setattr("config.load_config", lambda *_a, **_k: (
        calls.__setitem__("load", calls["load"] + 1) or {}
    ))
    orig_invalidate = app.invalidate_runner

    def counting_invalidate():
        calls["invalidate"] += 1
        orig_invalidate()

    app.invalidate_runner = counting_invalidate

    async with app.run_test() as pilot:
        app.push_screen(_mk_screen(config=app._config, config_path=str(cfg_file)))
        await pilot.pause()
        scr = app.screen

        open_settings_section(scr, "Модели")
        await pilot.pause()

        assert calls == {"load": 0, "invalidate": 0}


async def test_open_section_noop_when_config_is_empty():
    """Пустая (falsy) база — отдельный случай от «файл не менялся» выше:
    screen_config() (`self._config or {}`) и __init__ этого экрана (`config or {}`)
    на falsy-ветке аллоцируют новый {} при каждом обращении, так что голое `is`
    между двумя пустыми базами лгало бы «изменилось». Без config_path _read_raw/
    _read_origins дёшевы ({} за счёт раннего return), поэтому лишний вызов не
    упал бы сам — считаем вызовы явно."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    app = app_mod.TuiApp(config={})   # falsy _config; без config_path — reload не триггерится вовсе
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=app.screen_config(), config_path=None,
                              on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()

        calls = {"raw": 0, "origins": 0}
        orig_raw, orig_origins = scr._read_raw, scr._read_origins
        scr._read_raw = lambda: (calls.__setitem__("raw", calls["raw"] + 1) or orig_raw())
        scr._read_origins = lambda: (calls.__setitem__("origins", calls["origins"] + 1) or orig_origins())

        open_settings_section(scr, "Модели")
        await pilot.pause()

        assert calls == {"raw": 0, "origins": 0}


async def test_settings_apply_endpoint_writes_and_reloads(tmp_path):
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n  completions_url: \"http://old/v1\"\nagent:\n  model: \"m\"\n",
        encoding="utf-8")
    applied = []
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: applied.append(1),
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        await pilot.press("enter")                        # секция «Подключение»
        from textual.widgets import Input
        app.screen.query_one("#conn-url", Input).value = "https://new-host/v1/chat/completions"
        await pilot.press("enter")                        # применить (умный Enter → probe → ok)
        await pilot.pause(); await pilot.pause()
        assert "https://new-host/v1/chat/completions" in cfg_file.read_text(encoding="utf-8")
        assert applied == [1]
        assert app.screen._view == "menu"                 # v2: возврат в хаб
        rows = [str(w.render()) for w in app.screen.query("#set-flash")]
        assert any("✓ Сохранено" in r for r in rows)


async def test_flash_plain_saved_has_no_trailing_dot(tmp_path):
    """Одиночное «Сохранено» (нет активной сессии, нет предупреждений) — без завершающей
    точки; точка ставится только как разделитель, когда следом идёт ещё текст."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from textual.widgets import Input, Static
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n  completions_url: \"http://old/v1\"\nagent:\n  model: \"m\"\n",
        encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        app.active_runner = None                          # нет активной сессии → одиночный флэш
        await pilot.press("enter")                        # секция «Подключение»
        app.screen.query_one("#conn-url", Input).value = "https://new-host/v1/chat/completions"
        await pilot.press("enter")                        # сохранить
        await pilot.pause(); await pilot.pause()
        flash = str(app.screen.query_one("#set-flash", Static).render())
        assert "Сохранено" in flash
        assert "Сохранено." not in flash                  # без завершающей точки


async def test_settings_esc_without_enter_changes_nothing(tmp_path):
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    original = "backend:\n  completions_url: \"http://old/v1\"\n"
    cfg_file.write_text(original, encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        await pilot.press("enter")
        from textual.widgets import Input
        app.screen.query_one("#conn-url", Input).value = "https://new/v1"
        await pilot.press("escape")                       # назад в меню без Enter
        assert cfg_file.read_text(encoding="utf-8") == original


async def test_settings_readonly_when_target_is_bundled_template():
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from config import _default_config_path
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=_default_config_path(),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        await pilot.press("enter")                        # секция «Подключение»
        rows = [str(w.render()) for w in app.screen.query(".set-hint")]
        assert any("недоступен для записи" in r for r in rows)
        from textual.widgets import Input
        assert not app.screen.query(Input)                # редактор не смонтирован


async def test_settings_enter_in_readonly_section_does_not_crash():
    """Enter в read-only секции не должен ронять приложение."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from config import _default_config_path
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=_default_config_path(),
                             on_apply=lambda: None,
                             probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")         # открыть секцию «Адрес» (read-only)
        await pilot.pause()
        assert scr._view == 0
        await pilot.press("enter")         # попытка сохранить — не должна крашить
        await pilot.pause()
        assert app.screen is scr           # экран жив
        rows = [str(w.render()) for w in scr.query(".set-hint")]
        assert any("недоступен для записи" in r for r in rows)


async def test_update_section_shows_version_and_sync_row(tmp_path, monkeypatch):
    import app_version
    import tui.app as app_mod
    import updater.check as check_mod
    from tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "agent_version", lambda: "0.3.0")
    monkeypatch.setattr(app_version, "install_source", lambda _read=None: app_version.InstallSource(
        url="https://gitverse.ru/x/y", ref=None, commit="abc", editable=False))
    monkeypatch.setattr(check_mod, "check_for_update",           # вход авто-проверяет → без сети
                        lambda source, **k: check_mod.CheckResult("current", "abc", "установлена последняя версия"))
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        await _to_update_section(pilot)                                      # меню → «Обновление»
        await pilot.pause()
        body = " ".join(str(w.render()) for w in app.screen.query(".set-dyn"))
        assert "v0.3.0" in body and "master" in body
        assert "gitverse.ru/x/y" in body
        assert "Обновить config.yml до текущей версии" in body
        # курсор по умолчанию — верхнее «Обновить агента»; для подсказки sync-строки
        # переводим курсор на неё (Обновить агента → Проверить → sync)
        await pilot.press("down", "down")
        await pilot.pause()
        body = " ".join(str(w.render()) for w in app.screen.query(".set-dyn"))
        assert "Перепишет" in body                     # контекстная подсказка sync-строки


async def test_update_section_sync_runs_and_reports(tmp_path, monkeypatch):
    import config_sync
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    report = config_sync.SyncReport(7, 1, [], ["old_key"], [], str(cfg_file) + ".bak-x")
    called = {}

    def _fake_sync(path, template_path=None):
        called["path"] = str(path)
        return report

    monkeypatch.setattr(config_sync, "sync_config", _fake_sync)
    applied = []
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: applied.append(1),
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        await _to_update_section(pilot)
        await pilot.press("enter")                     # секция → выполнить sync
        await pilot.pause()
        assert called["path"] == str(cfg_file)
        assert applied == [1]                          # конфиг перечитан после sync
        body = " ".join(str(w.render()) for w in app.screen.query(".set-dyn"))
        assert "перенесено: 7" in body and "old_key" in body and ".bak-x" in body


async def test_sync_skips_when_nothing_to_update(tmp_path, monkeypatch):
    import config_sync
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(config_sync, "sync_config", lambda *a, **k: called.append(1))
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        scr._run_sync()
        assert called == []                                       # sync_config НЕ вызван


async def test_update_section_dev_install_hides_source(monkeypatch, tmp_path):
    import app_version
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source", lambda _read=None: None)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        await _to_update_section(pilot)
        body = " ".join(str(w.render()) for w in app.screen.query(".set-dyn"))
        assert "установка из исходников" in body
        assert "Обновить config.yml" in body           # sync доступен и для dev


async def test_settings_auth_no_auth_writes_empty_header(tmp_path):
    """Выбор «без авторизации» записывает Authorization: "" в файл."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        "  completions_url: \"http://host/v1/chat/completions\"\n"
        "  chat_completion_headers:\n"
        "    Authorization: \"Bearer old\"\n"
        "agent:\n  model: \"m\"\n",
        encoding="utf-8")
    applied = []
    cfg = {
        "completions_url": "http://host/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer old"},
        "model": "m",
    }
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=cfg, config_path=str(cfg_file),
                                       on_apply=lambda: applied.append(1),
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        # открыть секцию «Подключение» (idx=0 в меню)
        open_settings_section(scr, "Подключение")
        await pilot.pause()               # дать редактору домонтироваться (on_mount → _presets_ctrl)
        assert not scr.query(".set-current")          # v2: строка «Текущая» удалена
        from tui.editors.connection import ConnectionEditor
        ed = scr.query_one(ConnectionEditor)
        # Пресет-лист не раскрыт: адрес свой, не совпадает ни с одним пресетом
        # (спека-отклонение №5) — вызов ниже на всякий случай, без эффекта.
        ed.escape_presets()
        ed.move(2)                        # адрес → токен → галка «без авторизации»
        ed.toggle_no_auth()               # ● без авторизации
        await pilot.press("enter")                     # применить (умный Enter → probe → ok)
        await pilot.pause(); await pilot.pause()
        content = cfg_file.read_text(encoding="utf-8")
        assert 'Authorization: ""' in content
        assert applied == [1]


async def test_space_on_galka_toggles_no_auth_and_freezes_token(tmp_path):
    """Space на позиции строки-галки «без авторизации» (настоящая клавиша, не
    прямой вызов метода) переключает её: поле токена гаснет — disabled, пусто."""
    cfg_file = _write_cfg(tmp_path, auth='"Bearer old-token"')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    from textual.widgets import Input
    from tui.editors.connection import ConnectionEditor
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        # Пресет-лист не раскрывается: адрес в поле свой, не совпадает ни с одним
        # пресетом (спека-отклонение №5) — ↓/↓ сразу ходят по форме.
        assert ed.presets_open is False
        await pilot.press("down", "down")               # курсор → галка «без авторизации»
        await pilot.pause()
        assert ed._no_auth is False
        await pilot.press("space")                      # ● включить
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert ed._no_auth is True
        assert tok.disabled is True
        assert tok.value == ""


async def test_space_off_galka_types_space_and_does_not_toggle(tmp_path):
    """Space, когда курсор НЕ на галке, — обычный пробел уходит в сфокусированный
    Input (например, токен); галка не переключается."""
    cfg_file = _write_cfg(tmp_path, auth='"Bearer old-token"')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    from textual.widgets import Input
    from tui.editors.connection import ConnectionEditor
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        # Пресет-лист не раскрывается: адрес свой, не совпадает ни с одним пресетом
        # (спека-отклонение №5) — ↓ сразу ходит по форме.
        assert ed.presets_open is False
        await pilot.press("down")                       # курсор → поле токена (НЕ галка)
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert ed._no_auth is False                     # галка не тронута
        assert tok.value == " "                          # пробел допечатался в поле


async def test_connection_section_esc_collapses_presets_enter_substitutes(tmp_path):
    """Поле адреса пустое (канон 2026-08-17: сам список раскрывается только на
    ПУСТОМ значении — см. test_preset_list.py) — вход в него раскрывает
    пресеты сразу (канон §2.1); Esc сворачивает СПИСОК, секция остаётся открытой;
    повторный вход на адрес раскрывает список заново; Enter подставляет url
    выделенного пресета; список уже свёрнут — следующий Enter обычный submit (не
    падает на повторном mount тех же id)."""
    from tui.screens.settings import SettingsScreen
    import tui.app as app_mod
    from textual.widgets import Input
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('backend:\n  completions_url: ""\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause(); await pilot.pause()   # дать call_after_refresh дорисовать строки
        assert scr.query_one("#conn-url", Input).value == ""
        rows = [str(w.render()) for w in scr.query(".conn-preset-row")]
        assert any("OpenRouter" in r for r in rows)
        assert any("Cloud.ru" in r for r in rows)
        assert any("GigaCode" in r for r in rows)

        await pilot.press("escape")                            # свернуть список (не закрыть секцию)
        await pilot.pause()
        assert not scr.query(".conn-preset-row")               # список свернулся…
        assert scr._view == settings_section_index("Подключение")   # …секция осталась открытой

        await pilot.press("up"); await pilot.press("down")      # уйти с адреса и вернуться → раскрыть заново
        await pilot.pause()
        assert scr.query(".conn-preset-row")
        await pilot.press("enter")                             # подставить выделенный (первый) пресет
        await pilot.pause()
        assert scr.query_one("#conn-url", Input).value == "https://openrouter.ai/api/v1/chat/completions"
        assert not scr.query(".conn-preset-row")               # список свернулся

        await pilot.press("enter")                             # список уже закрыт — обычный submit, без краха
        await pilot.pause()
        assert app.screen is scr


async def test_connection_preset_hint_follows_the_cursor_after_esc(tmp_path):
    """Свернув пресет-лист адреса `Esc`, человек должен видеть, чем его вернуть
    (спека 2026-08-06 §4.4) — тот же хинт и то же слово, что у голосовых списков
    (`show_closed_hint`, дословно `LIST_CLOSED_HINT`). До правки `escape_presets`
    сворачивал список молча — способа узнать про `Alt+↓` не было.

    Канон 2026-08-17: строка под адресом ПОСТОЯННА — уход курсора её не снимает и
    текст не меняет. Она ярлык способа, а не команда момента (как `Space —
    переключить` у галки, работающая только со своей позиции); что нажимать сейчас,
    отвечает футер. Полный путь: Esc → хинт появился → ушли на «Токен» → хинт НА
    МЕСТЕ (раньше он исчезал) → вернулись на пустой адрес → список поднялся снова."""
    from tui.editors.field_list import LIST_CLOSED_HINT
    from tui.editors.preset_list import PRESET_HINT
    cfg_file = _write_cfg(tmp_path, url='""')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        assert scr.query(".conn-preset-row")          # предусловие: список раскрыт
        await pilot.press("escape")                    # свернуть список, секция жива
        await pilot.pause()
        assert scr._view == settings_section_index("Подключение")
        hints = scr.query(".conn-url-preset-hint")
        assert hints and str(hints[-1].render()) == LIST_CLOSED_HINT
        await pilot.press("down")                       # уйти с адреса на токен
        await pilot.pause()
        hints = scr.query(".conn-url-preset-hint")
        assert hints and str(hints[-1].render()) == LIST_CLOSED_HINT   # строка постоянна
        await pilot.press("up")                         # вернуться на адрес
        await pilot.pause()
        # Поле здесь ПУСТОЕ — на нём канон список поднимает, и хинт снова канонный.
        # (С заполненным адресом список остался бы свёрнутым — см. таблицу состояний
        # в test_editors_connection::test_canon_table_of_list_states.)
        assert scr.query(".conn-preset-row")
        hints = scr.query(".conn-url-preset-hint")
        assert hints and str(hints[-1].render()) == PRESET_HINT


async def test_alt_down_over_own_address_opens_without_a_highlight(tmp_path):
    """Находка A финального ревью 2026-08-06: Alt+↓ раскрывает пресет-лист поверх
    своего адреса, которого нет среди пресетов (спека §2.4) — строки видны все,
    подсвеченной нет. Хинт под рамкой и футер обязаны звать Enter «оставить свой
    адрес», а не «подставить» — иначе первый Enter только сворачивает список, ничего
    не сохраняя."""
    from tui.editors.connection import ConnectionEditor
    from tui.editors.preset_list import PRESET_HINT_NO_MATCH
    from tui.screens.settings import _FOOTER_PRESET_LIST_NO_MATCH
    cfg_file = _write_cfg(tmp_path)          # адрес по умолчанию не входит в пресеты
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert ed.presets_open is False      # предусловие: свой адрес, список закрыт
        await pilot.press("alt+down")
        await pilot.pause()
        assert ed.presets_open is True
        rows = [str(w.render()) for w in scr.query(".conn-preset-row")]
        assert rows and not any(r.startswith("▸") for r in rows)   # видны все, подсвеченной нет
        hint = str(list(scr.query(".conn-url-preset-hint"))[-1].render())
        assert hint == PRESET_HINT_NO_MATCH
        footer = str(scr.query_one("#set-footer", Static).render())
        assert footer == _FOOTER_PRESET_LIST_NO_MATCH


async def test_connection_footer_names_alt_down_only_on_the_url_field(tmp_path):
    """Находка D финального ревью 2026-08-06: Alt+↓ раскрывает список только с адреса
    (`ConnectionEditor.on_url_field` гейтит именно эту позицию, `reload_presets`) —
    на «Токене»/«Прокси» клавиша молчит, и футер не должен её там обещать.

    Адрес пресетный, поэтому по канону 2026-08-17 список свёрнут с самого входа —
    сворачивать его `Esc` больше не нужно (и нельзя: с заполненным адресом `Esc`
    закрыл бы секцию, это названный побочный эффект канона)."""
    from config_modules.endpoint_presets import DEFAULT_ENDPOINT_PRESETS
    cfg_file = _write_cfg(tmp_path, url=f'"{DEFAULT_ENDPOINT_PRESETS[0].url}"')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        footer = str(scr.query_one("#set-footer", Static).render())
        assert "Alt+↓ — показать список" in footer
        await pilot.press("down")            # адрес → токен
        await pilot.pause()
        footer2 = str(scr.query_one("#set-footer", Static).render())
        assert "Alt+↓" not in footer2
        await pilot.press("up")              # обратно на адрес
        await pilot.pause()
        footer3 = str(scr.query_one("#set-footer", Static).render())
        assert "Alt+↓ — показать список" in footer3


async def test_esc_with_presets_closed_exits_section_as_before(tmp_path):
    """Esc при уже свёрнутом пресет-листе закрывает секцию — как раньше
    (регресс-проверка): первый Esc сворачивает список, второй — выходит в меню.
    Адрес пустой — только так список раскрыт с самого начала (спека-отклонение
    №5), иначе сворачивать было бы нечего."""
    cfg_file = _write_cfg(tmp_path, url='""')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        await pilot.press("escape")                     # 1-й Esc: список раскрыт по умолчанию → свернуть
        await pilot.pause()
        assert scr._view == settings_section_index("Подключение")   # секция ещё жива
        await pilot.press("escape")                     # 2-й Esc: список уже свёрнут → выйти из секции
        await pilot.pause()
        assert scr._view == "menu"


async def test_mcp_section_mounts_editor(tmp_path, monkeypatch):
    """Секция MCP монтирует полноценный McpSectionEditor (не заглушку-мост)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tui.editors.mcp_section import McpSectionEditor
    import mcp_runtime.cli as cli
    monkeypatch.setattr(cli, "installable_names", lambda config=None: ["office"])
    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(cli, "manual_server_names", lambda uc: [])
    monkeypatch.setattr(cli, "installed_state",
                        lambda n, config=None: {"installed": False, "version": None, "update": None})
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(tmp_path / "c.yml"),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down", "down", "down", "enter")   # секция MCP (idx=3)
        await pilot.pause()
        assert scr.query(McpSectionEditor)


async def test_mcp_confirm_banner_blocks_section_switch(tmp_path, monkeypatch):
    """Регресс: пока висит confirm-баннер MCP, PgUp/PgDn НЕ листает секцию (иначе
    _switch_section→_render_view молча уничтожит редактор и _pending без confirm/cancel).
    Ранний гейт секции MCP глотает всё, кроме enter (→ confirm) и escape (→ cancel)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tui.editors.mcp_section import McpSectionEditor
    import mcp_runtime.cli as cli
    monkeypatch.setattr(cli, "installable_names", lambda config=None: ["office"])
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office"])
    monkeypatch.setattr(cli, "manual_server_names", lambda uc: [])
    monkeypatch.setattr(cli, "installed_state",
                        lambda n, config=None: {"installed": True, "version": "0.3.1", "update": "0.4.0"})
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(tmp_path / "c.yml"),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down", "down", "down", "enter")   # секция MCP (idx=3)
        await pilot.pause()
        ed = scr.query_one(McpSectionEditor)
        assert ed.request_install(["office"], verb="Обновить") is True   # поднять confirm-баннер
        await pilot.pause()
        assert ed.awaiting_confirm is True
        await pilot.press("pagedown")                        # попытка листнуть секцию
        await pilot.pause()
        assert scr._view == 3                                # секция НЕ сменилась
        assert ed.awaiting_confirm is True                   # баннер на месте
        assert list(ed.query(".mcp-confirm"))                # виджет баннера жив
        await pilot.press("escape")                          # отмена confirm
        await pilot.pause()
        assert ed.awaiting_confirm is False
        assert scr._view == 3                                # escape отменил confirm, не секцию


async def test_apply_endpoint_oserror_shows_notice(tmp_path, monkeypatch):
    """I2: OSError при write_values → notice «✗ не сохранено», приложение живо."""
    import onboarding_config_writer
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n  completions_url: \"http://old/v1\"\nagent:\n  model: \"m\"\n",
        encoding="utf-8")
    monkeypatch.setattr(onboarding_config_writer, "write_values",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("locked")))
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                   # открыть секцию «Подключение»
        from textual.widgets import Input
        app.screen.query_one("#conn-url", Input).value = "https://new/v1/chat/completions"
        await pilot.press("enter")                   # применить (умный Enter → probe → ok) → ожидаем OSError
        await pilot.pause(); await pilot.pause()
        assert app.screen is scr                     # приложение живо
        rows = [str(w.render()) for w in scr.query(".set-hint")]
        assert any("не сохранено" in r and "locked" in r for r in rows)


async def test_apply_endpoint_notice_when_runner_alive(tmp_path, monkeypatch):
    """M3: при живом runner notice содержит «следующего запуска» + _runner_stale=True."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n  completions_url: \"http://old/v1\"\nagent:\n  model: \"m\"\n",
        encoding="utf-8")

    class _AliveRunner:
        def is_running(self): return True

    applied = []
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: applied.append(1),
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        app.active_runner = _AliveRunner()
        scr = app.screen
        await pilot.press("enter")                   # секция «Подключение»
        from textual.widgets import Input
        scr.query_one("#conn-url", Input).value = "https://new/v1/chat/completions"
        await pilot.press("enter")                   # применить (умный Enter → probe → ok)
        await pilot.pause(); await pilot.pause()
        assert scr._view == "menu"                    # v2: флэш — в хабе
        rows = [str(w.render()) for w in scr.query("#set-flash")]
        assert any("следующего запуска" in r for r in rows)


async def test_apply_unchanged_value_reports_no_change(tmp_path):
    """skip-unchanged: значение из конфига не тронуто → «ничего не изменено», без записи."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    original = "backend:\n  completions_url: \"http://host/v1/chat/completions\"\nagent:\n  model: \"m\"\n"
    cfg_file.write_text(original, encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                    # секция «Подключение» (предзаполнена из конфига)
        await pilot.press("enter")                    # применить без изменений
        await pilot.pause()
        assert scr._view == 0                          # остаёмся в секции
        title = str(scr.query_one("#set-title", Static).render())
        assert "ничего не изменено" in title          # реплика в слоте заголовка
        assert cfg_file.read_text(encoding="utf-8") == original


async def test_flash_disappears_on_next_key(tmp_path):
    """Флэш гасится следующим нажатием клавиши (down)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n  completions_url: \"http://old/v1\"\nagent:\n  model: \"m\"\n",
        encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                    # секция «Подключение»
        from textual.widgets import Input
        scr.query_one("#conn-url", Input).value = "https://new/v1/chat/completions"
        await pilot.press("enter")                    # применить (умный Enter → probe → ok)
        await pilot.pause(); await pilot.pause()
        assert scr.query("#set-flash")                 # флэш есть
        await pilot.press("down")
        assert not scr.query("#set-flash")              # погашен следующим нажатием


async def test_notice_dies_on_the_next_action_like_flash(tmp_path):
    """Правило В1 (ревизия владельца 2026-08-14): notice-отказ живёт по тому же
    правилу, что плашка успеха, — до следующего осознанного действия. Раньше он
    жил вечно (гасился только побочным эффектом _render_view, и в секциях со
    своим виджетом — Модели/MCP/Подключение — не гас никогда: висел после ухода
    курсора и даже одновременно с confirm-баннером другой строки). Гашение — по
    родовому маркеру EPHEMERAL, вместе с обёрткой .set-slot."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('agent:\n  model: "m"\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        scr._notice("✗ не сохранено: тест")
        await pilot.pause()
        assert scr.query("#set-notice")
        await pilot.press("down")
        await pilot.pause()
        assert not scr.query("#set-notice")             # погашен следующим действием
        assert not scr.query(".set-slot")               # обёртка снесена, не копится


async def test_report_survives_the_next_key_and_closes_on_escape(tmp_path):
    """Третий род сообщений (ревизия владельца по смоку): ОТЧЁТ операции
    (актуализация/сброс config.yml) читают и по нему действуют — гаснуть на
    следующей клавише он не имеет права. Живёт до явного Esc, который тратится
    ТОЛЬКО на него (из секции уводит следующий), футер на это время обещает
    ровно закрытие. Различается природа, а не длина текста."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen, _UPDATE
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('agent:\n  model: "m"\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        # Секция «Обновление» — ЕДИНСТВЕННАЯ, где отчёты живут: страж обязан идти
        # её путём. Прежняя версия входила в «Подключение» (Enter из меню), где
        # `_report` не появляется никогда, а стрелки тело не перерисовывают, —
        # и была зелёной на невозможном сценарии (находка ревью хвоста).
        scr._view = _UPDATE
        scr._render_view()
        await pilot.pause()
        scr._report("✓ config.yml обновлён\nбэкап: …")
        await pilot.pause()
        assert scr.query("#set-report")
        assert "Esc — закрыть отчёт" in str(
            scr.query_one("#set-footer", Static).render())
        for key in ("down", "up", "n"):
            await pilot.press(key)                     # ↑/↓ здесь ПЕРЕРИСОВЫВАЮТ тело
            await pilot.pause()
            assert scr.query("#set-report"), f"отчёт погас на «{key}»"
            assert "Esc — закрыть отчёт" in str(
                scr.query_one("#set-footer", Static).render()), key
        await pilot.press("escape")
        await pilot.pause()
        assert not scr.query("#set-report")             # первый Esc закрыл отчёт
        assert scr._view == _UPDATE, "тот же Esc увёл из секции"
        assert "Esc — закрыть отчёт" not in str(
            scr.query_one("#set-footer", Static).render())


async def test_a_reply_replaces_the_report_and_clears_its_promise(tmp_path):
    """Реплика и отчёт делят экран, но не время: реплика ЗАМЕЩАЕТ отчёт, а не
    ложится поверх. Иначе оставался бы флаг — футер обещал бы «Esc — закрыть
    отчёт», а первый Esc уходил бы впустую, не уводя из секции (находка ревью
    хвоста; реальный путь — «обновить config.yml» дважды: второй раз отвечает
    репликой «уже актуален»)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen, _UPDATE
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('agent:\n  model: "m"\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        scr._view = _UPDATE
        scr._render_view()
        await pilot.pause()
        scr._report("✓ config.yml обновлён")
        await pilot.pause()
        scr._notice("config.yml уже актуален — обновлять нечего")
        await pilot.pause()
        assert not scr.query("#set-report")             # отчёт уступил место реплике
        assert scr.query("#set-notice")
        footer = str(scr.query_one("#set-footer", Static).render())
        assert "Esc — закрыть отчёт" not in footer      # и обещание снято
        await pilot.press("escape")
        await pilot.pause()
        assert scr._view == "menu", "Esc съеден мёртвым флагом отчёта"


async def test_flash_ok_mounts_band_with_glyph(tmp_path):
    """Флэш успеха — плашка с классом set-flash-ok и ведущим ✓."""
    from tui.screens.settings import SettingsScreen
    from textual.widgets import Static
    import tui.app as app_mod

    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('backend:\n  completions_url: "http://h/v1"\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        scr._flash("Сохранено.", outcome="ok")
        await pilot.pause()
        w = scr.query_one("#set-flash", Static)
        assert w.has_class("set-flash-ok")
        assert "✓" in str(w.render())


async def test_flash_warn_mounts_band_with_warn_glyph(tmp_path):
    """Флэш-предупреждение — класс set-flash-warn и ведущий ⚠."""
    from tui.screens.settings import SettingsScreen
    from textual.widgets import Static
    import tui.app as app_mod

    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('backend:\n  completions_url: "http://h/v1"\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        scr._flash("Сервер отклонил токен.", outcome="warn")
        await pilot.pause()
        w = scr.query_one("#set-flash", Static)
        assert w.has_class("set-flash-warn")
        assert "⚠" in str(w.render())


async def test_apply_model_writes_and_returns_to_hub(tmp_path, monkeypatch):
    """T6: apply секции «Модель» → model в файле, возврат в хаб (без #set-current)."""
    import model_picker
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tests.tui.conftest import wait_until
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n  completions_url: \"http://host/v1\"\nagent:\n  model: \"old-model\"\n",
        encoding="utf-8")
    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **kw: [])
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        cfg = dict(_cfg())
        cfg["model"] = "old-model"

        def _on_apply():
            # Эмулирует reload_config: обновляет app._config (экран подхватит через getattr).
            app._config = dict(cfg)
            app._config["model"] = "new-model"

        app.push_screen(SettingsScreen(config=cfg, config_path=str(cfg_file),
                                       on_apply=_on_apply,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Модели")
        await pilot.pause()
        assert not scr.query(".set-current")          # v2: строка «Текущая» удалена
        from textual.widgets import Input
        from tui.editors.model import ModelEditor
        await wait_until(pilot, lambda: scr.query_one(ModelEditor).models_loaded, "models loaded")
        await pilot.press("down")                       # T9: fetch=[] + active → псевдо-список
        await pilot.pause()                             # [old-model] [вписать вручную…] — на неё
        inp = scr.query_one(Input)
        inp.value = "new-model"
        await pilot.press("enter")                     # применить
        await pilot.pause()
        content = cfg_file.read_text(encoding="utf-8")
        assert "new-model" in content
        assert scr._view == "menu"                     # v2: возврат в хаб


async def test_run_sync_on_apply_error_shows_notice(tmp_path, monkeypatch):
    """M7: on_apply бросает → «не перечитан» ПЕРЕД штатным sync-отчётом; файл записан."""
    import config_sync
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    report = config_sync.SyncReport(3, 0, [], [], [], str(cfg_file) + ".bak")
    monkeypatch.setattr(config_sync, "sync_config", lambda *a, **kw: report)
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(
            config=_cfg(), config_path=str(cfg_file),
            on_apply=lambda: (_ for _ in ()).throw(RuntimeError("reload failed")),
            probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        await _to_update_section(pilot)
        await pilot.press("enter")
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(".set-dyn"))
        assert "не перечитан" in body and "reload failed" in body
        assert "✓ config.yml обновлён" in body


async def test_esc_from_editor_section_clears_editor_and_restores_menu(tmp_path):
    """Регресс бага 2: корень редактора несёт onb-dyn, а не set-dyn — при Esc не
    удалялся (мусор вверху + меню внизу). Теперь чистим все дети фрейма."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tui.editors.connection import ConnectionEditor
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                     # открыть секцию 0 (Подключение)
        await pilot.pause()
        assert scr.query(ConnectionEditor)             # редактор смонтирован
        await pilot.press("escape")                    # назад в меню
        await pilot.pause()
        assert not scr.query(ConnectionEditor)         # редактор удалён (баг 2)
        from tui.screens.settings import SECTIONS
        assert len(scr.query(".set-menurow")) == len(SECTIONS)   # меню восстановлено во фрейме
        # мусор редактора (hint/input) не остался
        from textual.widgets import Input
        assert not scr.query(Input)


async def test_editor_section_frame_hugs_content(tmp_path):
    """Регресс бага 1: редактор — Vertical (дефолт height:1fr) растягивал рамку.
    height:auto → рамка обнимает контент (адрес+токен+варианты), не весь экран."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tui.editors.connection import ConnectionEditor
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        await pilot.press("enter")                     # секция 0 (Подключение)
        await pilot.pause()
        ed = app.screen.query_one(ConnectionEditor)
        # контент-высота (hint+url+пресет-лист(раскрыт по умолчанию)+токен+галка+прокси,
        # framed-отступы), не заполнение экрана (~24 строки) — виджет крупнее одиночного
        # EndpointEditor, но всё ещё компактен
        assert ed.size.height <= 20


async def test_hub_glyphs_ok_after_config_probe():
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path="C:/tmp/c.yml",
                                       on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        await pilot.pause()                           # воркер + call_from_thread
        rows = _menu_lines(app.screen)
        assert any("Подключение" in r and "✓" in r for r in rows)
        assert any("Модели" in r and "✓" in r for r in rows)      # test-model в списке


async def test_hub_glyphs_bang_on_network_unknown():
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    res = ProbeResult("unknown", None, None, 5000, "нет соединения (timeout)",
                      offline=True)   # сетевой отказ — реальная проба всегда метит его так
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path="C:/tmp/c.yml",
                                       on_apply=lambda: None, probe_fn=_fake_probe(res)))
        await pilot.pause()
        await pilot.pause()
        rows = _menu_lines(app.screen)
        assert any("Подключение" in r and "!" in r for r in rows)  # недоступен СЕЙЧАС


def test_hub_loading_glyph_is_running_dot_wave():
    """Глиф загрузки — три точки `···`, teal-подсветка перебегает по кадрам;
    осевший глиф — одиночный ✓/!/—."""
    scr = _mk_screen()
    scr._probe_in_flight = True
    scr._wave_i = 0
    a = scr._menu_glyph_text(0)
    scr._wave_i = 1
    b = scr._menu_glyph_text(0)
    assert str(a) == "···" and str(b) == "···"     # три точки, не тофу/одиночный
    assert a.spans != b.spans                        # подсветка сместилась (кадр волны)
    scr._probe_in_flight = False
    scr._probe_config = _probe_ok()
    assert str(scr._menu_glyph_text(1)) == "✓"       # осевший — одиночный глиф (Модель)


async def test_hub_glyph_model_not_in_list_is_dot_not_bang():
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path="C:/tmp/c.yml",
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok(("other-model",)))))
        await pilot.pause()
        await pilot.pause()
        rows = _menu_lines(app.screen)
        model_row = next(r for r in rows if "Модели" in r)
        assert "!" not in model_row                   # не в списке → —, не !
        assert "—" in model_row


async def test_stale_probe_result_is_dropped():
    """Гонка: результат с устаревшим generation не перезаписывает свежий."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path="C:/tmp/c.yml",
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.pause()
        fresh = scr._probe_config
        stale = _probe_ok(("stale-model",))
        scr._on_config_probe(stale, gen=scr._config_gen - 1)   # устаревший gen
        assert scr._probe_config is fresh


async def test_config_probe_survives_entering_section_while_flying():
    """Fix 2: config-probe и draft-probe — раздельные generation. Вход в секцию во время
    полёта config-probe (draft-reset) не должен хоронить его результат (глифы хаба не
    замирают на «…» навсегда)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path="C:/tmp/c.yml",
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.pause()
        scr._probe_in_flight = True                      # эмуляция «config-probe летит»
        gen = scr._config_gen
        open_settings_section(scr, "Подключение")            # draft-reset бампает draft_gen, не config_gen
        result = _probe_ok()
        scr._on_config_probe(result, gen=gen)
        assert scr._probe_in_flight is False
        assert scr._probe_config is result


async def test_open_connection_autoruns_differential_probe(tmp_path):
    """Вход в «Подключение» сам запускает differential-проверку (как Ctrl+Space):
    контроль без токена, затем повтор с токеном — полный вердикт сразу,
    без ручной клавиши. Проверяем сам факт вызовов probe_fn."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tui.services.settings_probe import ProbeResult
    from tests.tui.conftest import wait_until

    calls = []

    def probe(endpoint, auth, verify, model, **kw):
        calls.append(auth)
        return ProbeResult("rejected", 401, None, 10, "токен отклонён (401)")

    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path="C:/tmp/config.yml",
                             on_apply=lambda: None, probe_fn=probe)
        app.push_screen(scr)
        await pilot.pause(); await pilot.pause()
        calls.clear()                          # отсечь config-probe с монтирования
        open_settings_section(scr, "Подключение")
        await wait_until(pilot, lambda: len(calls) >= 2, "auto differential probe ran")
        assert calls[0] is None                # контроль БЕЗ токена ушёл сам
        assert calls[1]                        # затем повтор С токеном


def _probe_rejected():
    from tui.services.settings_probe import ProbeResult
    return ProbeResult("rejected", 401, None, 90, "токен отклонён (401)",
                       auth_state="rejected")


def _probe_unknown():
    from tui.services.settings_probe import ProbeResult
    return ProbeResult("unknown", None, None, 5000, "нет соединения (timeout)",
                       offline=True)


async def _open_addr_with_probe(app, pilot, tmp_path, probe_result, url="https://new/v1/chat/completions"):
    from tui.screens.settings import SettingsScreen
    from textual.widgets import Input
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://old/v1\"\n", encoding="utf-8")
    scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                         on_apply=lambda: None, probe_fn=_fake_probe(probe_result))
    app.push_screen(scr)
    await pilot.pause(); await pilot.pause()
    await pilot.press("enter")                        # секция 0
    await pilot.pause()
    scr.query_one("#conn-url", Input).value = url
    return scr, cfg_file


async def test_rejected_token_saves_without_force(tmp_path, monkeypatch):
    """Токен отклонён, сервер доступен → Enter СОХРАНЯЕТ сразу (оба ключа), флэш warn.
    Никакого второго Enter / «сохранить как есть»."""
    from tui.screens.settings import SettingsScreen
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod

    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('backend:\n  completions_url: "http://old/v1/chat/completions"\n'
                        '  chat_completion_headers:\n    Authorization: ""\n', encoding="utf-8")
    # probe: сервер доступен (control 401), токен отклонён (with_token 401)
    def probe(endpoint, auth, verify, model, **kw):
        return ProbeResult("rejected", 401, None, 10, "отклонён")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=probe)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        from tui.editors.connection import ConnectionEditor
        from textual.widgets import Input
        ed = scr.query_one(ConnectionEditor)
        ed.query_one("#conn-url", Input).value = "https://new/v1/chat/completions"
        values = scr._collect_values(0)
        r = ProbeResult("rejected", 401, None, 10, "отклонён")
        scr._smart_enter_decide(0, values, r)
        await pilot.pause()
        content = cfg_file.read_text(encoding="utf-8")
        assert "https://new/v1/chat/completions" in content   # сохранено сразу, без force


async def test_smart_enter_rejected_saves_immediately_with_warn(tmp_path):
    """Проверка ⊥ Сохранение: rejected — Enter сохраняет сразу (флэш warn), без блока
    и без второго Enter («сохранить как есть» убрано)."""
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr, cfg_file = await _open_addr_with_probe(app, pilot, tmp_path, _probe_rejected())
        await pilot.press("enter")                    # умный Enter → probe → rejected → сохранено сразу
        await pilot.pause(); await pilot.pause()
        assert scr._view == "menu"                    # сохранено, вернулись в хаб
        assert "new" in cfg_file.read_text(encoding="utf-8")
        flash = str(scr.query_one("#set-flash").render())
        assert "сервер отклонил токен" in flash


async def test_smart_enter_unknown_saves_immediately_with_warn(tmp_path):
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr, cfg_file = await _open_addr_with_probe(app, pilot, tmp_path, _probe_unknown())
        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        assert scr._view == "menu"                    # сохранено сразу (сеть не наказывает)
        assert "new" in cfg_file.read_text(encoding="utf-8")
        flash = str(app.screen.query_one("#set-flash").render())
        assert "Сохранено" in flash and "не удалось" in flash


async def test_ctrl_space_probes_draft_without_saving(tmp_path):
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr, cfg_file = await _open_addr_with_probe(app, pilot, tmp_path, _probe_ok())
        # Развод клавиш (спека 2026-08-06 §2): Ctrl+Space больше не спрашивает
        # пресет-лист — одно нажатие сразу уходит в пробу.
        await pilot.press("ctrl+space")
        await pilot.pause(); await pilot.pause()
        assert "new" not in cfg_file.read_text(encoding="utf-8")   # НЕ сохранено
        probe_line = str(scr.query_one(".set-probe", Static).render())
        # секция «Подключение» — единый differential-probe (адрес+токен разом): control
        # без токена уже отвечает ok → совокупный вердикт «авторизация не требуется»
        # (probe адреса отдельно от probe токена больше не существует).
        assert "авторизация не требуется" in probe_line
        # Пресет-лист не раскрывался (адрес чужой, Ctrl+Space его больше не спрашивает) —
        # Enter сразу про сохранение, свернуть нечего.
        await pilot.press("enter")                    # проверено → Enter сохраняет без повтора
        await pilot.pause()
        assert scr._view == "menu"


async def test_draft_probe_invalidated_by_edit_during_flight(tmp_path):
    """I1: правка значения во время полёта draft-probe инвалидирует вердикт — протухший
    воркер (gen с момента вылета) не решает судьбу уже изменённого значения."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://old/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")                     # секция «Адрес»
        await pilot.pause()
        stale_gen = scr._draft_gen                      # gen на момент вылета воркера (Enter на A)
        scr._reset_draft_state()                         # эмуляция правки на B во время полёта (I1)
        assert scr._draft_gen != stale_gen                # gen бампнулся правкой
        called = []
        scr._on_draft_probe(_probe_ok(), gen=stale_gen, section=0,
                            on_done=lambda r: called.append(r))
        assert called == []                               # cb не вызван — вердикт для A отброшен
        assert scr._draft_checked is None
        assert "old" in cfg_file.read_text(encoding="utf-8")   # файл не тронут непроверенным


async def test_draft_probe_no_auth_choice_probes_without_old_token(tmp_path):
    """M1: выбор «без авторизации» проверяется БЕЗ старого токена из конфига — probe
    получает auth=None, а не «Bearer old» (иначе проверяется не то, что сохранится)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tui.editors.connection import ConnectionEditor
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        "  completions_url: \"http://host/v1/chat/completions\"\n"
        "  chat_completion_headers:\n"
        "    Authorization: \"Bearer old\"\n",
        encoding="utf-8")
    cfg = {
        "completions_url": "http://host/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer old"},
        "model": "m",
    }
    captured = []

    def probe_fn(endpoint, auth, verify, model, **kw):
        captured.append(auth)
        return _probe_ok()

    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=cfg, config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=probe_fn)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()               # свернуть пресет-лист (открыт по умолчанию на адресе)
        ed.move(2)                        # адрес → токен → галка «без авторизации»
        ed.toggle_no_auth()               # ● без авторизации
        scr._start_draft_probe(0)
        await pilot.pause(); await pilot.pause()
        assert captured                                    # probe вызван
        assert captured[-1] is None                         # НЕ "Bearer old"


async def test_ctrl_space_in_auth_without_saved_address_hints_locally(tmp_path):
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg = {"chat_completion_headers": {"Authorization": "Bearer x"}, "model": "m"}
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  chat_completion_headers:\n    Authorization: \"Bearer x\"\n",
                        encoding="utf-8")
    calls = []
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=cfg, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: calls.append(1))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")                    # секция «Подключение»
        await pilot.pause()
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert calls == []                            # сети не было
        assert "адрес" in str(scr.query_one(".set-probe", Static).render())


async def test_dirty_marker_appears_on_edit_and_clears_on_revert(tmp_path):
    """T7: правка Input в секции «Адрес» помечает #set-title «изменено»;
    возврат к исходному значению снимает маркер."""
    import tui.app as app_mod
    from textual.widgets import Input
    cfg_file = _write_cfg(tmp_path)          # эталон dirty — значение из файла
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen(config_path=str(cfg_file)))
        await pilot.pause()
        await pilot.press("enter")                     # секция «Подключение»
        await pilot.pause()
        scr = app.screen
        title = scr.query_one("#set-title", Static)
        assert "изменено" not in str(title.render())
        scr.query_one("#conn-url", Input).value = "https://new-host/v1/chat/completions"
        await pilot.pause()
        assert "изменено" in str(title.render())
        scr.query_one("#conn-url", Input).value = "http://host/v1/chat/completions"   # вернули исходное
        await pilot.pause()
        assert "изменено" not in str(title.render())


async def test_model_dirty_state_toggles_on_move(tmp_path, monkeypatch):
    """T7 (переписан волной B): перемещение выбора модели делает секцию «изменённой»,
    возврат к исходной — снимает. Сверка идёт с ФАЙЛОМ и решает, есть ли что писать
    («ничего не изменено» на Enter) — она и осталась.

    Маркер «изменено» у заголовка исчез сознательно: он — обещание submit-цикла, а
    секция «Модели» из save-машины вышла (решение 2 волны B) и пишет реестр
    немедленно. Обещать в одной секции два ритма записи нельзя, поэтому сторож
    читает состояние, а не заголовок."""
    import model_picker
    from model_picker import ModelInfo
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    models = [ModelInfo(id="test-model"), ModelInfo(id="other-model")]
    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **kw: models)
    cfg_file = _write_cfg(tmp_path)          # эталон dirty — модель из файла
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Модели")
        await pilot.pause()
        title = scr.query_one("#set-title", Static)
        assert scr._is_dirty(1) is False                # исходный выбор == модель конфига
        await pilot.press("down")                              # → other-model
        await pilot.pause()
        assert scr._is_dirty(1) is True
        await pilot.press("up")                                # обратно → test-model
        await pilot.pause()
        assert scr._is_dirty(1) is False
        assert "изменено" not in str(title.render())    # маркер submit-цикла здесь молчит


async def test_ctrl_space_in_readonly_section_does_not_crash():
    """C1: ручная проверка в read-only секции (пакетный шаблон) не должна ронять приложение —
    редактора нет, значит и проверять нечего."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from config import _default_config_path
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=_default_config_path(),
                             on_apply=lambda: None,
                             probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")         # открыть секцию «Адрес» (read-only)
        await pilot.pause()
        assert scr._view == 0
        await pilot.press("ctrl+space")    # попытка проверки — не должна крашить
        await pilot.pause()
        assert app.screen is scr           # приложение живо


def test_update_dirty_does_not_raise_when_view_is_menu():
    """I2: колбэк on_loaded (ModelEditor fetch) может прилететь, когда _view уже
    "menu" (Esc в полёте fetch) — SECTIONS[self._view] с индексом-строкой падал TypeError."""
    scr = _mk_screen()
    scr._view = "menu"
    scr._update_dirty()                    # не должно бросать


async def test_model_dirty_state_no_false_negative_when_active_not_first(tmp_path, monkeypatch):
    """T9: active-модель конфига НЕ первая в списке с сервера. Initial-курсор встаёт
    на неё (preselect) → сразу после async-загрузки «изменено» НЕ появляется (без
    этого — ложноотрицательный dirty из T7: курсор молча стоял на первой строке).
    Сдвиг курсора делает секцию изменённой, возврат на active — снимает.

    Читаем состояние, а не заголовок: маркер у секции «Модели» снят вместе с
    submit-циклом (см. соседний тест)."""
    import model_picker
    from model_picker import ModelInfo
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    models = [ModelInfo(id="other-model"), ModelInfo(id="test-model")]   # active — вторая
    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **kw: models)
    cfg_file = _write_cfg(tmp_path)          # эталон dirty — модель из файла
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Модели")
        await pilot.pause()                                # worker fetch отработал
        assert scr._is_dirty(1) is False                    # курсор сразу на active — не dirty
        await pilot.press("up")                             # → other-model
        await pilot.pause()
        assert scr._is_dirty(1) is True
        await pilot.press("down")                           # обратно → test-model (active)
        await pilot.pause()
        assert scr._is_dirty(1) is False


async def test_model_dirty_state_manual_entry_reacts_to_typing(tmp_path, monkeypatch):
    """M4: секция «Модели» без сохранённого значения, fetch=[] → нет active-фолбэка,
    редактор — голый ручной Input. Набранное имя обязано читаться как изменение —
    иначе Enter ответил бы «ничего не изменено» и не записал ничего."""
    import model_picker
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from textual.widgets import Input
    cfg = {"completions_url": "http://host/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer secret-token"},
           "model": ""}
    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **kw: [])
    cfg_file = _write_cfg(tmp_path, model='""')          # модель не задана — в файле тоже
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=cfg, config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Модели")
        await pilot.pause()                                # worker fetch=[] отработал
        assert scr._is_dirty(1) is False
        scr.query_one(Input).value = "new-model"
        await pilot.pause()
        assert scr._is_dirty(1) is True


async def test_draft_probe_no_auth_401_shows_requires_auth_hint(tmp_path):
    """M6: draft-probe без авторизации (auth=None) + сервер вернул 401 → текст
    «сервер требует авторизацию», а не «токен отклонён» (токена и не было)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tui.editors.connection import ConnectionEditor
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        "  completions_url: \"http://host/v1/chat/completions\"\n"
        "  chat_completion_headers:\n"
        "    Authorization: \"Bearer old\"\n",
        encoding="utf-8")
    cfg = {
        "completions_url": "http://host/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer old"},
        "model": "m",
    }
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=cfg, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=_fake_probe(_probe_rejected()))
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()               # свернуть пресет-лист (открыт по умолчанию на адресе)
        ed.move(2)                        # адрес → токен → галка «без авторизации»
        ed.toggle_no_auth()               # ● без авторизации
        scr._start_draft_probe(0)
        await pilot.pause(); await pilot.pause()
        probe_line = str(scr.query_one(".set-probe", Static).render())
        assert "требует авторизацию" in probe_line
        assert "токен отклонён" not in probe_line


async def test_auth_draft_probe_server_ignores_token(tmp_path):
    """Differential: сервер отдаёт и БЕЗ токена (control ok) → «авторизация не требуется»,
    а не ложное «токен принят». Контрольный запрос уходит с auth=None."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        "  completions_url: \"http://host/v1/chat/completions\"\n"
        "  chat_completion_headers:\n"
        "    Authorization: \"Bearer whatever\"\n",
        encoding="utf-8")
    cfg = {
        "completions_url": "http://host/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer whatever"},
        "model": "m",
    }
    captured = []

    def probe_fn(endpoint, auth, verify, model, **kw):
        captured.append(auth)
        return _probe_ok()                                # сервер всегда отдаёт (auth игнорит)

    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=cfg, config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=probe_fn)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")            # токен в поле
        from tests.tui.conftest import wait_until
        # Ждём ОСАДКИ автопробы входа по её собственному следу — вердикту секции
        # (`_draft_checked`, канон `settings_connection`), а не по появлению
        # контрольного auth=None в журнале: такой запрос шлёт и config-probe хаба,
        # когда догоняет вердикт о токене, — и ожидание кончалось бы ДО того, как
        # поля редактора смонтированы.
        await wait_until(pilot, lambda: scr._draft_checked is not None, "auto-probe on entry")
        captured.clear()             # отбросить config-probe маунта и авто-probe входа
        scr._start_draft_probe(0)                         # ручная проверка
        await pilot.pause(); await pilot.pause()
        assert captured == [None]                          # только контроль без токена (control ok → без 2-го)
        probe_line = str(scr.query_one(".set-probe", Static).render())
        assert "авторизация не требуется" in probe_line
        assert "токен принят" not in probe_line


# Таблица слов probe-строки «Подключения» переехала в юниты владельца формата
# (`tests/tui/test_probe_presentation.py`): строка теперь общая для секции, формы
# записи модели и мастера, и держать её словарь в тестах одного из потребителей
# значит проверять два раза одно и расходиться при первой правке.


async def test_auth_section_saved_token_is_not_dirty(tmp_path):
    """Регресс: токен уже сохранён → при входе в секцию заголовок НЕ «изменено»
    (dirty сравнивался с "" вместо исходного токена в поле)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        "  completions_url: \"http://host/v1/chat/completions\"\n"
        "  chat_completion_headers:\n"
        "    Authorization: \"Bearer secret-token\"\n",
        encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        title = scr.query_one("#set-title", Static)
        assert "изменено" not in str(title.render())     # токен в поле == исходный
        assert scr._is_dirty(0) is False


async def test_auth_section_repeated_enter_no_change_does_not_crash(tmp_path):
    """Регресс DuplicateIds: повторный Enter без изменений роняло приложение
    (notice монтировался с фикс. id, remove() в Textual асинхронен). Теперь
    реплика — в слоте заголовка, повтор безопасен."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        "  completions_url: \"http://host/v1/chat/completions\"\n"
        "  chat_completion_headers:\n"
        "    Authorization: \"Bearer secret-token\"\n",
        encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        for _ in range(3):
            await pilot.press("enter")                    # ничего не введено/не менялось
            await pilot.pause()
        assert app.screen is scr                          # приложение живо, без DuplicateIds
        title = str(scr.query_one("#set-title", Static).render())
        assert "ничего не изменено" in title


async def test_title_note_clears_to_true_state(tmp_path):
    """title-note «ничего не изменено» — временный; автогашение возвращает истинное
    состояние заголовка (имя секции без «изменено», раз ничего не менялось)."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n  completions_url: \"http://host/v1/chat/completions\"\n",
        encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                       on_apply=lambda: None,
                                       probe_fn=_fake_probe(_probe_ok())))
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")                        # секция «Адрес»
        await pilot.press("enter")                        # без изменений → title-note
        await pilot.pause()
        title = scr.query_one("#set-title", Static)
        assert "ничего не изменено" in str(title.render())
        assert scr._note_timer is not None                # таймер автогашения взведён
        scr._clear_title_note()                           # эмуляция срабатывания таймера
        assert "ничего не изменено" not in str(title.render())
        assert "изменено" not in str(title.render())      # ничего не менялось → чистое имя
        assert scr._note_timer is None


async def test_draft_probe_worker_exception_resolves_as_unknown(tmp_path, monkeypatch):
    """M7: исключение внутри probe_fn не должно оставлять _probe_in_flight/checked
    зависшими — воркер доставляет «unknown»-результат вместо падения молча."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://old/v1\"\n", encoding="utf-8")

    def _boom(*a, **kw):
        raise RuntimeError("network stack exploded")

    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_boom)
        app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")                       # секция «Адрес»
        await pilot.pause()
        await pilot.press("ctrl+space")
        await pilot.pause(); await pilot.pause()
        assert scr._draft_checked is not None
        assert scr._draft_checked.verdict == "unknown"


async def test_update_menu_glyph_arrow_when_available(tmp_path, monkeypatch):
    """Меню «Обновление» ещё не открывали (_update_check_result is None) — глиф
    зеркалит персист-флаг app._update_available, найденный авто-чеком при старте."""
    import app_version, tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        app._update_available = True
        from tui.screens.settings import _UPDATE
        glyph, _ = scr._menu_glyph(_UPDATE)      # индекс секции, а не литерал: состав меню растёт
        assert glyph == "↑"


async def test_delete_key_in_mcp_section_reaches_screen(tmp_path, monkeypatch):
    """Del в секции MCP поднимает confirm удаления (§5.3: MCP переведён с Ctrl+D на Del,
    та же клавиша, что в голосовой секции).

    Прогон на настоящем `mcp_runtime.cli`, а не на облегчённом `settings_mcp_section`:
    статусы пакетов приходят фоновым воркером, как в реальном приложении.
    """
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tui.editors.mcp_section import McpSectionEditor
    import mcp_runtime.cli as cli
    monkeypatch.setattr(cli, "installable_names", lambda config=None: ["office"])
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office"])
    monkeypatch.setattr(cli, "manual_server_names", lambda uc: [])
    monkeypatch.setattr(cli, "load_manifest_entries", lambda config=None: {})
    monkeypatch.setattr(cli, "installed_state",
                        lambda n, config=None, manifest=None: {"installed": True, "version": "0.3.1",
                                                              "update": None})
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(tmp_path / "c.yml"),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down", "down", "down", "enter")   # секция MCP (idx=3)
        await pilot.pause()
        ed = scr.query_one(McpSectionEditor)
        for _ in range(50):                                  # статусы пакетов грузит фоновый воркер
            st = ed._states.get("office")
            if st is not None and st.installed:
                break
            await pilot.pause()
        assert ed._states["office"].installed is True        # предусловие: пакет есть, есть что удалять

        await pilot.press("delete")
        await pilot.pause()
        assert ed.awaiting_confirm is True                   # клавиша дошла до экрана
        assert isinstance(app.screen, SettingsScreen)        # экран настроек не покинут


# --- ссылки на переменные окружения ${ENV:VAR} (настоящий файл + настоящий os.environ) ---


async def _open_settings(app, pilot, cfg_file, cfg):
    from tui.screens.settings import SettingsScreen
    scr = SettingsScreen(config=cfg, config_path=str(cfg_file), on_apply=lambda: None,
                         probe_fn=_fake_probe(_probe_ok()))
    app.push_screen(scr)
    await pilot.pause()
    return scr


async def test_env_ref_token_survives_save_without_changes(tmp_path, monkeypatch):
    """Ссылка + переменная задана: Enter без правок НЕ подменяет ссылку резолвнутым
    секретом. Загруженный конфиг уже резолвнут — писать его обратно в файл нельзя."""
    monkeypatch.setenv("GC_API_KEY", "sk-REALSECRET123")
    cfg_file = _write_cfg(tmp_path, auth='"${ENV:GC_API_KEY}"')
    original = cfg_file.read_text(encoding="utf-8")
    cfg = _loaded(cfg_file)
    assert cfg["chat_completion_headers"]["Authorization"] == "sk-REALSECRET123"   # резолв сработал
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        assert scr._is_dirty(0) is False                      # ничего не трогали
        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        content = cfg_file.read_text(encoding="utf-8")
        assert "sk-REALSECRET123" not in content              # секрет не утёк в файл
        assert content == original                            # ссылка цела, файл не тронут


async def test_env_ref_missing_var_not_shown_as_token_set(tmp_path, monkeypatch):
    """Рекомендованная форма «Bearer ${ENV:VAR}» + переменной НЕТ: резолв даёт "Bearer " →
    маска показывала *** («токен задан»), а Enter дописывал второй Bearer."""
    monkeypatch.delenv("GC_API_KEY", raising=False)
    cfg_file = _write_cfg(tmp_path, auth='"Bearer ${ENV:GC_API_KEY}"')
    original = cfg_file.read_text(encoding="utf-8")
    cfg = _loaded(cfg_file)
    assert cfg["chat_completion_headers"]["Authorization"] == "Bearer "   # резолв в пустоту
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        row = next(r for r in _menu_lines(scr) if "Подключение" in r)
        assert "${ENV:GC_API_KEY}" in row                     # показана ссылка…
        assert "переменной нет" in row                        # …и её честный статус
        assert "***" not in row                               # не «токен задан»
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        content = cfg_file.read_text(encoding="utf-8")
        assert "Bearer Bearer" not in content
        assert content == original


async def test_env_ref_missing_var_is_not_reported_as_no_auth(tmp_path, monkeypatch):
    """Ссылка без схемы + переменной нет: резолв даёт "" → экран говорил «без авторизации»,
    хотя в файле живая ссылка. Должен сказать «переменной нет»."""
    monkeypatch.delenv("GC_API_KEY", raising=False)
    cfg_file = _write_cfg(tmp_path, auth='"${ENV:GC_API_KEY}"')
    cfg = _loaded(cfg_file)
    assert cfg["chat_completion_headers"]["Authorization"] == ""
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        row = next(r for r in _menu_lines(scr) if "Подключение" in r)
        assert "${ENV:GC_API_KEY}" in row and "переменной нет" in row
        assert "без авторизации" not in row


async def test_literal_over_env_ref_needs_confirm_then_writes(tmp_path, monkeypatch):
    """Осознанный литерал поверх ссылки: сначала confirm (с именем переменной),
    и только по второму Enter — запись. Молчаливой перезаписи нет."""
    monkeypatch.setenv("GC_API_KEY", "sk-REALSECRET123")
    cfg_file = _write_cfg(tmp_path, auth='"${ENV:GC_API_KEY}"')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    from textual.widgets import Input
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        await pilot.press("down")                             # курсор → токен
        await pilot.pause()
        scr.query_one("#conn-token", Input).value = "sk-typed-by-hand"
        await pilot.press("enter")                            # probe → запись → confirm
        await pilot.pause(); await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        assert "GC_API_KEY" in banner                         # баннер назвал переменную
        assert "sk-typed-by-hand" not in cfg_file.read_text(encoding="utf-8")   # ещё не записано
        assert scr._view == settings_section_index("Подключение")   # остались в секции
        await pilot.press("enter")                            # подтверждение
        await pilot.pause(); await pilot.pause()
        content = cfg_file.read_text(encoding="utf-8")
        assert 'Authorization: "Bearer sk-typed-by-hand"' in content
        assert "${ENV:GC_API_KEY}" not in content


async def test_literal_token_edit_saves_without_confirm(tmp_path):
    """Регресс: обычный литеральный токен (без ссылки) правится и сохраняется как раньше."""
    cfg_file = _write_cfg(tmp_path, auth='"Bearer old-token"')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    from textual.widgets import Input
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        await pilot.press("down")                             # курсор → токен
        await pilot.pause()
        scr.query_one("#conn-token", Input).value = "sk-new"
        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        assert not scr.query(".set-confirm")                  # ссылки не было — подтверждать нечего
        content = cfg_file.read_text(encoding="utf-8")
        assert 'Authorization: "Bearer sk-new"' in content
        assert scr._view == "menu"                            # сохранено, возврат в хаб


async def test_bearer_without_token_hints_instead_of_nothing_changed(tmp_path):
    """Действует «без авторизации»; выбрали Bearer (курсор на нём), токен не ввели, Enter →
    у заголовка подсказка «введите токен», а не молчаливое «ничего не изменено»; файл цел."""
    cfg_file = _write_cfg(tmp_path, auth='""')            # действует «без авторизации»
    original = cfg_file.read_text(encoding="utf-8")
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    from textual.widgets import Static
    from tui.editors.connection import ConnectionEditor
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()               # свернуть пресет-лист (открыт по умолчанию на адресе)
        ed.move(2)                         # курсор → галка «без авторизации» (единств. вариант — Bearer)
        ed.toggle_no_auth()                # снять галку → выбран Bearer, токен НЕ введён
        await pilot.pause()
        await pilot.press("enter")                        # попытка сохранить
        await pilot.pause()
        title = str(scr.query_one("#set-title", Static).render())
        assert "введите токен" in title
        assert "ничего не изменено" not in title
        assert cfg_file.read_text(encoding="utf-8") == original   # ничего не записано


async def test_env_ref_endpoint_shown_as_ref_and_not_rewritten(tmp_path, monkeypatch):
    """Та же механика у адреса (секрета нет, болезнь та же): экран показывает ссылку,
    а не резолвнутый литерал, и сохранение без правок файл не трогает."""
    monkeypatch.setenv("GC_URL", "http://real-host/v1/chat/completions")
    cfg_file = _write_cfg(tmp_path, url='"${ENV:GC_URL}"')
    original = cfg_file.read_text(encoding="utf-8")
    cfg = _loaded(cfg_file)
    assert cfg["completions_url"] == "http://real-host/v1/chat/completions"
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        row = next(r for r in _menu_lines(scr) if "Подключение" in r)
        assert "${ENV:GC_URL}" in row
        assert "real-host" not in row                         # резолвнутое значение не показываем
        await pilot.press("enter")                            # секция «Подключение»
        await pilot.pause()
        await pilot.press("enter")                            # сохранить без правок
        await pilot.pause(); await pilot.pause()
        assert cfg_file.read_text(encoding="utf-8") == original


async def test_save_warns_that_session_counters_are_reset(tmp_path, monkeypatch):
    """Сохранение при живой сессии честно предупреждает об обнулении счётчиков.

    Счётчики токенов/стоимости живут в executor'е, а _on_apply (reload_config) его
    пересобирает — цифры в футере начинаются с нуля. Раньше они молча исчезали.
    """
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from textual.widgets import Static

    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('agent:\n  model: "old-model"\n', encoding="utf-8")

    class _Runner:                      # сессия была, но сейчас не бежит
        def is_running(self):
            return False

    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.active_runner = _Runner()   # как после завершённого запроса
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()

        scr._do_save(2, {"model": "new-model"})     # секция «Модель»
        await pilot.pause()

        flash = " ".join(str(w.render()) for w in scr.query(Static))
        assert "Сохранено" in flash
        assert "Счётчики токенов сессии обнулены" in flash, f"нет предупреждения: {flash!r}"


# --- перекрытие оверлеем: /settings правит базовый файл, а действует оверлей ---


@pytest.fixture
def no_managed_overlay(tmp_path, monkeypatch):
    """Иначе тесты читают НАСТОЯЩИЙ ~/.gc/config.local.yml разработчика: на машине, где
    managed-оверлей задаёт agent.model, «перекрытие» появится из ниоткуда (или наоборот).
    Тем же приёмом, что и в tests/test_config_origin.py."""
    monkeypatch.setattr(
        "mcp_runtime.paths.managed_config_path",
        lambda: tmp_path / "no-managed-overlay-here.yml",
    )


def _screen_text(scr):
    """Весь видимый текст экрана (подписи мы ищем среди Static)."""
    return " ".join(str(w.render()) for w in scr.query(Static))


def _models_fetch(monkeypatch, *ids):
    import model_picker
    from model_picker import ModelInfo
    monkeypatch.setattr(model_picker, "fetch_models",
                        lambda *a, **kw: [ModelInfo(id=i) for i in ids])


async def _open_model_section(app, pilot, cfg_file, cfg):
    from tests.tui.conftest import wait_until
    from tui.editors.model import ModelEditor
    scr = await _open_settings(app, pilot, cfg_file, cfg)
    open_settings_section(scr, "Модели")
    await pilot.pause()
    await wait_until(pilot, lambda: scr.query_one(ModelEditor).models_loaded, "models loaded")
    return scr


def _project_overlay(tmp_path, monkeypatch, text="agent:\n  model: proj-model\n"):
    proj = tmp_path / "proj"
    (proj / ".gc").mkdir(parents=True)
    (proj / ".gc" / "config.yml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(proj)
    return proj


async def test_overridden_field_shows_effective_value_and_source(tmp_path, monkeypatch, no_managed_overlay):
    """Поле перекрыто project-оверлеем → в поле значение файла, в подписи — действующее."""
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path, model='"base-model"')
    proj = _project_overlay(tmp_path, monkeypatch)
    _models_fetch(monkeypatch, "base-model", "other-model")
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_model_section(app, pilot, cfg_file, cfg)
        text = _screen_text(scr)
        assert "proj-model" in text                       # действующее — видно
        assert "перекрыто" in text
        assert ".gc/config.yml в проекте" in text
        assert proj.exists()


async def test_saving_overridden_field_warns_it_will_not_apply(tmp_path, monkeypatch, no_managed_overlay):
    """Сохранение перекрытого поля: записано в базовый файл, но не действует — сказать честно."""
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path, model='"base-model"')
    proj = _project_overlay(tmp_path, monkeypatch)
    _models_fetch(monkeypatch, "base-model", "other-model")
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_model_section(app, pilot, cfg_file, cfg)
        await pilot.press("down")                              # выбрать другую модель в списке
        await pilot.press("enter")           # сохранить
        await pilot.pause(); await pilot.pause()
        text = _screen_text(scr)
        assert "не действует" in text
        assert "перекрыто" in text
        assert "Модели" in text          # какое поле перекрыто — флеш виден уже на хабе
    assert "other-model" in cfg_file.read_text(encoding="utf-8")   # запись ушла в БАЗОВЫЙ файл
    # оверлей не тронут
    assert "proj-model" in (proj / ".gc" / "config.yml").read_text(encoding="utf-8")


async def test_plain_value_has_no_origin_hint(tmp_path, monkeypatch, no_managed_overlay):
    """Ничего не перекрыто, ссылок нет → подписи нет: экран не шумит."""
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path, model='"base-model"')
    monkeypatch.chdir(tmp_path)          # нет .gc/ → проектного оверлея нет
    _models_fetch(monkeypatch, "base-model", "other-model")
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_model_section(app, pilot, cfg_file, cfg)
        text = _screen_text(scr)
        assert "перекрыто" not in text
        assert "переменная подставилась" not in text


async def test_auth_override_shows_masked_effective_value(tmp_path, monkeypatch, no_managed_overlay):
    """Секция «Авторизация» перекрыта оверлеем → подпись есть, но секрет замаскирован
    (баг: подписи для секции 1 не было вовсе — экран молчал о перекрытии токена)."""
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path, auth='"Bearer base-token"')
    _project_overlay(
        tmp_path, monkeypatch,
        text='backend:\n  chat_completion_headers:\n    Authorization: "Bearer proj-token"\n')
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        text = _screen_text(scr)
        assert "перекрыто" in text
        assert ".gc/config.yml в проекте" in text
        assert "proj-token" not in text                    # секрет не открытым текстом


async def test_env_ref_and_override_hints_stack(tmp_path, monkeypatch, no_managed_overlay):
    """В файле ссылка ${ENV:VAR} И поле перекрыто оверлеем → складываются обе строки."""
    import tui.app as app_mod
    monkeypatch.setenv("GC_MODEL", "resolved-model")
    cfg_file = _write_cfg(tmp_path, model='"${ENV:GC_MODEL}"')
    _project_overlay(tmp_path, monkeypatch, text="agent:\n  model: proj-model\n")
    _models_fetch(monkeypatch, "resolved-model", "proj-model")
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_model_section(app, pilot, cfg_file, cfg)
        text = _screen_text(scr)
        assert "переменная подставилась" in text
        assert "перекрыто" in text


async def test_model_picker_marks_env_ref_as_env_not_missing(tmp_path, monkeypatch, no_managed_overlay):
    """model: ${ENV:VAR} — это ссылка, а не «модель пропала с сервера». Так и пометить."""
    import tui.app as app_mod
    monkeypatch.setenv("GC_MODEL", "resolved-model")
    cfg_file = _write_cfg(tmp_path, model='"${ENV:GC_MODEL}"')
    monkeypatch.chdir(tmp_path)
    _models_fetch(monkeypatch, "other-model")           # сервер не возвращает resolved-model
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_model_section(app, pilot, cfg_file, cfg)
        text = _screen_text(scr)
        assert "из переменной окружения" in text
        assert "нет на сервере" not in text


async def test_overridden_save_keeps_warn_detail(tmp_path, monkeypatch, no_managed_overlay):
    """overridden-ветка _do_save раньше обрывалась до append(warn_detail) — предупреждение
    о непройденной probe-проверке молча терялось. Оба сообщения должны дойти."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = _write_cfg(tmp_path, model='"base-model"')
    _project_overlay(tmp_path, monkeypatch)
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=cfg, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        scr._do_save(2, {"model": "other-model"},
                     warn_detail="проверить не удалось — сервер недоступен (Tab — повторить)")
        await pilot.pause()
        text = _screen_text(scr)
        assert "перекрыто" in text
        assert "проверить не удалось" in text


async def test_overridden_save_still_reports_reset_counters(tmp_path, monkeypatch,
                                                            no_managed_overlay):
    """Перекрытие поля не отменяет побочных последствий сохранения: _on_apply пересобрал
    конфиг и обнулил счётчики сессии — сказать надо и об этом, а не одно вместо другого."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    cfg_file = _write_cfg(tmp_path, model='"base-model"')
    _project_overlay(tmp_path, monkeypatch)
    cfg = _loaded(cfg_file)

    class _Runner:                      # сессия была, но сейчас не бежит
        def is_running(self):
            return False

    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        app.active_runner = _Runner()
        scr = SettingsScreen(config=cfg, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        scr._do_save(2, {"model": "other-model"})
        await pilot.pause()
        text = _screen_text(scr)
        assert "перекрыто" in text
        assert "счётчики токенов сессии обнулены" in text, f"потеряно предупреждение: {text!r}"


async def test_non_string_auth_in_overlay_does_not_crash_section(tmp_path, monkeypatch,
                                                                 no_managed_overlay):
    """`Authorization: 12345` в оверлее — валидный YAML: маскирование не должно ронять
    монтирование секции (значение приходит из чужого файла, его тип не гарантирован)."""
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path, auth='"Bearer base-token"')
    _project_overlay(
        tmp_path, monkeypatch,
        text="backend:\n  chat_completion_headers:\n    Authorization: 12345\n")
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        text = _screen_text(scr)
        assert "перекрыто" in text
        assert "12345" not in text                      # секрет (пусть и числом) — под маской


async def test_overridden_model_row_is_not_called_current(tmp_path, monkeypatch,
                                                          no_managed_overlay):
    """Модель из файла перекрыта → в списке она «в файле», а не «текущая»: экран не может
    одновременно утверждать, что действует она и что действует оверлей."""
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path, model='"base-model"')
    _project_overlay(tmp_path, monkeypatch)
    _models_fetch(monkeypatch, "base-model", "other-model")
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_model_section(app, pilot, cfg_file, cfg)
        rows = [str(w.render()) for w in scr.query(".onb-option")]
        assert any("base-model" in r and "в файле" in r for r in rows), rows
        assert not any("текущая" in r for r in rows)


async def test_save_without_session_does_not_warn_about_counters(tmp_path):
    """Без сессии терять нечего — лишнего предупреждения быть не должно."""
    import tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from textual.widgets import Static

    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('agent:\n  model: "old-model"\n', encoding="utf-8")

    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.active_runner = None                   # сессии не было
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()

        scr._do_save(2, {"model": "new-model"})
        await pilot.pause()

        flash = " ".join(str(w.render()) for w in scr.query(Static))
        assert "Сохранено" in flash
        assert "счётчики" not in flash


def test_sections_composition_merged():
    """7 секций: «Подключение» первая, «Профили» сразу за «Моделями», «Голосовой
    ввод» между MCP и «Режимами работы», «Режимы работы» перед «Обновлением».

    Волна B: легаси-«Модель» поглощена секцией «Модели», и та вышла из save-машины
    — редакторная теперь одна (`{conn}`), probe — тоже (`{conn}`). Гейт записи
    пакетного шаблона при этом шире редакторных: «Модели» тоже пишут в config.yml."""
    from tui.screens import settings as s
    assert s.SECTIONS == ("Подключение", "Модели", "Профили", "MCP-серверы",
                          "Голосовой ввод", "Режимы работы", "Обновление")
    assert (s._CONNECTION == 0 and s._MODELS == 1 and s._PROFILES == 2 and s._MCP == 3
            and s._VOICE == 4 and s._MODES == 5 and s._UPDATE == 6)
    assert s._EDITOR_SECTIONS == {s._CONNECTION}
    assert s._PROBE_SECTIONS == {s._CONNECTION}
    assert s._WRITE_GATED_SECTIONS == {s._CONNECTION, s._MODELS}


def test_sections_order_contains_modes():
    from tui.screens.settings import SECTIONS
    assert SECTIONS == ("Подключение", "Модели", "Профили", "MCP-серверы",
                        "Голосовой ввод", "Режимы работы", "Обновление")


def test_fit_mode_pairs_whole_pairs_only():
    from tui.screens.settings import fit_mode_pairs
    pairs = ["самопроверка выполнения задачи: off", "формат ответа: allow"]
    full = fit_mode_pairs(pairs, width=200)
    assert full == "самопроверка выполнения задачи: off · формат ответа: allow"
    tight = fit_mode_pairs(pairs, width=40)
    assert tight == "самопроверка выполнения задачи: off · …"
    assert "формат" not in tight                      # пара не режется посередине
    tiny = fit_mode_pairs(pairs, width=10)
    assert tiny == "…"                                # не влезла ни одна пара


async def test_modes_section_preview_shows_toggle_pairs():
    """Превью строки «Режимы работы» в меню — пары label: value в порядке TOGGLES
    (первая пара — «gate», всегда целиком; вторая — целиком либо усечена в `· …`,
    смотря по фактической ширине региона в тестовом терминале)."""
    import tui.app as app_mod
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        rows = _menu_lines(app.screen)
        modes_row = next(r for r in rows if "Режимы работы" in r)
        assert "самопроверка выполнения задачи: smart" in modes_row
        assert ("формат ответа: judge" in modes_row) or ("· …" in modes_row)


async def test_modes_preview_truncated_on_first_frame_of_narrow_menu(tmp_path):
    """Carry-fix ревью T2 (Low): `_modes_preview_width` не должен читать регион СВОЕЙ ЖЕ
    ещё не смонтированной строки — превью «Режимы работы» усечено парами (`…`) уже на
    первом кадре узкого терминала, без единого нажатия клавиши. Без фикса ширина региона
    неизвестна (0) на первом полном ре-рендере меню → fit_mode_pairs не усекает вовсе."""
    import tui.app as app_mod
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "judge"\n',
                        encoding="utf-8")
    app = app_mod.TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()
    async with app.run_test(size=(60, 24)) as pilot:
        app.push_screen(_mk_screen(config=app._config, config_path=str(cfg_file)))
        await pilot.pause()
        rows = _menu_lines(app.screen)
        modes_row = next(r for r in rows if "Режимы работы" in r)
        assert "…" in modes_row


async def test_modes_subscreen_writes_global(tmp_path):
    """Подэкран «Режимы работы»: строка-пресет первая (head «rigor · строгость
    завершения хода», без слеша). Ревизия 7 (flat-группы, спека шапка/§4.2):
    дети рисуются БЕЗ ведущего маркера «· » и БЕЗ отступа — «gate ·
    самопроверка выполнения задачи», «answers · формат ответа» — одним левым
    краем с родителем (иерархия только цветом, см. стилевой тест в
    test_rigor_acceptance.py). Крутка пресета пишет ПАРУ; крутка детальной —
    один ключ (глобал)."""
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "deny"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        assert not scr.query(".set-modes-save")

        # курсор 0 = пресет rigor (high: on+deny). → пишет ПАРУ следующего пресета.
        rows = [str(w.render()) for w in scr.query(".set-row")]
        assert any("rigor · строгость завершения хода" in r for r in rows)
        assert not any("/rigor" in r for r in rows)
        assert not any("├" in r or "└" in r for r in rows)
        assert not any(r[2:].startswith("· ") for r in rows)   # ревизия 7: маркер убран у всех строк
        assert any(r[2:].startswith("gate · самопроверка выполнения задачи") for r in rows)
        assert any(r[2:].startswith("answers · формат ответа") for r in rows)

        await pilot.press("right")       # high -> low (cycle_value по low/medium/high)
        await pilot.pause(0.1)
        text = cfg_file.read_text(encoding="utf-8")
        # пара low: off + allow — обе записаны
        assert 'finish_gate_mode: "off"' in text and 'text_answer_mode: "allow"' in text

        # спуск на детальную gate, крутка пишет ОДИН ключ
        await pilot.press("down")
        await pilot.pause(0.1)
        await pilot.press("right")       # off -> smart
        await pilot.pause(0.1)
        assert 'finish_gate_mode: "smart"' in cfg_file.read_text(encoding="utf-8")


async def test_modes_apply_error_flash_keeps_screen(tmp_path, monkeypatch):
    """Писатель падает (OSError) → флеш «✗ не сохранено: …», подэкран не
    закрывается, config.yml не меняется."""
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')
    original = cfg_file.read_text(encoding="utf-8")

    def boom(path, values, **kw):
        raise OSError("disk full")
    monkeypatch.setattr("onboarding_config_writer.write_values", boom)

    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        await pilot.press("right")
        await pilot.pause(0.1)
        assert scr._view == settings_section_index("Режимы работы")
        assert cfg_file.read_text(encoding="utf-8") == original
        notice = " ".join(str(w.render()) for w in scr.query(Static))
        assert "не сохранено" in notice


async def test_modes_footer_shows_value_hint(tmp_path):
    """Hint выделенного (внутри рамки, без клавиш) меняется при смене выделения.
    Курсор 0 = /rigor; спускаемся на детальные gate/answers."""
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "deny"\n')  # high -> пресет-строка
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        footer = str(scr.query_one("#set-footer", Static).render())
        assert footer == ("↑/↓ — выбор · ←/→/Enter — переключить · "
                           "PgUp/PgDn — секции · Esc — меню")
        hint0 = str(scr.query_one(".set-modes-hint", Static).render())
        assert hint0.startswith("high —")          # строка пресета, значение high
        assert "Enter" not in hint0

        await pilot.press("down")                    # -> gate (самопроверка), значение on
        await pilot.pause(0.1)
        hint1 = str(scr.query_one(".set-modes-hint", Static).render())
        assert hint1.startswith("on —")
        assert hint1 != hint0

        await pilot.press("down")                    # -> answers (формат), значение deny
        await pilot.pause(0.1)
        hint2 = str(scr.query_one(".set-modes-hint", Static).render())
        assert hint2.startswith("deny —")
        assert hint2 != hint1


async def test_modes_preset_row_shows_custom_when_global_nonstandard(tmp_path):
    """Глобал нестандартный (on+allow) → строка-пресет резолвится в custom, трек
    отращивает 4-ю позицию (спека §2, §4.2)."""
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "allow"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        rows = [str(w.render()) for w in scr.query(".set-row")]
        rigor_row = next(r for r in rows if "rigor ·" in r)
        assert rigor_row.rstrip().endswith("custom")
        assert "  ●" in rigor_row or "  ○" in rigor_row  # 4-я вне-шкальная позиция


async def test_modes_preset_row_shows_session_override_tail(tmp_path):
    """Хвост «· в текущей сессии: X» на строке пресета (§6.2): X = резолв сессионной
    пары. Глобал medium, сессия -> high."""
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n')
    app.tui_state.active_task_id = "t1"
    app._threads_dir = str(tmp_path / "threads")
    app.apply_session_overrides({"tui.finish_gate_mode": "on", "tui.text_answer_mode": "deny"})
    async with app.run_test(size=(200, 24)) as pilot:
        scr = await _open_modes_section(pilot)
        rows = [str(w.render()) for w in scr.query(".set-row")]
        rigor_row = next(r for r in rows if "rigor ·" in r)
        assert "в текущей сессии: high" in rigor_row


async def test_modes_preset_track_width_reserved_regardless_of_global(tmp_path):
    """Ревизия 4 (спека §4.2): 4-я позиция трека пресета зарезервирована ВСЕГДА
    (`extra_lit=False` -> `render_track` сам добавляет +3 пустых символа) —
    колонка значения строки-пресета не сдвигается между стандартным глобалом
    (medium) и custom (on+allow). Без фикса `_modes_columns`/`_mode_row_text`
    считают `extra=None` для не-custom -> трек короче на 3 символа -> значение
    "medium" стоит на 3 позиции левее, чем "custom"."""
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    app_std, _ = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n')
    app_custom, _ = _app_with_config(
        custom_dir, 'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "allow"\n')
    async with app_std.run_test() as pilot_std:
        scr_std = await _open_modes_section(pilot_std)
        rows_std = [str(w.render()) for w in scr_std.query(".set-row")]
        row_std = next(r for r in rows_std if "rigor ·" in r)
    async with app_custom.run_test() as pilot_custom:
        scr_custom = await _open_modes_section(pilot_custom)
        rows_custom = [str(w.render()) for w in scr_custom.query(".set-row")]
        row_custom = next(r for r in rows_custom if "rigor ·" in r)
    assert row_std.index("medium") == row_custom.index("custom")


async def test_modes_rows_track_base_left_aligned(tmp_path):
    """Ревизия 5 (спека §4.2/§6, тот же контракт, что и в палитре): базовый
    трек ВСЕХ строк секции (rigor и дети) начинается в ОДНОЙ колонке — первый
    глиф ●/○ на одной позиции. Раньше равенство было только по ШИРИНАМ
    (право-прижатый трек внутри общего поля) — короткий базовый трек детей
    (5 ячеек, без 4-й позиции) сдвигался вправо, чтобы упереться в тот же
    правый край, что и трек пресета (5+3 ячейки с зарезервированной custom-
    позицией) — первый глиф у детей и у пресета не совпадал. custom-резерв
    рисуется правее троек и не двигает ни тройки, ни подписи между стандартным
    глобалом (medium) и custom (on+allow)."""
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    app_std, _ = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n')
    app_custom, _ = _app_with_config(
        custom_dir, 'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "allow"\n')

    async with app_std.run_test() as pilot_std:
        scr_std = await _open_modes_section(pilot_std)
        label_w, base_w, extra_w, value_w = scr_std._modes_columns()
        rows_std = [str(w.render()) for w in scr_std.query(".set-row")]
        rigor_std = next(r for r in rows_std if "rigor ·" in r)
        gate_std = next(r for r in rows_std if "самопроверка" in r)
        answers_std = next(r for r in rows_std if "формат ответа" in r)

    async with app_custom.run_test() as pilot_custom:
        scr_custom = await _open_modes_section(pilot_custom)
        rows_custom = [str(w.render()) for w in scr_custom.query(".set-row")]
        rigor_custom = next(r for r in rows_custom if "rigor ·" in r)
        gate_custom = next(r for r in rows_custom if "самопроверка" in r)
        answers_custom = next(r for r in rows_custom if "формат ответа" in r)

    assert base_w == 5 and extra_w == 3   # тройки(5) + custom-резерв(3) — общие для всех строк

    def first_glyph_col(row: str) -> int:
        for i, ch in enumerate(row):
            if ch in ("●", "○"):
                return i
        raise AssertionError(f"no track glyph in row: {row!r}")

    col_rigor = first_glyph_col(rigor_std)
    assert first_glyph_col(gate_std) == col_rigor
    assert first_glyph_col(answers_std) == col_rigor

    # Позиция базы (первый глиф) и подписей значений не сдвигается между
    # стандартным глобалом и custom — ни у пресета, ни у детей.
    assert first_glyph_col(rigor_custom) == col_rigor
    assert first_glyph_col(gate_custom) == first_glyph_col(gate_std)
    assert first_glyph_col(answers_custom) == first_glyph_col(answers_std)
    assert rigor_std.index("medium") == rigor_custom.index("custom")
    assert gate_std.rindex("smart") == gate_custom.rindex("on")
    assert answers_std.rindex("judge") == answers_custom.rindex("allow")


async def test_modes_value_column_fixed_width_aligns_session_tail(tmp_path):
    """Ревизия 6, п.3 (спека шапка): колонка подписи значения — фиксированной
    ширины (`value_w`, max по ВСЕМ именам значений секции + custom, докстрин
    `_modes_columns`) — хвост «· в текущей сессии: X» стартует в ОДНОЙ колонке
    у всех строк секции, даже когда глобальные значения разной длины (custom=6,
    judge=5, off=3) и сессионные X — тоже разной длины (high=4, deny=4, on=2)."""
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "off"\n  text_answer_mode: "judge"\n')
    app.tui_state.active_task_id = "t1"
    app._threads_dir = str(tmp_path / "threads")
    # глобал (off, judge) — не пресет -> rigor резолвится в custom (6 симв.);
    # сессия (on, deny) — ровно пара "high" (4 симв.), отличная от custom.
    app.apply_session_overrides({"tui.finish_gate_mode": "on", "tui.text_answer_mode": "deny"})
    async with app.run_test(size=(200, 24)) as pilot:
        scr = await _open_modes_section(pilot)
        rows = [str(w.render()) for w in scr.query(".set-row")]
        rigor_row = next(r for r in rows if "rigor ·" in r)
        gate_row = next(r for r in rows if "самопроверка" in r)
        answers_row = next(r for r in rows if "формат ответа" in r)

        assert "в текущей сессии: high" in rigor_row
        assert "в текущей сессии: on" in gate_row
        assert "в текущей сессии: deny" in answers_row

        tail = "· в текущей сессии:"
        assert rigor_row.index(tail) == gate_row.index(tail) == answers_row.index(tail)


async def test_modes_hint_global_framing_even_with_session_override(tmp_path):
    """Ревьюный follow-up V1 (`_modes_hint_text` -> `preset_custom_hint`): строка
    пресета в Settings правит ГЛОБАЛ (спека §4.2), поэтому custom-хинт обязан
    описывать глобальную пару текстом «ваш дефолт: …», даже когда сессия ТОЖЕ
    переопределяет пару — иначе хинт называет сессионные значения и рамку «своя
    пара этой сессии», хотя строка над ним показывает глобал. Глобал custom
    (on+allow), сессия расходится с ним (off+deny)."""
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "allow"\n')
    app.tui_state.active_task_id = "t1"
    app._threads_dir = str(tmp_path / "threads")
    app.apply_session_overrides({"tui.finish_gate_mode": "off", "tui.text_answer_mode": "deny"})
    async with app.run_test(size=(200, 24)) as pilot:
        scr = await _open_modes_section(pilot)
        hint = str(scr.query_one(".set-modes-hint", Static).render())
        assert "ваш дефолт: самопроверка on, формат allow" in hint
        assert "своя пара этой сессии" not in hint


async def test_modes_generic_toggle_appears_and_cycles_without_code_changes(tmp_path, monkeypatch):
    """Generic-гвоздь (design §4/§7 «Расширяемость», carry-⚠️ ревью T3): фиктивный
    4-значный тоггл, добавленный в TOGGLES ОДНОЙ записью (без правок рендера/палитры/
    подэкрана), появляется строкой в подэкране «Режимы работы» и парой в превью
    секции; ←/→ ходит по всем 4 значениям по кругу. Command в реестр палитры не
    регистрируем — подэкран и превью строятся из TOGGLES напрямую (config.py их
    не видит).

    state-провайдер фиктивного тоггла читает мутабельный holder (monkeypatch-friendly
    паттерн test_palette_toggles.py::test_five_value_toggle_works_generically, где
    state тоже не читает реальный cfg); apply_toggle тоже монкипатчится — реальная
    схема config.yml (extra=forbid) не знает ключ tui.fake_mode, поэтому пишем в
    holder напрямую, как это уже делает test_modes_subscreen_error_flash_keeps_screen
    для error-пути того же apply_toggle."""
    import tui.app as app_mod
    from tui import palette as p
    from tui import toggles as tg

    state_holder = {"value": "f0"}
    fake = tg.ToggleSpec(
        dotted="tui.fake_mode",
        values=tuple(tg.ToggleValue(f"f{i}", f"подсказка {i}", p.ACCENT) for i in range(4)),
        state=lambda cfg: state_holder["value"],
        label="фиктивный режим",
        summary="генерик-проверка",
    )
    monkeypatch.setitem(tg.TOGGLES, "fake", fake)

    def _fake_write(path, values, **kw):
        # _modes_apply пишет ровно один ключ {"tui.fake_mode": "<value>"}; реальная
        # схема config.yml (extra=forbid) фиктивный ключ не знает — перехватываем
        # запись и обновляем holder, из которого читает state-провайдер тоггла.
        state_holder["value"] = values["tui.fake_mode"]
    monkeypatch.setattr("onboarding_config_writer.write_values", _fake_write)

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "judge"\n',
                        encoding="utf-8")
    app = app_mod.TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()
    async with app.run_test(size=(200, 24)) as pilot:
        scr = await _open_settings(app, pilot, cfg_file, app._config)

        # 1) превью секции «Режимы работы» — пара фиктивного тоггла присутствует
        rows = _menu_lines(scr)
        modes_row = next(r for r in rows if "Режимы работы" in r)
        assert "фиктивный режим: f0" in modes_row

        # 2) подэкран: строка фиктивного тоггла смонтирована
        open_settings_section(scr, "Режимы работы")
        await pilot.pause()
        assert scr._view == settings_section_index("Режимы работы")
        names = tg.group_toggle_names("rigor")   # fake — default group="rigor"
        fake_idx = names.index("fake")
        row_texts = [str(w.render()) for w in scr.query(".set-row")]
        assert any("фиктивный режим" in r and "f0" in r for r in row_texts)

        # 3) ←/→ на строке фиктивного тоггла ходит по кругу через все 4 значения
        scr._modes_idx = fake_idx
        for expected in ("f1", "f2", "f3", "f0"):
            await pilot.press("right")
            await pilot.pause()
            assert state_holder["value"] == expected
        row_texts = [str(w.render()) for w in scr.query(".set-row")]
        assert any("фиктивный режим" in r and "f0" in r for r in row_texts)


def _app_with_config(tmp_path, yaml_text):
    """TuiApp с реальным config.yml на диске (тот же паттерн, что apply_toggle-тесты)."""
    import tui.app as app_mod

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    app = app_mod.TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()
    return app, cfg_file


async def _open_modes_section(pilot):
    """Настройки → «Режимы работы»: открыть экран настроек в этой секции."""
    app = pilot.app
    # on_apply — как в проде (_cmd_settings): экран применяет запись через него,
    # а не зовёт app.reload_config мимо инъекции.
    scr = _mk_screen(config=app._config, config_path=app._config_path,
                     on_apply=app.reload_config)
    app.push_screen(scr)
    await pilot.pause()
    open_settings_section(scr, "Режимы работы")
    await pilot.pause(0.1)
    return scr


async def test_modes_section_group_separator_between_two_preset_groups(tmp_path, monkeypatch):
    """Ревизия 7 (спека §4.2: разделитель групп — родовой по `preset_keys`,
    ОБЩИЙ для палитры и Settings; мирроринг
    test_palette_toggles.py::test_group_separator_between_two_preset_groups).
    Секция «Режимы работы» монтирует строки НАПРЯМУЮ из `TOGGLES` (нет своего
    реестра/фильтра, как у палитры) — честный 2-group тест подменяет сам
    `TOGGLES` ЦЕЛИКОМ (`monkeypatch.setattr`, не точечный `setitem`), иначе
    к синтетическим группам примешалась бы настоящая `rigor` (сегодня в
    проде группа одна). `_mount_modes_section` в остальном не тронута —
    это её РЕАЛЬНЫЙ путь монтирования на подменных данных, не фейковый стенд."""
    pytest.importorskip("textual")
    from tui import palette as p
    from tui import toggles as tg

    group_a = tg.ToggleSpec(
        values=(tg.ToggleValue("x", "hint x", p.INK), tg.ToggleValue("y", "hint y", p.ACCENT)),
        state=lambda cfg: "x", label="группа A", summary="демо",
        preset_keys=("tui.zz_a_child",),
    )
    group_b = tg.ToggleSpec(
        values=(tg.ToggleValue("p", "hint p", p.INK), tg.ToggleValue("q", "hint q", p.ACCENT)),
        state=lambda cfg: "p", label="группа B", summary="демо",
        preset_keys=("tui.zz_b_child",),
    )
    monkeypatch.setattr(tg, "TOGGLES", {"zzgroupa": group_a, "zzgroupb": group_b})

    app, _ = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)

        seps = list(scr.query(".set-group-sep"))
        assert len(seps) == 1                          # ровно один — между двумя группами

        rows = list(scr.query(".set-row"))
        assert len(rows) == 2
        assert rows[0].region.y < seps[0].region.y < rows[1].region.y


async def test_modes_apply_blocked_when_not_writable(tmp_path, monkeypatch):
    """При `_writable() is False` (пакетный шаблон) запись глобала НЕ проходит,
    config.yml не меняется, уведомление объясняет причину."""
    import config as config_mod
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')
    original = cfg_file.read_text(encoding="utf-8")
    monkeypatch.setattr(config_mod, "_default_config_path", lambda: str(cfg_file))

    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        assert not scr._writable()

        await pilot.press("right")
        await pilot.pause(0.1)

        assert cfg_file.read_text(encoding="utf-8") == original
        notice = " ".join(str(w.render()) for w in scr.query(Static))
        assert "недоступен для записи" in notice


async def test_modes_apply_preserves_unrelated_keys(tmp_path):
    """Запись глобала трогает только dotted-ключ переключённого тоггла —
    посторонний ключ config.yml (agent.model) остаётся с прежним значением.
    Курсор 0 = /rigor (пресет) — спускаемся на детальную gate."""
    app, cfg_file = _app_with_config(
        tmp_path,
        'tui:\n  finish_gate_mode: "on"\n'
        'agent:\n  model: "custom-model-xyz"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        await pilot.press("down")            # /rigor -> gate
        await pilot.pause(0.1)
        await pilot.press("right")          # gate on -> off глобально
        await pilot.pause(0.1)

        text = cfg_file.read_text(encoding="utf-8")
        assert 'finish_gate_mode: "off"' in text
        assert 'model: "custom-model-xyz"' in text


async def test_modes_apply_does_not_reset_live_session_override(tmp_path):
    """Центральная гарантия модели «место = scope» (Settings пишет глобал,
    сессию не трогает) — прямым тестом, не композицией существующих. Оверрайд
    "on" совпадает с исходным глобалом ДО переключения — это осознанный выбор:
    после единственного нажатия глобал уходит в "off" (цикл идёт от глобала),
    и только тогда пара расходится ("on" оверрайда против "off" нового
    глобала) — иначе ассерты не различили бы, откуда взялось эффективное
    значение. Если `_modes_apply` начнёт сбрасывать/перезаписывать сессионные
    переопределения при записи глобала — первым упадёт п.3 (эффективное
    значение перестанет быть оверрайдом). Курсор 0 = /rigor (пресет) —
    спускаемся на детальную gate; терминал шире (200 колонок) — хвост оверрайда
    в строке не зависит от ширины, здесь важен сам факт его наличия."""
    from tui.toggles import toggle_state
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')
    async with app.run_test(size=(200, 24)) as pilot:
        scr = await _open_modes_section(pilot)
        app.set_session_override("tui.finish_gate_mode", "on")

        await pilot.press("down")            # /rigor -> gate
        await pilot.pause(0.1)
        await pilot.press("right")          # глобал on -> off; оверрайд не трогаем
        await pilot.pause(0.1)

        text = cfg_file.read_text(encoding="utf-8")
        assert 'finish_gate_mode: "off"' in text                    # (1) новый глобал в файле

        assert app._session_overrides == {"tui.finish_gate_mode": "on"}  # (2) оверрайд выжил

        assert toggle_state(app, "gate") == "on"                    # (3) эффективное = оверрайд, не новый глобал

        rows = [str(w.render()) for w in scr.query(".set-row")]
        gate_row = next(r for r in rows if "самопроверка" in r)
        assert "· в текущей сессии: on" in gate_row                    # (4) пометка отражает пару слоёв


async def test_modes_row_shows_session_override_note(tmp_path):
    """§6.2 (ревизия 2026-07-21 вечер): когда сессия перекрывает глобальный дефолт
    режима, хвостом В СТРОКЕ тоггла (сразу после значения) появляется «· в текущей
    сессии: Y» — привязка к параметру однозначна, отдельной строки больше нет;
    без оверрайда хвоста нет ни у одной строки; слова «глобально» нигде нет
    (значение в строке — это и есть глобал); трек/значение строки по-прежнему
    показывают ГЛОБАЛ (секция правит его), не эффективное. Терминал шире (200
    колонок) — общие колонки секции выросли (глиф дерева + возможная 4-я позиция
    трека /rigor), хвост оверрайда проверяется независимо от ширины."""
    from tui.toggles import modes_section_names
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')
    async with app.run_test(size=(200, 24)) as pilot:
        scr = await _open_modes_section(pilot)
        rows = [str(w.render()) for w in scr.query(".set-row")]
        assert not any("в текущей сессии" in r for r in rows)   # оверрайда нет — хвоста нет нигде

        app.set_session_override("tui.finish_gate_mode", "off")
        scr._render_view()
        await pilot.pause(0.1)

        rows = [str(w.render()) for w in scr.query(".set-row")]
        assert len(rows) == len(modes_section_names())   # пометка не добавляет строк

        gate_row = next(r for r in rows if "самопроверка" in r)
        answers_row = next(r for r in rows if "формат ответа" in r)
        assert "· в текущей сессии: off" in gate_row
        assert "глобально" not in gate_row
        assert "в текущей сессии" not in answers_row             # неразошедшийся — без хвоста
        # значение в строке — глобал, не сессия (сплит по самому хвосту: head ребёнка
        # тоже несёт маркер «· » ревизии 3, поэтому голый split("·") больше не годится)
        assert gate_row.split("· в текущей сессии:")[0].rstrip().endswith("on")


async def test_modes_no_override_note_from_legacy_key_alone(tmp_path):
    """Инвариант §6.2: расхождение глобал≠эффективное возможно ТОЛЬКО от
    сессионного оверрайда — не от того, ЧЕМ посчитан сам глобал. Легаси-ключ
    `agent.strict_turn_finish_validation: false` (без tui.finish_gate_mode) уже
    заставляет правило коллизии эффективного /gate отдать "off", но без сессии
    toggle_state_base и toggle_state гоняют то же правило по одному и тому же
    конфигу — расхождения нет. Гвоздь против будущей правки effective_config,
    которая начнёт менять БАЗОВЫЕ ключи не из session overrides."""
    from tui.toggles import toggle_state, toggle_state_base
    app, cfg_file = _app_with_config(
        tmp_path, 'agent:\n  strict_turn_finish_validation: false\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        rows = [str(w.render()) for w in scr.query(".set-row")]
        assert not any("в текущей сессии" in r for r in rows)
        assert toggle_state_base(app, "gate") == toggle_state(app, "gate") == "off"


async def test_modes_row_hides_session_override_note_when_narrow(tmp_path):
    """Important-1 (ревью): ветка «хвост не влез» `_mode_row_text` (§6.2, all-or-
    nothing по `_modes_row_width`) не была исполнена ни одним из существующих
    тестов — все на широком терминале, где хвост всегда помещается. На узком
    терминале (60 колонок → `_modes_row_width()` == 50) ядро строки gate (курсор +
    label + трек + значение, 44 символа) ещё влезает, а ядро + хвост
    "   · в текущей сессии: off" (44 + 23 = 67 символов) — уже нет: хвост уходит
    ЦЕЛИКОМ (не обрезком/многоточием — в отличие от hint-строки у него нет
    механики усечения), ядро остаётся видимым полностью. На широком терминале
    (100 колонок → row_width == 90) тот же оверрайд даёт хвост целиком."""
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')
    async with app.run_test(size=(60, 24)) as pilot:
        scr = await _open_modes_section(pilot)
        app.set_session_override("tui.finish_gate_mode", "off")
        scr._render_view()
        await pilot.pause(0.1)          # carry-fix _refresh_modes_rows после layout

        rows = [str(w.render()) for w in scr.query(".set-row")]
        gate_row = next(r for r in rows if "самопроверка" in r)
        assert "в текущей сессии" not in gate_row     # хвост ушёл целиком
        assert "· в тек" not in gate_row            # не обрезком/многоточием
        assert "самопроверка выполнения задачи" in gate_row   # ядро: label цел
        assert gate_row.rstrip().endswith("on")               # ядро: значение (глобал) цело

    app2, _cfg_file2 = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')
    async with app2.run_test(size=(100, 24)) as pilot2:
        scr2 = await _open_modes_section(pilot2)
        app2.set_session_override("tui.finish_gate_mode", "off")
        scr2._render_view()
        await pilot2.pause(0.1)

        rows2 = [str(w.render()) for w in scr2.query(".set-row")]
        gate_row2 = next(r for r in rows2 if "самопроверка" in r)
        assert "· в текущей сессии: off" in gate_row2   # на широком — хвост есть


async def test_connection_section_mounts_connection_editor(tmp_path):
    from tui.screens.settings import SettingsScreen
    from tui.editors.connection import ConnectionEditor
    import tui.app as app_mod
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('backend:\n  completions_url: "http://h/v1/chat/completions"\n'
                        '  chat_completion_headers:\n    Authorization: ""\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        assert scr.query(ConnectionEditor)


async def test_settings_model_filter_and_save(tmp_path, monkeypatch):
    """Секция Модель: фильтр РЕАЛЬНО сужает список (match остаётся, non-match уходит),
    ↑↓ выбирает, Enter сохраняет выбранную модель в config.yml."""
    from textual.widgets import Input
    # settings._mount_section_body(_MODEL) делает `from model_picker import fetch_models`
    # (локальный импорт) → мокать надо СИМВОЛ в model_picker, не в модуле settings.
    # Три модели: активная (glm) + совпадающая с "gpt" + НЕ совпадающая (deepseek).
    # Без non-match фильтр был бы no-op: _other_models() и так исключает активную, и
    # видимый список из 2 моделей байт-в-байт совпадал бы до и после фильтра — сломанная
    # фильтрация прошла бы тест зелёным. deepseek доказывает, что фильтр сузил.
    monkeypatch.setattr("model_picker.fetch_models",
                        lambda *a, **k: [type("M", (), {"id": "glm-5.2-fp8", "tool_capable": True})(),
                                         type("M", (), {"id": "openai/gpt-5.6-terra", "tool_capable": True})(),
                                         type("M", (), {"id": "deepseek-v3-base", "tool_capable": True})()],
                        raising=False)
    cfg_file = _write_cfg(tmp_path, model='"glm-5.2-fp8"')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Модели")
        await pilot.pause(); await pilot.pause()           # fetch-воркер
        scr.query_one("#onb-model", Input).value = "gpt"   # поле само себе фильтр (§3.2.6)
        await pilot.pause()
        # Фильтр реально сузил список на уровне интеграции SettingsScreen (до навигации):
        # Фильтр гасит строки через `display`, а не сносит их (общая механика
        # `FieldListController`) — «на экране» и значит `w.display`.
        rows = [str(w.render()) for w in scr.query(".onb-option") if w.display]
        assert any("gpt-5.6-terra" in r for r in rows)     # match остался
        assert not any("deepseek" in r for r in rows)      # non-match отфильтрован
        # ряды после фильтра: текущая (glm-5.2-fp8) → gpt-5.6-terra. Строки «вписать
        # вручную» больше нет, поэтому одного down хватает; ↑↓ доходят до ed.move()
        # при фокусе в поле-фильтре.
        await pilot.press("down")
        await pilot.press("enter")                          # сохранить выбранную
        await pilot.pause(); await pilot.pause()
        content = cfg_file.read_text(encoding="utf-8")
        assert 'model: "openai/gpt-5.6-terra"' in content   # точное поле, не подстрока


def test_settings_header_hides_the_number_when_running_from_sources(monkeypatch):
    """Шапка спрашивала номер напрямую и показывала его даже из исходников — рядом с
    секцией «Обновление», которая честно писала «установка из исходников». Одно
    состояние называлось на двух экранах по-разному."""
    import app_version
    from tui.screens.settings import SettingsScreen

    scr = SettingsScreen(config={}, config_path=None, on_apply=lambda: None)
    monkeypatch.setattr(app_version, "install_source",
                        lambda: app_version.InstallSource("u", None, None, True))
    assert scr._version_text() == "gc-ua · dev"

    monkeypatch.setattr(app_version, "install_source",
                        lambda: app_version.InstallSource("u", "master", "c", False))
    monkeypatch.setattr(app_version, "agent_version", lambda: "1.2.3")
    assert scr._version_text() == "gc-ua · v1.2.3"


# --- одна клавиша удаления: Del в MCP, тот же гейт по фокусу, что у голоса (§5.3) ---


async def test_delete_key_removes_mcp_server(tmp_path):
    """Del удаляет сервер в MCP так же, как локальный сервис в голосе."""
    from tui.services.mcp_manager import McpState
    async with settings_mcp_section(tmp_path, servers=["ctx7"]) as (pilot, scr):
        ed = scr._mcp_editor()
        for _ in range(50):                                  # статус пакета грузит фоновый воркер
            if ed.status_ready("ctx7"):
                break
            await pilot.pause()
        ed._states["ctx7"] = McpState(True, None, None)      # статус: пакет установлен
        await pilot.press("delete")
        await pilot.pause()
        assert ed.awaiting_confirm, "Del должен запросить подтверждение удаления"


async def test_delete_in_input_edits_text(tmp_path):
    """Фокус в поле ввода — Del правит текст, а не удаляет объект.

    Секция MCP полей ввода не имеет, поэтому гейт проверяем там, где они есть,
    и где Del теперь тоже удаляет объект — в голосовой секции. Гейт общий, его
    достаточно проверить один раз на живом поле.
    """
    from textual.widgets import Input
    async with settings_voice_section(tmp_path, mode="cloud") as (pilot, scr):
        await pilot.press("tab")                        # в группу полей: фокус в #voice-url
        await pilot.pause()
        inp = scr.query_one("#voice-url", Input)
        inp.value = "abc"
        inp.cursor_position = 0
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert inp.value == "bc", "Del в поле обязан править текст"


def test_local_lang_footer_does_not_promise_delete():
    """Курсор на «Язык» локальной панели: Del здесь не работает (гейт по фокусу
    плюс требование позиции), значит футер его не обещает (спека §2.11)."""
    from tui.screens.settings import _VOICE_FOOTER_LOCAL_LANG
    assert "Del" not in _VOICE_FOOTER_LOCAL_LANG


# --- Ctrl+Space: перечитать то, на чём стою (§5.1) ---------------------------


async def test_ctrl_space_on_plain_field_reprobes(tmp_path):
    """Курсор на поле без списка (токен): клавиша шлёт пробу — так же, как и на
    адресе (развод §2 спеки 2026-08-06: Ctrl+Space про список нигде не спрашивает,
    роль от курсора не зависит)."""
    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        await pilot.press("down")                  # с адреса на токен — не обязательно, но и не мешает
        await pilot.pause()
        calls.clear()                              # забыть автопробу входа в секцию
        await pilot.press("ctrl+space")
        await wait_until(pilot, lambda: bool(calls), "ручная проверка должна уйти")


async def test_ctrl_space_reprobe_ignores_debounce_snapshot(tmp_path):
    """Ручная проверка проходит и при неизменных полях — иначе залипший красный вердикт
    нечем сбросить (спека §5.2)."""
    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        await pilot.press("down")                  # на токен — не обязательно: на адресе тоже проба
        await pilot.pause()
        calls.clear()
        await pilot.press("ctrl+space")
        await wait_until(pilot, lambda: len(calls) == 1, "первая ручная проверка ушла")
        await pilot.press("ctrl+space")            # поля не трогали
        await wait_until(pilot, lambda: len(calls) == 2,
                         "снимок против дребезга не должен глушить ручную")


async def test_alt_down_on_the_agent_model_field_reloads_the_list(tmp_path, monkeypatch):
    """Первая строка таблицы §2.5: поле моделей АГЕНТА — поле со списком с сервера,
    и Alt+↓ идёт за списком заново (спека 2026-08-06 §2: раскрытие/перечитывание —
    его роль, Ctrl+Space в этой секции пробы не даёт вовсе, см. §5.2 спеки).

    Сценарий — ровно тот, ради которого клавиша расширялась: сервер лежал (список не
    пришёл, на экране одинокое поле), потом ожил. Сессионный кэш списков этому не
    мешает — неудачу он не хранит (`make_cached_fetch`: `if models:`)."""
    from tui.editors.model import ModelEditor
    calls = []

    def fake_fetch(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return None                                    # сервер лежал
        return [type("M", (), {"id": "glm-5.2-fp8", "tool_capable": True})(),
                type("M", (), {"id": "deepseek-v3-base", "tool_capable": True})()]

    # Локальный импорт в _mount_section_body → мокаем СИМВОЛ в model_picker.
    monkeypatch.setattr("model_picker.fetch_models", fake_fetch, raising=False)
    cfg_file = _write_cfg(tmp_path, model='""')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Модели")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        assert not scr.query(".onb-option"), "предусловие: сервер списка не отдал"
        await pilot.press("alt+down")
        await wait_until(pilot, lambda: any("deepseek" in str(w.render())
                                            for w in scr.query(".onb-option")),
                         "список перечитан с сервера")


async def test_alt_down_on_the_agent_model_field_goes_past_the_session_cache(
        tmp_path, monkeypatch):
    """Список УЖЕ загрузился удачно — и клавиша всё равно обязана сходить на сервер
    (§2.5 спеки «идём на сервер за списком», Alt+↓ шлёт всегда).

    Без обхода кэша она была бы ДЕТЕРМИНИРОВАННЫМ no-op, а не «иногда вернёт старое»:
    ключ кэша — (url, auth, verify, proxy), и все четыре заморожены в конструкторе
    ModelEditor. Тот же ключ — тот же ответ, всегда. Голосовой список-сосед той же
    природы ходит мимо кэша вовсе, и разъезд одной клавиши на двух полях — ровно то,
    против чего написан канон."""
    from tui.editors.model import ModelEditor

    def _m(mid):
        return type("M", (), {"id": mid, "tool_capable": True})()

    calls = []

    def fake_fetch(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return [_m("glm-5.2-fp8")]                 # что сервер отдавал сначала
        return [_m("glm-5.2-fp8"), _m("deepseek-v3-base")]

    monkeypatch.setattr("model_picker.fetch_models", fake_fetch, raising=False)
    cfg_file = _write_cfg(tmp_path, model='""')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Модели")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        rows = lambda: [str(w.render()) for w in scr.query(".onb-option")]
        assert any("glm-5.2-fp8" in r for r in rows())    # предусловие: список пришёл
        assert not any("deepseek" in r for r in rows())
        await pilot.press("alt+down")
        await wait_until(pilot, lambda: any("deepseek" in r for r in rows()),
                         "перечитывание должно идти мимо сессионного кэша")


async def test_alt_down_on_the_agent_model_field_keeps_the_chosen_row(tmp_path, monkeypatch):
    """В /settings пометки ● нет — выбор и есть курсор. Перечитывание не имеет права
    молча вернуть его на якорь: секция перестала бы читаться «изменено», а Enter не
    сохранил бы ничего."""
    from tui.editors.model import ModelEditor

    def _m(mid):
        return type("M", (), {"id": mid, "tool_capable": True})()

    calls = []

    def fake_fetch(*a, **k):
        calls.append(1)
        base = [_m("glm-5.2-fp8"), _m("openai/gpt-5.6-terra")]
        return base if len(calls) == 1 else base + [_m("deepseek-v3-base")]

    monkeypatch.setattr("model_picker.fetch_models", fake_fetch, raising=False)
    cfg_file = _write_cfg(tmp_path, model='"glm-5.2-fp8"')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Модели")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        ed = scr.query_one(ModelEditor)
        await pilot.press("down")                              # текущая → gpt
        assert ed.peek() == "openai/gpt-5.6-terra"             # предусловие: выбор сделан
        await pilot.press("alt+down")
        await wait_until(pilot, lambda: any("deepseek" in str(w.render())
                                            for w in scr.query(".onb-option")),
                         "список перечитан")
        assert ed.peek() == "openai/gpt-5.6-terra", "несохранённый выбор выброшен"


async def test_ctrl_space_on_the_model_section_does_nothing(tmp_path, monkeypatch):
    """Пробы в «Модели» нет (§2.5, §5.2 спеки 2026-08-06) — Ctrl+Space съедается
    молча: ни перечитывания списка, ни падения, ни провала в глобальные биндинги
    (курсор и выбор остаются на месте)."""
    from tui.editors.model import ModelEditor

    def fake_fetch(*a, **k):
        return [type("M", (), {"id": "glm-5.2-fp8", "tool_capable": True})()]

    monkeypatch.setattr("model_picker.fetch_models", fake_fetch, raising=False)
    cfg_file = _write_cfg(tmp_path, model='"glm-5.2-fp8"')
    cfg = _loaded(cfg_file)
    import tui.app as app_mod
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Модели")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        rows_before = [str(w.render()) for w in scr.query(".onb-option")]
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert app.screen is scr                       # приложение живо
        assert [str(w.render()) for w in scr.query(".onb-option")] == rows_before


async def test_connection_glyph_is_about_server_and_token_not_the_model(tmp_path):
    """Живой кейс GigaCode: сервер доступен, токен принят, но имя модели неверное —
    ступень 2 отвечает 404 «проверьте адрес и имя модели». Раньше «достижимым»
    считался только ok или 401, поэтому такой ответ красил «Подключение» красным
    «!» с пометкой «сервер недоступен СЕЙЧАС» — при живом сервере и рабочем токене.

    Глиф «Подключения» отвечает за сервер и токен; про модель краснеет своя строка."""
    from tui.screens.settings import SettingsScreen, _CONNECTION
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('backend:\n  completions_url: "http://h/v1"\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        scr._raw["Authorization"] = "Bearer sk-live"

        scr._probe_config = ProbeResult(
            "rejected", 404, None, 12,
            "отклонено сервером (404): проверьте адрес и имя модели")
        assert scr._menu_glyph(_CONNECTION)[0] == "✓"    # сервер ответил, токен не 401

        # Настоящий обрыв — вот теперь «!» по делу.
        scr._probe_config = ProbeResult("unknown", None, None, 12,
                                        "нет соединения (timeout)", offline=True)
        assert scr._menu_glyph(_CONNECTION)[0] == "!"

        # Отклонённый токен — тоже «!», но по другой причине.
        scr._probe_config = ProbeResult("rejected", 401, None, 12, "токен отклонён (401)")
        assert scr._menu_glyph(_CONNECTION)[0] == "!"


async def test_connection_glyph_not_green_when_server_demands_a_token(tmp_path):
    """Токен снят (Ctrl+U / «без авторизации»), а сервер отвечает 401 — конфиг
    нерабочий, агент на нём не поедет. Хаб обязан это показать: раньше правило
    «токена нет → решает только достижимость» ставило зелёную галочку рядом с
    секцией, которая честно писала «⚠ сервер требует авторизацию (401)»."""
    from tui.screens.settings import SettingsScreen, _CONNECTION
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text('backend:\n  completions_url: "http://h/v1"\n', encoding="utf-8")
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        scr._raw["Authorization"] = ""            # токена нет

        scr._probe_config = ProbeResult("rejected", 401, None, 12, "токен отклонён (401)")
        assert scr._menu_glyph(_CONNECTION)[0] == "!"

        # А вот сервер, которому токен и не нужен, — зелёный по делу.
        scr._probe_config = ProbeResult("ok", 200, None, 12, "доступен")
        assert scr._menu_glyph(_CONNECTION)[0] == "✓"


async def test_connection_footer_names_the_clear_key_on_the_token_field(tmp_path):
    """Футер секции контекстный: «Alt+↓ — показать список» обещается только на
    адресе. Симметрично и «Ctrl+U — очистить» — только на токене и только когда
    есть что стирать. Клавиши на разных полях, в одной строке не встречаются,
    так что бюджету ширины ничего не грозит."""
    from tui.screens.settings import SettingsScreen, _CONNECTION
    import tui.app as app_mod
    cfg_file = _write_cfg(tmp_path)          # с сохранённым токеном — иначе стирать нечего
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                             on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok()))
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr._connection_editor()

        assert "Ctrl+U" not in scr._footer_text()      # курсор на адресе — молчим
        ed.move(1)                                     # → токен
        await pilot.pause()
        assert "Ctrl+U — очистить" in scr._footer_text()

        ed.clear_token()                               # стёрли — стирать больше нечего
        await pilot.pause()
        assert "Ctrl+U" not in scr._footer_text()


async def test_ctrl_u_over_env_ref_warns_about_deletion_not_a_leak(tmp_path, monkeypatch):
    """Ctrl+U поверх ссылки `${ENV:VAR}`: страж перезаписи обязан сработать (ссылка
    не гибнет молча), но сказать ПРАВДУ — сохранение её удалит, а не «запишет
    введённое значение открытым текстом». Утечки на этом пути нет: поле пустое.

    Единый текст на оба случая пугал утечкой ровно там, где идёт стирание."""
    monkeypatch.setenv("GC_API_KEY", "sk-REALSECRET123")
    cfg_file = _write_cfg(tmp_path, auth='"${ENV:GC_API_KEY}"')
    import tui.app as app_mod
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr._connection_editor()
        ed.move(1)                                   # → поле токена
        await pilot.pause()
        assert ed.clear_token() is True              # маска была — есть что стирать
        await pilot.pause()

        await pilot.press("enter")                   # сохранение → страж
        await pilot.pause(); await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query("#set-envconfirm"))
        assert "GC_API_KEY" in banner                        # страж сработал
        assert "удалит ссылку из файла" in banner
        assert "открытым текстом" not in banner


async def test_literal_over_env_ref_still_warns_about_the_leak(tmp_path, monkeypatch):
    """Обратный случай не должен пострадать от правки выше: вписанный поверх ссылки
    литерал по-прежнему предупреждает именно об утечке в файл."""
    monkeypatch.setenv("GC_API_KEY", "sk-REALSECRET123")
    cfg_file = _write_cfg(tmp_path, auth='"${ENV:GC_API_KEY}"')
    import tui.app as app_mod
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        from textual.widgets import Input as _Input
        scr._connection_editor().query_one("#conn-token", _Input).value = "sk-typed-by-hand"
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query("#set-envconfirm"))
        assert "GC_API_KEY" in banner
        assert "открытым текстом" in banner
        assert "удалит ссылку" not in banner


async def test_ctrl_u_invalidates_the_verdict_and_reprobes_on_leaving_the_field(tmp_path):
    """Ctrl+U — правка значения, а `Input.Changed` по ней не летит (пустое поверх
    пустого). Значит обесценивать проверенное обязан сам обработчик: иначе на экране
    остаётся вердикт, проверявший конфигурацию С токеном, а уцелевший снимок полей
    гейтом дребезга запрещает уходу с поля перепроверить — «✓ токен принят» так и
    висит над конфигурацией БЕЗ токена, и Enter сохраняет с этим вердиктом."""
    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        ed = scr._connection_editor()
        await pilot.press("down")                    # адрес → токен (значения те же: пробы нет)
        await pilot.pause()
        assert scr._draft_checked is not None        # вердикт входа в секцию на месте
        before = len(calls)

        await pilot.press("ctrl+u")
        await pilot.pause()
        assert ed.token_cleared is True              # клавиша дошла и сработала
        assert scr._draft_checked is None            # вердикт обесценен

        await pilot.press("down")                    # уход с токена → автопроба заново
        await wait_until(pilot, lambda: len(calls) > before,
                         "уход с поля после Ctrl+U перезапустил пробу")
        assert calls[-1][1] is None                  # и ушла БЕЗ токена


async def test_ctrl_u_dismisses_the_pending_env_confirm(tmp_path, monkeypatch):
    """Ctrl+U снимает висящий confirm перезаписи `${ENV:...}`.

    Confirm держит СНИМОК значений (`_pending_env_save`), сделанный до очистки. Оставь
    его на экране — и следующий Enter сохранит именно тот снимок: очистка токена в него
    не попала, и в файл уехало бы состояние ДО Ctrl+U.

    Ссылка здесь в АДРЕСЕ, а не в токене: страж поднимается правкой адреса, а снять
    его потом обязана очистка токена. На поле токена курсор переводим прямым
    `ed.move(1)` (стрелка сняла бы confirm сама, и проверять стало бы нечего) —
    `clear_token()` гейтит именно позицию: на адресе Ctrl+U молчит, иначе целившийся
    в строку адреса пометил бы к удалению сохранённый токен. Поле токена при этом
    пустое, `Input.Changed` по очистке не летит, и снять страж некому, кроме самого
    обработчика клавиши."""
    monkeypatch.setenv("GC_URL", "https://from-env/v1/chat/completions")
    cfg_file = _write_cfg(tmp_path, url='"${ENV:GC_URL}"')   # токен — обычный литерал (маска есть)
    import tui.app as app_mod
    from textual.widgets import Input as _Input
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr._connection_editor()
        ed.query_one("#conn-url", _Input).value = "https://typed-by-hand/v1/chat/completions"
        await pilot.pause()

        await pilot.press("enter")                   # сохранение → страж перезаписи ссылки
        await pilot.pause(); await pilot.pause()
        assert list(scr.query("#set-envconfirm")), "страж не поднялся — проверять нечего"

        ed.move(1)                                   # → поле токена: там клавиша обещана
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert ed.token_cleared is True              # клавиша дошла и сработала
        assert not list(scr.query("#set-envconfirm")), "баннер остался висеть"
        assert scr._pending_env_save is None          # и снимок под ним не пережил очистку


def test_env_confirm_text_names_both_fates_when_they_differ():
    """Адрес перезаписали литералом, токен стёрли — обе ссылки затрагиваются, но
    по-разному. Умолчать об одной значило бы выбрать, о чём соврать."""
    scr = _mk_screen()
    text = scr._env_confirm_text(["GC_URL"], ["GC_API_KEY"])
    assert "GC_URL — сохранение запишет введённое значение в файл открытым текстом" in text
    assert "GC_API_KEY — сохранение удалит ссылку из файла" in text


# ── хаб догоняет вердикт о токене ровно там, где его нет (разбор ревью 2026-08-18) ──

def _models_ok(ids=("test-model",)):
    from model_picker import ModelInfo
    from tui.services.settings_probe import ProbeResult
    return ProbeResult("ok", 200, [ModelInfo(id=i) for i in ids], 10, "доступен")


def _auth_probe_returning(*results):
    """Заглушка маршрута chat/completions: по одному ответу на вызов, потом последний."""
    seen = []

    def probe(endpoint, auth, verify, model, *, config_proxy=None, post_ex=None):
        seen.append(auth)
        return results[min(len(seen) - 1, len(results) - 1)]

    probe.seen = seen
    return probe


async def _hub(app, pilot, *, probe_fn, auth_probe_fn):
    from tui.screens.settings import SettingsScreen
    scr = SettingsScreen(config=_cfg(), config_path="C:/tmp/c.yml", on_apply=lambda: None,
                         probe_fn=probe_fn, auth_probe_fn=auth_probe_fn)
    app.push_screen(scr)
    await pilot.pause(); await pilot.pause(); await pilot.pause()
    return scr


async def test_hub_judges_the_token_on_the_real_route():
    """Публичный справочник моделей (OpenRouter) отвечает и без токена — вердикта о
    токене в нём нет. Хаб догоняет differential-пробой по chat/completions, иначе он
    рисует «✓» ровно там, где секция пишет «✗ токен отклонён»."""
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod
    auth = _auth_probe_returning(ProbeResult("rejected", 401, None, 5, "токен отклонён (401)"))
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = await _hub(app, pilot, probe_fn=_fake_probe(_models_ok()),
                         auth_probe_fn=auth)
        assert auth.seen, "differential не запускался"
        assert scr._menu_glyph(settings_section_index("Подключение"))[0] == "!"
        assert scr._menu_glyph(settings_section_index("Модели"))[0] == "✓"   # модели не потеряны


async def test_hub_shows_the_accepted_token_green():
    """Тот же путь с принятым токеном: сервер требует авторизацию, наш токен проходит."""
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod
    auth = _auth_probe_returning(
        ProbeResult("rejected", 401, None, 5, "токен отклонён (401)"),   # control без токена
        ProbeResult("ok", 200, None, 5, "доступен"),                     # повтор с токеном
    )
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = await _hub(app, pilot, probe_fn=_fake_probe(_models_ok()), auth_probe_fn=auth)
        assert scr._menu_glyph(settings_section_index("Подключение"))[0] == "✓"


async def test_hub_does_not_recheck_a_verdict_that_is_already_certain():
    """Справочник ответил 401 — токен отклонён достоверно, второй заход был бы
    лишним запросом. Догоняем только неизвестность, а не всё подряд."""
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod
    auth = _auth_probe_returning(ProbeResult("ok", 200, None, 5, "доступен"))
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        rejected = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")
        scr = await _hub(app, pilot, probe_fn=_fake_probe(rejected), auth_probe_fn=auth)
        assert auth.seen == []                                            # лишних запросов нет
        assert scr._menu_glyph(settings_section_index("Подключение"))[0] == "!"


async def test_hub_does_not_recheck_when_the_probe_already_used_the_real_route():
    """Справочника нет (gigacode: /config отдаёт 401), и проба сама ушла на
    chat/completions — её «ок» уже про реальный маршрут. Догонять нечего."""
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod
    auth = _auth_probe_returning(ProbeResult("ok", 200, None, 5, "доступен"))
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        via_post = ProbeResult("ok", 200, None, 5, "доступен")            # models=None
        scr = await _hub(app, pilot, probe_fn=_fake_probe(via_post), auth_probe_fn=auth)
        assert auth.seen == []
        assert scr._menu_glyph(settings_section_index("Подключение"))[0] == "✓"


async def test_hub_says_nothing_about_a_token_it_could_not_check():
    """Догон сорвался (сеть моргнула ровно на нём): утверждать про токен нечего —
    «—», а не «✓» и не «!». Молчать честнее, чем гадать."""
    from tui.services.settings_probe import ProbeResult
    import tui.app as app_mod

    def failing_auth(endpoint, auth, verify, model, *, config_proxy=None, post_ex=None):
        return ProbeResult("unknown", None, None, 5, "нет соединения (timeout)", offline=True)

    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        scr = await _hub(app, pilot, probe_fn=_fake_probe(_models_ok()),
                         auth_probe_fn=failing_auth)
        assert scr._menu_glyph(settings_section_index("Подключение"))[0] == "—"


# ── перенесено из feat/profiles-a при синке 2026-08-25 ──

def _conn_glyph(result):
    from tui.services.settings_probe import ProbeResult  # noqa: F401  (тип результата)
    scr = _mk_screen(_cfg())
    scr._probe_config = result
    return scr._menu_glyph(settings_section_index("Подключение"))[0]

def test_hub_glyph_blames_the_server_when_it_did_not_answer():
    """До сервера не дошли — он и виноват. Контрольный случай для теста ниже."""
    from tui.services.settings_probe import ProbeResult
    r = ProbeResult("unknown", None, None, 0, "нет соединения (timeout)", offline=True)
    assert _conn_glyph(r) == "!"

def test_hub_glyph_does_not_blame_the_server_for_our_own_failure():
    """Проверка сорвалась у нас (исключение в воркере — например, кривой прокси-резолв):
    ответа нет, но и до сервера мы не доходили. Строка секции про этот же результат
    пишет «не удалось проверить», а хаб рисовал «!» и объявлял сервер недоступным —
    правило «винить сервер можно только когда до него не дошли» ветка применила
    только к одному из двух рендеров."""
    from tui.services.settings_probe import ProbeResult
    r = ProbeResult("unknown", None, None, 0, "внутренняя ошибка проверки: boom")
    assert _conn_glyph(r) != "!"

async def test_ctrl_u_under_the_env_banner_is_not_lost(tmp_path, monkeypatch):
    """Подтверждение перезаписи ссылки висит, человек тем временем стирает токен.
    Подтверждают ВСЕГДА текущую форму: снимок, снятый до очистки, сохранил бы токен,
    который на экране уже помечен к удалению, — а баннер назвал бы не ту судьбу.

    Печать в поле проходит этот же сброс через `on_input_changed`; Ctrl+U стирает
    то, чего в поле нет (секрет — под маской), и события Changed не порождает."""
    monkeypatch.setenv("GC_URL", "http://from-env/v1/chat/completions")
    cfg_file = _write_cfg(tmp_path, url='"${ENV:GC_URL}"')
    import tui.app as app_mod
    from textual.widgets import Input
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr._connection_editor()
        ed.query_one("#conn-url", Input).value = "http://typed/v1/chat/completions"
        await pilot.pause()

        await pilot.press("enter")                   # страж: ссылка в адресе будет перезаписана
        await pilot.pause(); await pilot.pause()
        assert scr._pending_env_save is not None     # предусловие: подтверждение висит
        assert "Authorization" not in scr._pending_env_save[1]   # токен пока не тронут

        ed.move(1)                                   # → поле токена
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert ed.token_cleared is True
        assert scr._pending_env_save is None         # прежний снимок протух вместе с формой

        await pilot.press("enter")                   # страж поднимается заново — уже по текущей форме
        await pilot.pause(); await pilot.pause()
        assert scr._pending_env_save is not None
        assert scr._pending_env_save[1].get("Authorization") == ""   # очистка не потерялась

async def test_ctrl_u_reprobes_on_the_next_leave(tmp_path):
    """Очистка токена меняет то, что уйдёт в запрос (проба пойдёт без него), значит
    вердикт устарел. Снимок дребезга хранил только (адрес, набранный токен, прокси):
    Ctrl+U его не двигал, и уход с поля пробу не повторял — на экране оставалось
    «токен принят» под конфигурацией, где токена уже нет."""
    cfg_file = _write_cfg(tmp_path)
    import tui.app as app_mod
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _open_settings(app, pilot, cfg_file, cfg)
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        ed = scr._connection_editor()
        ed.move(1)                                   # → поле токена
        await pilot.pause()

        probes = []
        orig = scr._start_draft_probe
        scr._start_draft_probe = lambda *a, **kw: (probes.append(1), orig(*a, **kw))[1]

        await pilot.press("down")                    # уход без правок — проверять нечего
        await pilot.pause()
        assert probes == []                          # предусловие: гейт дребезга работает

        await pilot.press("up")                      # вернулись на токен
        await pilot.press("ctrl+u")
        await pilot.press("down")                    # теперь уходим уже с очисткой
        await pilot.pause()
        assert ed.token_cleared is True
        assert len(probes) == 1


# ── глиф «Модели»: вердикт о СТЕКЕ, а не об одном файле ─────────────────────────


def _stacked_layers(tmp_path, monkeypatch):
    """База + каталог профилей рядом с ней, изолированные от машины (тот же приём,
    что в `tests/config/test_stacked_validation.py`): иначе managed-оверлей и
    проектный `.gc/config.yml` разработчика подмешались бы в стек, который судит
    глиф."""
    from config_modules import env_vars
    from config_modules.env_vars import EnvVars
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    (tmp_path / "profiles").mkdir()
    return tmp_path / "config.yml", tmp_path / "profiles"


_STACK_BASE = ('profile: "work"\n'
               "backend:\n"
               '  completions_url: "http://host/v1/chat/completions"\n'
               "models:\n"
               '  a:\n    model: "x"\n'
               '  b:\n    model: "y"\n')


def _models_glyph(base) -> str:
    scr = _mk_screen(config=_cfg(), config_path=str(base))
    return scr._models_glyph()[0]


def test_hub_models_glyph_judges_the_stack_not_the_single_file(tmp_path, monkeypatch):
    """«Модели в основном конфиге, уровни в профиле» — рабочая раскладка: стек
    резолвится, приложение стартует. Вердикт по ОДНОМУ файлу видит два алиаса без
    единого слота и красит строку хаба «!» — про конфиг, который работает."""
    base, profiles = _stacked_layers(tmp_path, monkeypatch)
    base.write_text(_STACK_BASE, encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    assert _models_glyph(base) == "✓"


def test_hub_models_glyph_flags_a_stack_broken_by_the_profile(tmp_path, monkeypatch):
    """Обратная сторона того же правила: сам по себе config.yml резолвится, а слот
    активного профиля смотрит в пустоту — следующий старт упадёт. «✓» здесь было бы
    зелёным светом конфигу, который не загрузится."""
    base, profiles = _stacked_layers(tmp_path, monkeypatch)
    base.write_text('profile: "work"\nmodels:\n  a:\n    model: "x"\n'
                    'slots:\n  default: "a"\n', encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "ghost"\n', encoding="utf-8")
    assert _models_glyph(base) == "!"


def test_hub_models_glyph_is_not_recomputed_while_the_files_stand_still(
        tmp_path, monkeypatch):
    """Вердикт — полная YAML+pydantic валидация стека (≈33 мс на конфиге размером с
    шаблон), а строку хаба перерисовывает КАЖДОЕ ↑/↓ в меню. Пока ни один файл стека
    не изменился, ответ тот же — считать его заново незачем. Разбор YAML цели (10 мс
    там же) — по той же причине и с тем же ключом: содержимое."""
    import config as config_mod
    import config_raw
    base, profiles = _stacked_layers(tmp_path, monkeypatch)
    base.write_text(_STACK_BASE, encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    scr = _mk_screen(config=_cfg(), config_path=str(base))
    calls, parses = [], []
    real = config_mod.validate_config_text_with_sections
    real_parse = config_raw.parse_document
    monkeypatch.setattr(config_mod, "validate_config_text_with_sections",
                        lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    monkeypatch.setattr(config_raw, "parse_document",
                        lambda *a, **kw: (parses.append(1), real_parse(*a, **kw))[1])
    for _ in range(5):
        scr._menu_pass_snapshot = None       # новый проход меню — как на каждое ↑/↓
        assert scr._models_glyph()[0] == "✓"
    assert len(calls) == 1, f"валидация стека прогонялась {len(calls)} раз(а)"
    assert len(parses) == 1, f"YAML цели разбирался {len(parses)} раз(а)"


def test_hub_models_glyph_never_serves_a_stale_verdict(tmp_path, monkeypatch):
    """Устаревший глиф хуже медленных стрелок: правка ЛЮБОГО файла стека — свой
    пересчёт. Проверяются оба слоя: и профиль (его правит под-вид «Профилей»), и
    цель записи самой секции."""
    base, profiles = _stacked_layers(tmp_path, monkeypatch)
    base.write_text(_STACK_BASE, encoding="utf-8")
    profile = profiles / "work.yml"
    profile.write_text('slots:\n  default: "a"\n', encoding="utf-8")
    scr = _mk_screen(config=_cfg(), config_path=str(base))
    assert scr._models_glyph()[0] == "✓"

    profile.write_text('slots:\n  default: "ghost"\n', encoding="utf-8")
    scr._menu_pass_snapshot = None
    assert scr._models_glyph()[0] == "!", "правка профиля не видна"

    base.write_text(_STACK_BASE + '  ghost:\n    model: "z"\n', encoding="utf-8")
    scr._menu_pass_snapshot = None
    assert scr._models_glyph()[0] == "✓", "правка цели записи не видна"


# ── «сейчас работает уровень…» — про сессию, а не про файл ─────────────────────


async def test_the_models_section_names_the_level_that_is_really_acting(tmp_path):
    """`/level` переключает уровень на сессию, в файл не записывая, а экран
    открывается с БАЗОЙ (`screen_config()` — без сессионных переопределений).
    Пикер привязок обещает «работает активным уровнем: сейчас …» — называть там
    записанное в файле значит называть не то, что работает."""
    import tui.app as app_mod
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("level: default\n"
                        "backend:\n"
                        '  completions_url: "http://host/v1/chat/completions"\n'
                        "models:\n  flash:\n    model: \"m\"\n", encoding="utf-8")
    cfg = {"completions_url": "http://host/v1/chat/completions", "level": "default"}
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        app._session_overrides["level"] = "max"      # /level посреди сессии
        scr = _mk_screen(config=cfg, config_path=str(cfg_file))
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Модели")
        await pilot.pause()
        assert scr._models_editor()._active_level == "max"


# --- level переехал из «Профилей» в «Режимы работы» отдельным блоком ---


def modes_row_index(name: str) -> int:
    """Позиция строки тоггла в секции «Режимы работы» — состав спрашивается там же,
    где его строит экран."""
    from tui.toggles import modes_section_names
    return modes_section_names().index(name)


def test_modes_section_ends_with_the_level_row():
    """`level` правит ГЛОБАЛЬНЫЙ `level:` конфига, к профилям он отношения не
    имеет — его дом здесь, рядом с равными себе ручками «как работаем».

    Снизу, под чертой (решение владельца): сначала «как работаем», потом «чем».
    Состав секции спрашивается у одной функции — секция больше не равна группе
    `"rigor"`, и знание о её композиции не размазано по шести местам."""
    from tui.toggles import modes_section_names

    names = modes_section_names()
    assert names[-1] == "level"
    assert names[:-1] == ["rigor", "gate", "answers"]


async def test_level_row_stands_behind_a_separator(tmp_path):
    """Отдельный блок = черта на смене группы. Правило родовое: разделитель
    ставится перед новым preset-родителем ИЛИ на смене `group` — старый
    2-group страж (обе группы preset, одна `group`) от этого не страдает."""
    pytest.importorskip("textual")

    app, _ = _app_with_config(tmp_path, 'level: "default"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)

        seps = list(scr.query(".set-group-sep"))
        rows = list(scr.query(".set-row"))
        assert len(seps) == 1
        assert len(rows) == 4
        assert rows[2].region.y < seps[0].region.y < rows[3].region.y


async def test_level_row_keeps_its_default_wording(tmp_path):
    """Голова строки — `level · уровень модели по умолчанию`. «По умолчанию»
    отличает файловое значение от сессионного `/level`, и в Настройках оно
    уместно ровно потому, что здесь правится файл. В палитре тот же тоггл
    крутит сессию — там это слово соврало бы, поэтому `spec.label` не трогаем:
    у секции своя подпись."""
    pytest.importorskip("textual")

    app, _ = _app_with_config(tmp_path, 'level: "max"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)

        text = scr._mode_row_text("level", selected=False).plain
        assert "level · уровень модели по умолчанию" in text
        assert text.rstrip().endswith("max")


async def test_modes_hint_uses_the_dynamic_provider(tmp_path, monkeypatch):
    """Подсказка строки берёт динамический `hint_provider`, если тоггл его
    объявил. У `level` значения статических подсказок не несут вовсе — их роль
    играет модель, резолвнутая роутером для этого уровня, — и без провайдера
    строка уровня осталась бы с пустой подсказкой (тот же приём, которым
    динамический хинт получает палитра: `dynamic_toggle_hint`)."""
    pytest.importorskip("textual")

    monkeypatch.chdir(tmp_path)
    app, _ = _app_with_config(
        tmp_path,
        'level: "default"\n'
        'models:\n  flash:\n    model: "google/gemini-2.5-flash"\n'
        'slots:\n  default: "flash"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        scr._modes_idx = modes_row_index("level")

        hint = scr._modes_hint_text()

        assert "flash" in hint            # модель за уровнем, а не пустота
        assert "основная рабочая" in hint  # роль уровня — канон копирайта


async def test_level_row_track_is_lit_in_the_colour_of_the_level(tmp_path, monkeypatch):
    """Страж переноса: горящая позиция трека окрашена по ТЕКУЩЕМУ уровню — тем же
    языком, что точка-индикатор футера. Свойство обеспечено общим `padded_track`,
    но именно ради него строка и переезжала целиком, а не по кусочкам."""
    pytest.importorskip("textual")
    from tui.toggles import TOGGLES

    monkeypatch.chdir(tmp_path)
    app, _ = _app_with_config(tmp_path, 'level: "max"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)

        text = scr._mode_row_text("level", selected=False)
        max_colour = next(v.color for v in TOGGLES["level"].values if v.value == "max")
        assert any(max_colour.lower() in str(s.style).lower() for s in text.spans)


# --- Правило записи секции: куда пишем и что действует ---


async def test_modes_write_goes_to_the_project_config_that_already_sets_the_key(
        tmp_path, monkeypatch):
    """Проектный `.gc/config.yml` уже задаёт этот ключ — пишем ТУДА.

    То же правило, что у `level` и указателя профиля: запись в базовый была бы
    молчаливым no-op, проектный слой всё равно перекроет. Предикат — по ПОЛНОМУ
    пути ключа: `deep_merge` рекурсивен, и наличие одного лишь `tui:` в проекте
    не перекрывает ничего."""
    pytest.importorskip("textual")
    import yaml

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    project_config = project_dir / ".gc"
    project_config.mkdir()
    project_config = project_config / "config.yml"
    project_config.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")

    app, base_cfg = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "off"\n  text_answer_mode: "allow"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        scr._modes_idx = modes_row_index("gate")

        scr._modes_apply(1)

        assert yaml.safe_load(project_config.read_text(encoding="utf-8"))["tui"][
            "finish_gate_mode"] != "on"
        assert yaml.safe_load(base_cfg.read_text(encoding="utf-8"))["tui"][
            "finish_gate_mode"] == "off"          # базовый не тронут


async def test_modes_write_goes_to_the_base_config_when_the_project_is_silent(
        tmp_path, monkeypatch):
    """Проектный конфиг есть, но про ЭТОТ ключ молчит — пишем в базовый.

    Правило именно «где ключ уже задан», а не «есть ли проектный файл»: он
    приезжает с клоном репозитория, и класть туда личную настройку значило бы
    править файл проекта за спиной команды."""
    pytest.importorskip("textual")
    import yaml

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    gc_dir = project_dir / ".gc"
    gc_dir.mkdir()
    project_config = gc_dir / "config.yml"
    project_config.write_text('profile: ""\n', encoding="utf-8")

    app, base_cfg = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "off"\n  text_answer_mode: "allow"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        scr._modes_idx = modes_row_index("gate")

        scr._modes_apply(1)

        assert yaml.safe_load(base_cfg.read_text(encoding="utf-8"))["tui"][
            "finish_gate_mode"] != "off"
        assert "tui" not in yaml.safe_load(project_config.read_text(encoding="utf-8"))


async def test_modes_write_says_what_actually_acts_when_a_layer_wins(tmp_path, monkeypatch):
    """Выбор файла не покрывает случай, когда ключ перекрыт ФАЙЛОМ ПРОФИЛЯ: в
    профиль секция не пишет вовсе. Значит после записи надо сверить действующее
    значение и сказать правду — иначе трек показывает выбранное, а работает
    другое (правило `level` до переезда, теперь общее для секции)."""
    pytest.importorskip("textual")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    # Профиль держит "on", а цикл от "off" даёт "smart" — значения РАЗВЕДЕНЫ:
    # совпади они, тест зеленел бы и без всякой сверки.
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text(
        'tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")

    app, _ = _app_with_config(
        tmp_path,
        'profile: "work"\ntui:\n  finish_gate_mode: "off"\n  text_answer_mode: "allow"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        scr._modes_idx = modes_row_index("gate")

        scr._modes_apply(1)
        await pilot.pause()          # нотис монтируется тактом позже

        notices = [str(w.render()) for w in scr.query("#set-notice")]
        # Слой НАЗЫВАЕТСЯ поимённо, а не гадается перечислением: «проектный конфиг
        # или профиль» врало бы в первой половине — проектный, задающий ключ,
        # перехватывает саму запись (правило выбора файла) и перекрыть её не может.
        assert any("действует" in n and "on" in n and "профилем work" in n
                   for n in notices), notices


async def test_modes_write_creates_a_missing_top_level_anchor(tmp_path, monkeypatch):
    """Конфиг без строки `level:` (создан до появления ключа) не запирает
    переключение: якорь дописывается из пакетного шаблона.

    Правило родовое, не про `level`: топ-уровневый скаляр `write_values` сам
    дописать не умеет — его фоллбэк на шаблон работает только для dotted-ключей.
    Переехало сюда вместе со строкой уровня."""
    pytest.importorskip("textual")

    monkeypatch.chdir(tmp_path)
    app, base_cfg = _app_with_config(
        tmp_path,
        'backend:\n  completions_url: "http://host/v1/chat/completions"\n'
        'agent:\n  model: "test-model"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        scr._modes_idx = modes_row_index("level")

        scr._modes_apply(1)
        await pilot.pause()

        assert 'level: "max"' in base_cfg.read_text(encoding="utf-8")


async def test_modes_write_flashes_the_file_it_wrote_to(tmp_path, monkeypatch):
    """Плашка называет ФАЙЛ: теперь запись уходит не всегда в один и тот же
    конфиг (проектный перехватывает ключ, который сам задаёт), и «сохранено» без
    имени файла оставляло бы человека гадать, где искать результат."""
    pytest.importorskip("textual")

    monkeypatch.chdir(tmp_path)
    app, base_cfg = _app_with_config(
        tmp_path, 'level: "default"\ntui:\n  finish_gate_mode: "off"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        scr._modes_idx = modes_row_index("level")

        scr._modes_apply(1)
        await pilot.pause()

        flashes = [str(w.render()) for w in scr.query("#set-flash")]
        assert any(str(base_cfg) in f for f in flashes), flashes


async def test_modes_flash_names_the_file_absolutely(tmp_path, monkeypatch):
    """Путь во флеше — абсолютный, каким бы ни был `--config`.

    Живой смоук показал разнобой: проектная цель резолвится в полный путь, а
    базовая печаталась ровно так, как её передали относительным `--config`, — и
    две соседние плашки одной секции называли файл по-разному. Отвечают они на
    один вопрос «куда легло», значит и отвечать обязаны одинаково."""
    pytest.importorskip("textual")

    monkeypatch.chdir(tmp_path)
    app, base_cfg = _app_with_config(tmp_path, 'level: "default"\n')
    app._config_path = "./config.yml"          # так его передаёт относительный --config
    async with app.run_test() as pilot:
        scr = _mk_screen(config=app._config, config_path="./config.yml",
                         on_apply=app.reload_config)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Режимы работы")
        await pilot.pause(0.1)
        scr._modes_idx = modes_row_index("level")

        scr._modes_apply(1)
        await pilot.pause()

        flashes = [str(w.render()) for w in scr.query("#set-flash")]
        assert any(str(base_cfg.resolve()) in f for f in flashes), flashes


async def test_modes_write_leaves_the_file_untouched_when_it_fails(tmp_path, monkeypatch):
    """«✗ не сохранено» обязано означать нетронутый файл — как и обещает комментарий
    рядом («запись всё или ничего»).

    Якорь топ-уровневого ключа дописывается ОТДЕЛЬНОЙ записью и валидации не знает:
    если следом `write_values` отвергнет итог, на диске уже лежит `level:` со
    ЗНАЧЕНИЕМ ИЗ ШАБЛОНА — действующий уровень молча изменился, а человеку сказали,
    что не записано. Ровно тот класс лжи, что закрыт для мастера в `8346f00b`.

    Невалидность наводится ИЗВНЕ после старта — так этот путь и достижим: конфиг
    правят руками, пока окно открыто."""
    pytest.importorskip("textual")

    monkeypatch.chdir(tmp_path)
    app, base_cfg = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "off"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        scr._modes_idx = modes_row_index("level")
        # правка руками, пока окно открыто: ключ вне схемы (`extra="forbid"`)
        base_cfg.write_text(base_cfg.read_text(encoding="utf-8") + "ключ_вне_схемы: 1\n",
                            encoding="utf-8")
        before = base_cfg.read_text(encoding="utf-8")

        scr._modes_apply(1)
        await pilot.pause()

        assert base_cfg.read_text(encoding="utf-8") == before
        notices = [str(w.render()) for w in scr.query("#set-notice")]
        assert any("не сохранено" in n for n in notices), notices


async def test_overriding_layer_names_the_key_that_actually_diverged(tmp_path, monkeypatch):
    """У пары пресета перекрытыми могут оказаться ОБА ключа — разными слоями.
    Называть надо тот, чьё действующее значение разошлось с записанным, а не
    первый попавшийся: иначе человека посылают в файл, который запись как раз
    перехватил и перекрыть уже не мог.

    Здесь проект задаёт `finish_gate_mode` (туда пара и уходит целиком), а
    профиль — `text_answer_mode`; разошёлся именно второй, его слой и назван."""
    pytest.importorskip("textual")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    gc_dir = project_dir / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text(
        'tui:\n  text_answer_mode: "deny"\n', encoding="utf-8")

    app, _ = _app_with_config(
        tmp_path,
        'profile: "work"\ntui:\n  finish_gate_mode: "off"\n  text_answer_mode: "allow"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)

        name = scr._overriding_layer_name(
            {"tui.finish_gate_mode": "on", "tui.text_answer_mode": "allow"})

        assert "профилем work" in name, name


async def test_modes_write_to_the_project_config_survives_a_packaged_base(
        tmp_path, monkeypatch):
    """Запуск на пакетном шаблоне (GC_HOME не настроен) не запрещает запись в
    ПРОЕКТНЫЙ конфиг: он территория пользователя и писуч всегда.

    Гейт остался один — по ЦЕЛИ (`_target_writable`), как у соседних записей
    «Профилей». Прежняя предпроверка по пути запуска отказывала раньше него и
    противоречила новому правилу выбора файла."""
    pytest.importorskip("textual")
    import yaml
    import config as config_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    gc_dir = project_dir / ".gc"
    gc_dir.mkdir()
    project_config = gc_dir / "config.yml"
    project_config.write_text('tui:\n  finish_gate_mode: "off"\n', encoding="utf-8")

    packaged = tmp_path / "packaged" / "config.default.yml"
    packaged.parent.mkdir()
    packaged.write_text('tui:\n  finish_gate_mode: "off"\n  text_answer_mode: "allow"\n',
                        encoding="utf-8")
    monkeypatch.setattr(config_mod, "_default_config_path", lambda: str(packaged))

    app, _ = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "off"\n  text_answer_mode: "allow"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes_section(pilot)
        scr._config_path = str(packaged)          # база — пакетный шаблон
        scr._modes_idx = modes_row_index("gate")

        scr._modes_apply(1)
        await pilot.pause()

        assert yaml.safe_load(project_config.read_text(encoding="utf-8"))["tui"][
            "finish_gate_mode"] != "off"          # в проектный писать можно
        assert 'finish_gate_mode: "off"' in packaged.read_text(encoding="utf-8")
