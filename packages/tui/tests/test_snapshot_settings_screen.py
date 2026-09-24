"""/settings — хаб настроек и секция «Подключение» (визуальный контракт §2-3, Р19).

Два состояния, ради которых снимки и заведены:

* **хаб** — содержимое короткое: рамка обнимает меню, полоса подсказок стоит
  сразу под её нижней гранью;
* **«Подключение» на низком окне** — карточка в кадр не влезает: рамка упирается
  в потолок (`ScrollFrameMixin._sync_frame_cap`), скроллбар идёт ВНУТРИ рамки, а
  футер — ровно под гранью. Это и есть Р19: до него рамка росла вместе с
  содержимым, нижней грани не было вовсе, а футер уезжал за кадр.

Всё, что поедет само (версия, каталог `~/.gc`, список подключённых MCP, сеть
probe и авто-чек обновления), прибито фикстурами — иначе снимок флапал бы от
машины к машине.
"""
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_testkit.tui_support import wait_until

_VERSION = "0.4.2"                                        # версия в шапке — фикстурой
_MODEL = "CodeAgent"
_URL = "https://api.example-host/v1/chat/completions"
_TOKEN = "Bearer sk-demo-token"


def _config() -> dict:
    """База, как её видит приложение: `load_config` отдаёт backend/agent плоско."""
    return {
        "completions_url": _URL,
        "chat_completion_headers": {"Authorization": _TOKEN},
        "model": _MODEL,
    }


def _write_config_file(tmp_path):
    """Настоящий config.yml.

    Значения строк меню экран берёт ИЗ ФАЙЛА (`SettingsScreen._read_raw`), а не
    из переданной базы: фикстура, писавшая мимо файла, показала бы «—» в каждой
    строке — и снимок сторожил бы пустоту вместо меню.
    """
    p = tmp_path / "config.yml"
    p.write_text(
        "backend:\n"
        f'  completions_url: "{_URL}"\n'
        "  chat_completion_headers:\n"
        f'    Authorization: "{_TOKEN}"\n'
        "agent:\n"
        f'  model: "{_MODEL}"\n',
        encoding="utf-8")
    return p


def _probe(endpoint, auth, verify, model, **kw):
    """Probe без сети, с тем же differential-контрактом, что у живого
    (`settings_probe.probe` + `interpret_auth`): запрос БЕЗ токена — 401,
    с токеном — ok со списком моделей. Вердикт секции → «сервер доступен ·
    токен принят», глифы хаба → ✓/✓."""
    from gc_core.model_picker import ModelInfo
    from gc_tui.services.settings_probe import ProbeResult
    if auth is None:
        return ProbeResult("rejected", 401, None, 40, "токен отклонён (401)")
    return ProbeResult("ok", 200, [ModelInfo(id=_MODEL), ModelInfo(id="CodeChat")],
                       118, "доступен")


def _app(tmp_path, monkeypatch):
    """Приложение с открытым конфигом и отвязанное от машины."""
    from gc_core import app_version
    import recommended_mcp.runtime.cli as mcp_cli
    from gc_core.config_modules import env_vars
    from gc_tui.app import TuiApp

    monkeypatch.setattr(app_version, "agent_version", lambda: _VERSION)
    # GC_HOME → tmp: оттуда экран берёт managed-оверлей (подпись «перекрыто
    # ~/.gc/config.local.yml») и список подключённых MCP — реальный каталог
    # пользователя сделал бы снимок машинно-зависимым.
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "gc"))
    monkeypatch.setattr(mcp_cli, "wired_names", lambda config=None: ["office", "duckduckgo"])
    # Стартовый авто-чек обновления — сеть и гонка «…»/«—» в глифе строки
    # «Обновление»: снимок ловил бы то один кадр, то другой.
    monkeypatch.setattr(TuiApp, "_start_auto_update_check", lambda self: None)

    cfg_file = _write_config_file(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_config())
    app.set_config_path(str(cfg_file))
    return app, cfg_file


def _push_settings(pilot, cfg_file):
    """Экран — теми же аргументами, что и `/settings` в проде
    (`TraceScreen._cmd_settings`); инъекция probe_fn — штатный шов экрана."""
    from gc_tui.screens.settings import SettingsScreen
    scr = SettingsScreen(config=pilot.app.screen_config(), config_path=str(cfg_file),
                         on_apply=pilot.app.reload_config, probe_fn=_probe)
    pilot.app.push_screen(scr)
    return scr


def test_settings_menu_snapshot(snap_compare, tmp_path, monkeypatch):
    """Хаб: пять секций со значениями и осевшими глифами, рамка по содержимому."""
    app, cfg_file = _app(tmp_path, monkeypatch)

    async def setup(pilot):
        await pilot.pause()
        scr = _push_settings(pilot, cfg_file)
        # Пока config-probe летит, глифы редакторных секций — бегущая волна «···»:
        # без ожидания снимок фиксировал бы случайный кадр анимации.
        await wait_until(pilot, lambda: not scr._probe_in_flight, "config probe settled")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(100, 30), run_before=setup)


def test_settings_connection_low_snapshot(snap_compare, tmp_path, monkeypatch):
    """Секция «Подключение» на низком окне — Р19: рамка упёрлась в потолок,
    скроллбар внутри неё, футер под нижней гранью. В кадре — поле под курсором:
    вердикт автопроверки входа кадр не двигает (его подводит только ручной запуск)."""
    app, cfg_file = _app(tmp_path, monkeypatch)

    async def setup(pilot):
        await pilot.pause()
        scr = _push_settings(pilot, cfg_file)
        await wait_until(pilot, lambda: not scr._probe_in_flight, "config probe settled")
        await pilot.press("enter")          # курсор меню на «Подключение» — как в проде
        # Вход в секцию сам запускает differential-проверку; её вердикт —
        # последняя строка карточки и остаётся под сгибом: автопроверку человек
        # не запускал, он смотрит на поле. Ждём вердикт, чтобы кадр был
        # окончательным, а не «проверяю…».
        await wait_until(pilot, lambda: scr._draft_checked is not None,
                         "connection probe verdict")
        await pilot.pause()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(100, 12), run_before=setup)
