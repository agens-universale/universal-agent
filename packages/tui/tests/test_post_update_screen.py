import pytest
pytest.importorskip("textual")
from textual.widgets import Static


class _Upg:  # UpgradeInfo-подобный
    old_version = "0.3.0"; new_version = "0.4.0"


def _states(monkeypatch, updates: dict):
    """installed_state-заглушка: {имя: версия-обновления|None}.

    Заодно гасит пункт голосового движка: он смотрит в РЕАЛЬНЫЙ state.json машины, и на
    стенде с отставшей установкой всплыл бы сам — сдвинул бы индексы строк, а на Enter
    запустил бы настоящую переустановку speaches прямо из тестов."""
    from recommended_mcp.runtime import cli as cli
    from gc_tui.voice_runtime import state as voice_state
    monkeypatch.setattr(voice_state, "load_state", lambda: None)
    monkeypatch.setattr(cli, "load_manifest_entries", lambda config=None: ([], None))
    monkeypatch.setattr(cli, "installed_state",
                        lambda name, config=None, manifest=None:
                        {"installed": True, "version": "0.1.0", "update": updates.get(name)})


async def test_post_update_lists_checkboxes(monkeypatch):
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office", "ddg"])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: True)
    _states(monkeypatch, {"office": "0.3.1", "ddg": "0.2.0"})
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause(); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "Агент обновлён: 0.3.0 → 0.4.0" in body
        assert "[●] Обновить подключённые MCP" in body
        assert "office" in body and "ddg" in body
        assert "[●] Обновить config.yml" in body


async def test_post_update_hides_mcp_when_nothing_to_update(monkeypatch):
    """MCP подключены, но их версии совпадают с манифестом новой версии агента →
    пункт не предлагаем (как с config.yml: нечего делать — нечего показывать)."""
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office", "ddg"])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: True)
    _states(monkeypatch, {})                       # ни у кого нет обновления
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause(); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "Обновить подключённые MCP" not in body
        assert "[●] Обновить config.yml" in body


async def test_post_update_mcp_row_lists_only_outdated(monkeypatch):
    """В строке — только серверы, которым правда есть что обновить."""
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office", "ddg"])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    _states(monkeypatch, {"office": "0.3.1"})      # обновление только у office
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause(); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "office" in body and "ddg" not in body
        assert scr._mcp_outdated == ["office"]


async def test_post_update_hides_config_when_nothing_to_sync(monkeypatch):
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "Обновить config.yml" not in body          # нечего синхронизировать → скрыт


class _VoiceState:
    def __init__(self, version):
        self.version = version
        self.root = "/data/voice/speaches"
        self.port = 8735
        self.model = "Systran/faster-whisper-small"


def _voice(monkeypatch, *, installed: str | None, pin: str = "v0.9.0"):
    """Локальный движок версии `installed` (None — не установлен) при пине `pin`."""
    from gc_tui.voice_runtime import state as voice_state
    monkeypatch.setattr(voice_state, "SPEACHES_PIN", pin)
    monkeypatch.setattr(voice_state, "load_state",
                        lambda: _VoiceState(installed) if installed else None)


async def test_post_update_offers_voice_engine_when_behind(monkeypatch):
    """Движок обновляется только отсюда: `gc-voice install` руками никто не гоняет."""
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    _voice(monkeypatch, installed="v0.8.3")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "[●] Обновить голосовой движок" in body     # отмечен по умолчанию
        assert "v0.8.3 → v0.9.0" in body


async def test_post_update_hides_voice_when_at_pin_or_absent(monkeypatch):
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        _voice(monkeypatch, installed="v0.9.0")            # уже на пине
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        assert "голосовой движок" not in \
            " ".join(str(w.render()) for w in scr.query(Static))
        _voice(monkeypatch, installed=None)                # не установлен вовсе
        scr2 = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        assert scr2._voice_update is None


async def test_post_update_enter_updates_voice_engine(monkeypatch):
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_core import upgrade_gate; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    from gc_tui.services import voice_setup
    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    monkeypatch.setattr(upgrade_gate, "mark_current_run", lambda *a, **k: None)
    _voice(monkeypatch, installed="v0.8.3")
    updated = []
    monkeypatch.setattr(voice_setup, "update_local",
                        lambda config, echo=print: updated.append(config) or True)
    cfg = {"voice": {"url": "http://127.0.0.1:8735/v1/audio/transcriptions"}}
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config=cfg, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert updated == [cfg]        # конфиг доезжает: им поднимают сервис обратно


async def test_post_update_space_unmarks_then_enter_skips_it(monkeypatch):
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_core import upgrade_gate; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office"])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    _states(monkeypatch, {"office": "0.3.1"})
    installed = []
    monkeypatch.setattr(cli, "install", lambda name: installed.append(name) or 0)
    stamped = []
    monkeypatch.setattr(upgrade_gate, "mark_current_run", lambda *a, **k: stamped.append(True))
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause(); await pilot.pause()
        assert "mcp" in scr._marked
        await pilot.press("space")           # снять отметку с единственного пункта
        assert "mcp" not in scr._marked
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "[ ] Обновить подключённые MCP" in body
        await pilot.press("enter")           # выполнить (пункт снят — install не зовём)
        await pilot.pause()
        assert installed == []
        assert stamped == [True]             # стамп — после закрытия


async def test_post_update_enter_installs_marked_and_stamps_once(monkeypatch):
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_core import upgrade_gate; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office", "ddg"])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: True)
    _states(monkeypatch, {"office": "0.3.1", "ddg": "0.2.0"})
    installed = []
    synced = []

    class _FakeReport:
        transferred = 3

    def _fake_sync(path, **k):
        synced.append(path)
        return _FakeReport()

    monkeypatch.setattr(cli, "install", lambda name: installed.append(name) or 0)
    monkeypatch.setattr(config_sync, "sync_config", _fake_sync)
    stamped = []
    monkeypatch.setattr(upgrade_gate, "mark_current_run", lambda *a, **k: stamped.append(True))
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause(); await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert installed == ["office", "ddg"]
        assert synced == ["c.yml"]
        assert stamped == [True]


async def test_post_update_esc_stamps_without_executing(monkeypatch):
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_core import upgrade_gate; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office"])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: True)
    installed = []
    synced = []
    monkeypatch.setattr(cli, "install", lambda name: installed.append(name) or 0)
    monkeypatch.setattr(config_sync, "sync_config", lambda path, **k: synced.append(path))
    stamped = []
    monkeypatch.setattr(upgrade_gate, "mark_current_run", lambda *a, **k: stamped.append(True))
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        await pilot.press("escape")          # позже — ничего не выполняет
        await pilot.pause()
        assert installed == [] and synced == []
        assert stamped == [True]             # но стамп пишется


async def test_post_update_nothing_to_do_shows_only_title(monkeypatch):
    """Обновлять нечего → блок «Осталось обновить» не показываем вовсе (ни подзаголовка,
    ни «всё уже актуально»), футер честный: отмечать нечего."""
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office"])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    _states(monkeypatch, {})                       # у MCP тоже нечего обновлять
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause(); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "Агент обновлён: 0.3.0 → 0.4.0" in body
        assert "Осталось обновить" not in body
        assert "всё уже актуально" not in body
        assert "Enter или Esc — продолжить" in body
        assert "Space — отметить" not in body


async def test_post_update_hint_and_footer_when_there_is_work(monkeypatch):
    """Есть что обновить → подзаголовок «Осталось обновить:» и полный футер."""
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: True)
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "Осталось обновить:" in body
        assert "Довести машину" not in body
        assert "Space — отметить" in body


async def test_post_update_equal_version_title_is_plain(monkeypatch):
    """Номер версии не изменился → просто «Агент обновлён», без «до актуальной версии»."""
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen

    class _SameVer:
        old_version = "0.0.1"; new_version = "0.0.1"

    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_SameVer(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "✓ Агент обновлён" in body
        assert "до актуальной версии" not in body


async def test_post_update_title_always_carries_version(monkeypatch):
    """Версия видна всегда: сдвига не было → «Агент обновлён · v0.0.1»."""
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen

    class _SameVer:
        old_version = "0.0.1"; new_version = "0.0.1"

    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_SameVer(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "✓ Агент обновлён · v0.0.1" in body
        assert "0.0.1 → 0.0.1" not in body


async def test_post_update_title_divider_only_when_there_is_work(monkeypatch):
    """Линия под заголовком отделяет заголовок ОТ блока — без блока она не висит."""
    from recommended_mcp.runtime import cli as cli; from gc_core import config_sync; from gc_tui import app as app_mod
    from gc_tui.screens.post_update import PostUpdateScreen
    monkeypatch.setattr(cli, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr); await pilot.pause()
        title = scr.query_one(".pu-title")
        assert title.styles.border_bottom[0] in (None, "", "none")   # пусто → без линии

    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: True)
    app2 = app_mod.TuiApp(config={})
    async with app2.run_test() as pilot:
        scr2 = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app2.push_screen(scr2); await pilot.pause()
        title2 = scr2.query_one(".pu-title")
        assert title2.styles.border_bottom[0] == "solid"             # есть блок → линия
