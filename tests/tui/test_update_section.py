import pytest
pytest.importorskip("textual")
from textual.widgets import Static
from tests.tui.conftest import open_settings_section, settings_section_index


async def test_update_section_shows_actions_and_hint(tmp_path, monkeypatch):
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://gitverse.ru/x/y", "master", "abc", False))
    monkeypatch.setattr(app_version, "agent_version", lambda: "0.3.0")
    # вход в секцию авто-проверяет → мок без сети
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: check_mod.CheckResult("current", "abc", "установлена последняя версия"))
    cfg_file = tmp_path / "config.yml"; cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(".set-dyn"))
        assert "Обновить агента" in body
        assert "Проверить обновления" in body
        assert "Обновить config.yml" in body
        assert "gitverse.ru/x/y" in body


async def test_update_section_auto_checks_on_entry_and_cursor_on_top(tmp_path, monkeypatch):
    """Вход в секцию сам запускает проверку (статус, не «не проверялось»); курсор по
    умолчанию — на верхнем «Обновить агента»; повторный вход не долбит сеть (кэш)."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    from gc_core.updater.check import CheckResult
    calls = []
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))

    def _fake_check(source, **k):
        calls.append(source)
        return CheckResult("update", "new", "доступна новая версия")

    monkeypatch.setattr(check_mod, "check_for_update", _fake_check)
    cfg_file = tmp_path / "c.yml"; cfg_file.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление")
        await pilot.pause(); await pilot.pause()
        assert calls                                   # авто-проверка сработала на входе
        assert scr._update_idx == 0                    # курсор на верхнем действии
        body = " ".join(str(w.render()) for w in scr.query(".set-dyn"))
        assert "доступна новая версия" in body
        n = len(calls)
        open_settings_section(scr, "Обновление"); await pilot.pause()       # повторный вход
        assert len(calls) == n                          # без нового запроса (кэш)


async def test_check_updates_sets_status(tmp_path, monkeypatch):
    from gc_core import app_version; from gc_tui import app as app_mod
    from gc_core.updater import check as check_mod
    from gc_tui.screens.settings import SettingsScreen
    from gc_core.updater.check import CheckResult
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: CheckResult("update", "new", "доступна новая версия"))
    cfg_file = tmp_path / "c.yml"; cfg_file.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause()
        scr._check_updates_now()                          # синхронный вход в воркер (инъекция)
        await pilot.pause(); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(".set-dyn"))
        assert "доступна новая версия" in body or "Обновить агента" in body


async def test_update_confirm_and_hint_text_use_location_phrase(tmp_path, monkeypatch):
    """I4: confirm-баннер и подсказка обновления берут формулировку места установки из
    ЕДИНОГО владельца (updater.runner.install_location_phrase), а не хардкодят
    «в отдельном окне» — на POSIX там просто нет никакого окна (spawn без консоли)."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_core.updater import runner as runner_mod
    from gc_tui.screens.settings import SettingsScreen
    monkeypatch.setattr(runner_mod, "install_location_phrase", lambda: "в фоне")
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: check_mod.CheckResult("update", "new", "доступна новая версия"))
    cfg_file = tmp_path / "c.yml"; cfg_file.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause(); await pilot.pause()
        hint_body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "в фоне" in hint_body and "в отдельном окне" not in hint_body
        scr._request_update()
        confirm_body = " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        assert "в фоне" in confirm_body and "в отдельном окне" not in confirm_body


async def test_update_confirm_banner_then_spawn(tmp_path, monkeypatch):
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_core.updater import runner as runner_mod
    from gc_tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",           # вход авто-проверяет → без сети
                        lambda source, **k: check_mod.CheckResult("update", "new", "доступна новая версия"))
    spawned = {}
    monkeypatch.setattr(runner_mod, "spawn_detached_update",
                        lambda command, **k: spawned.update(cmd=command, kw=k))
    cfg_file = tmp_path / "c.yml"; cfg_file.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause()
        scr._request_update()                             # показать confirm
        assert scr._update_awaiting_confirm is True
        body = " ".join(str(w.render()) for w in scr.query(".set-dyn"))
        assert "закроется" in body and "Enter — обновить" in body
        scr._confirm_update()                             # подтвердить
        assert "cmd" in spawned                           # спавн вызван


async def test_update_confirm_banner_blocks_section_switch(tmp_path, monkeypatch):
    """Регресс гейта (тот же Critical, что в MCP): пока висит confirm-баннер обновления,
    PgUp/PgDn через on_key НЕ листает секцию и НЕ уничтожает баннер; escape — отменяет
    confirm, а не секцию. Ранний гейт секции «Обновление» (до pageup-ветки) глотает всё,
    кроме enter (→ confirm) и escape (→ cancel)."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",           # вход авто-проверяет → без сети
                        lambda source, **k: check_mod.CheckResult("update", "new", "доступна новая версия"))
    cfg_file = tmp_path / "c.yml"; cfg_file.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause()
        scr._request_update()                             # поднять confirm-баннер
        await pilot.pause()
        assert scr._update_awaiting_confirm is True
        await pilot.press("pageup")                       # попытка листнуть секцию
        await pilot.pause()
        assert scr._view == settings_section_index("Обновление")                             # секция НЕ сменилась
        assert scr._update_awaiting_confirm is True       # баннер жив
        assert list(scr.query(".set-confirm"))            # виджет баннера на месте
        await pilot.press("pagedown")                     # и в другую сторону
        await pilot.pause()
        assert scr._view == settings_section_index("Обновление")
        assert scr._update_awaiting_confirm is True
        await pilot.press("escape")                       # отмена confirm
        await pilot.pause()
        assert scr._update_awaiting_confirm is False
        assert scr._view == settings_section_index("Обновление")                             # escape отменил confirm, не секцию


async def test_update_blocked_when_current(tmp_path, monkeypatch):
    """При актуальной версии Enter на «Обновить агента» не поднимает confirm,
    а даёт title-note о том, что уже установлена последняя версия."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    from gc_core.updater.check import CheckResult
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: CheckResult("current", "old", "установлена последняя версия"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None, probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause(); await pilot.pause()
        scr._request_update()
        assert scr._update_awaiting_confirm is False              # confirm НЕ поднят
        title = str(scr.query_one("#set-title").render())
        assert "последняя версия" in title                       # title-note о том, что уже актуально


async def test_apply_check_result_persists_even_if_view_changed(tmp_path, monkeypatch):
    """I1: воркер check_for_update прилетает ПОСЛЕ того как юзер ушёл из секции
    «Обновление» (Esc/PgUp) — результат всё равно сохраняется и _update_checking
    сбрасывается, иначе секция залипает на «проверяю…» навсегда (гейт авто-чека в
    _open_section больше не срабатывает, т.к. _update_checking никогда не станет False)."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    from gc_core.updater.check import CheckResult
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",           # вход авто-проверяет → без сети
                        lambda source, **k: CheckResult("current", "old", "установлена последняя версия"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause()
        scr._update_checking = True                      # воркер в полёте
        scr._view = 0                                     # юзер успел уйти из секции «Обновление»
        scr._apply_check_result(CheckResult("current", "old", "установлена последняя версия"))
        assert scr._update_checking is False              # НЕ залипло
        assert scr._update_check_result is not None       # результат сохранён


async def test_apply_check_result_persists_verdict_and_syncs_main_card(tmp_path, monkeypatch):
    """I2: ручная проверка обновлений в /settings должна персистить вердикт (тем же
    record_check_result, что и стартовый авто-чек) и обновлять app._update_available +
    карточку главного экрана — иначе секция и карточка/`/notices` расходятся, а
    ручная находка не переживает перезапуск."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_core import upgrade_gate
    from gc_tui.screens.settings import SettingsScreen
    state_path = tmp_path / "agent_state.json"
    monkeypatch.setattr(upgrade_gate, "_state_path", lambda: state_path)
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",           # вход авто-проверяет → без сети
                        lambda source, **k: check_mod.CheckResult("current", "old", "установлена последняя версия"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause(); await pilot.pause()
        scr._apply_check_result(check_mod.CheckResult("update", "newcommit", "доступна новая версия"))
        assert app._update_available is True
        state = upgrade_gate.read_state()
        assert state.get("last_update_remote_commit") == "newcommit"
        assert state.get("last_update_checked_installed") == "old"
        main = app._main_screen()
        card = str(main._mcp_status_row.render())
        assert "↑ доступна новая версия" in card


async def test_apply_check_result_unknown_keeps_known_update_verdict(tmp_path, monkeypatch):
    """Ре-ревью: транзиентный unknown (офлайн/флапающий прокси) НЕ должен гасить уже
    известное «доступно обновление» — record_check_result намеренно сохраняет прошлый
    вердикт при unknown, а app._update_available обязан пере-выводиться из персиста
    (update_available_from_state), а не из статуса текущего результата напрямую."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_core import upgrade_gate
    from gc_tui.screens.settings import SettingsScreen
    state_path = tmp_path / "agent_state.json"
    monkeypatch.setattr(upgrade_gate, "_state_path", lambda: state_path)
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",           # вход авто-проверяет → без сети
                        lambda source, **k: check_mod.CheckResult("current", "old", "установлена последняя версия"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause(); await pilot.pause()
        scr._apply_check_result(check_mod.CheckResult("update", "newcommit", "доступна новая версия"))
        assert app._update_available is True
        scr._apply_check_result(check_mod.CheckResult("unknown", None, "не удалось: офлайн"))
        assert app._update_available is True               # флаг выжил
        main = app._main_screen()
        card = str(main._mcp_status_row.render())
        assert "↑ доступна новая версия" in card            # строка в карточке выжила


async def test_apply_check_result_current_clears_available(tmp_path, monkeypatch):
    """Не сломать существующее поведение: настоящий «current»-вердикт должен опускать
    app._update_available в False (в отличие от unknown)."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_core import upgrade_gate
    from gc_tui.screens.settings import SettingsScreen
    state_path = tmp_path / "agent_state.json"
    monkeypatch.setattr(upgrade_gate, "_state_path", lambda: state_path)
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",           # вход авто-проверяет → без сети
                        lambda source, **k: check_mod.CheckResult("current", "old", "установлена последняя версия"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause(); await pilot.pause()
        scr._apply_check_result(check_mod.CheckResult("update", "newcommit", "доступна новая версия"))
        assert app._update_available is True
        scr._apply_check_result(check_mod.CheckResult("current", "old", "установлена последняя версия"))
        assert app._update_available is False


async def test_checked_line_unknown_is_dash_not_warn(tmp_path, monkeypatch):
    """v3: «не удалось проверить» — нет вердикта (— dim), а не проблема (⚠)."""
    from gc_core import app_version; from gc_core.updater import check as check_mod; from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    from gc_core.updater.check import CheckResult
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: CheckResult("unknown", None, "нет соединения"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление"); await pilot.pause(); await pilot.pause()
        line = str(scr._checked_line_text())
        assert line.startswith("Проверено") and "—" in line and "⚠" not in line
        glyph, _ = scr._menu_glyph(settings_section_index("Обновление"))
        assert glyph == "—"                                # меню-глиф тоже без вердикта, не !


async def test_update_confirm_warns_when_agent_running(tmp_path, monkeypatch):
    """Confirm-баннер обновления: при живом ходе агента — явное «прервёт выполнение»;
    в простое — обычное «агент закроется». Одна ветка формулировки, не новая логика."""
    from gc_core import app_version; from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://gitverse.ru/x/y", "master", "abc", False))

    class _Runner:
        def __init__(self, running): self._r = running
        def is_running(self): return self._r

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()

        app.active_runner = _Runner(True)
        assert "Идёт выполнение" in scr._update_confirm_text()
        assert "прервёт" in scr._update_confirm_text()

        app.active_runner = _Runner(False)
        assert "Идёт выполнение" not in scr._update_confirm_text()
        assert "Агент закроется" in scr._update_confirm_text()

        app.active_runner = None            # раннера нет вовсе — тоже «агент закроется»
        assert "Агент закроется" in scr._update_confirm_text()
