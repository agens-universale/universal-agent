import pytest


def _screen(**over):
    from gc_tui.screens.onboarding import OnboardingScreen
    cfg = {"completions_url": "http://your-backend:PORT/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
           "model": "<ВПИШИТЕ_МОДЕЛЬ>"}
    kw = dict(config=cfg, config_path="cfg.yml", review_all=False,
              on_commit=lambda: None, fetch=lambda *a, **k: None,
              mcp=_FakeMcp())
    kw.update(over)
    return OnboardingScreen(**kw)


class _FakeMcp:
    def installable_names(self): return ["duckduckgo", "office"]
    def wired_names(self): return []
    def descriptions(self): return {}
    def install(self, name=None): return 0


def test_summary_hides_template_sentinels():
    """Шаблонные заглушки конфига (URL/MODEL_SENTINEL) в верхней сводке — прочерк,
    а не «your-backend:PORT» / «<ВПИШИТЕ_МОДЕЛЬ>» как заданное значение."""
    scr = _screen()                       # config = все три ..._SENTINEL
    line = scr._summary_text()
    assert "your-backend" not in line and "ВПИШИТЕ" not in line
    assert "сервер —" in line and "модель —" in line


@pytest.mark.asyncio
async def test_endpoint_restored_on_return(tmp_path):
    # ввёл адрес → далее → PgUp назад: значение восстановлено, а не пустое
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr.query_one("#onb-endpoint", Input).value = "https://h/v1/chat/completions"
        await pilot.press("enter")           # → шаг авторизации
        await pilot.pause()
        assert scr._step == 1
        await pilot.press("pageup")          # PgUp назад на адрес
        await pilot.pause()
        assert scr._step == 0
        assert scr.query_one("#onb-endpoint", Input).value == "https://h/v1/chat/completions"


@pytest.mark.asyncio
async def test_token_restored_on_return(tmp_path):
    # ввёл токен → далее → PgUp назад: поле снова заполнено (масккировано), значение цело
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 1
        scr._render_step()
        await pilot.pause()
        scr.query_one("#onb-token", Input).value = "sk-secret"
        await pilot.press("enter")           # submit токена → шаг модели
        await pilot.pause()
        assert scr._values["Authorization"] == "Bearer sk-secret"
        assert scr._step == 2
        await pilot.press("pageup")          # PgUp назад на авторизацию
        await pilot.pause()
        assert scr._step == 1
        assert scr.query_one("#onb-token", Input).value == "sk-secret"


@pytest.mark.asyncio
async def test_token_not_cleared_on_skip_auth(tmp_path):
    # ввёл токен → далее → PgUp назад → выбрал «без авторизации» → далее → PgUp назад:
    # поле #onb-token всё ещё содержит исходный токен (не затёрт пустым выбором)
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 1
        scr._render_step()
        await pilot.pause()
        # Ввёл токен на шаге авторизации
        scr.query_one("#onb-token", Input).value = "sk-secret"
        await pilot.press("enter")           # submit токена → шаг модели
        await pilot.pause()
        assert scr._values["Authorization"] == "Bearer sk-secret"
        assert scr._step == 2
        # Вернулись на авторизацию
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step == 1
        assert scr.query_one("#onb-token", Input).value == "sk-secret"
        # Выбрали «без авторизации» (пустой raw)
        await pilot.press("down")           # выбор опции
        await pilot.pause()
        await pilot.press("enter")          # submit пустого выбора
        await pilot.pause()
        assert scr._step == 2
        # Вернулись — токен всё ещё в поле
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step == 1
        assert scr.query_one("#onb-token", Input).value == "sk-secret"


async def _final_blob(pilot, scr, *, mcp):
    scr._values = {"completions_url": "https://h/v1", "Authorization": "Bearer sk", "model": "m"}
    scr._mcp_selected = set(mcp)
    scr._step = 4
    scr._render_step()
    await pilot.pause()
    return " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))


@pytest.mark.asyncio
async def test_final_summary_shows_mcp_row_none_selected(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        blob = await _final_blob(pilot, scr, mcp=[])
        assert "MCP" in blob                                 # строка MCP есть всегда
        assert "не выбраны" in blob                          # честная фраза «не ставим»
        assert "установятся после подтверждения" not in blob  # но не обещаем установку


@pytest.mark.asyncio
async def test_final_summary_shows_mcp_when_selected(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        blob = await _final_blob(pilot, scr, mcp=["office"])
        assert "office" in blob
        assert "установятся после подтверждения" in blob


@pytest.mark.asyncio
async def test_mcp_installed_marked_and_not_toggleable(tmp_path):
    # уже подключённый MCP помечен «установлен» и не переключается Space
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _McpInstalled:
        def installable_names(self): return ["office", "science"]
        def wired_names(self): return ["office"]
        def install(self, name=None): return 0

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(mcp=_McpInstalled())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 3
        scr._render_step()
        await pilot.pause()
        await pilot.press("space")           # курсор на office (idx 0, установлен) → не выбирается
        await pilot.pause()
        assert "office" not in scr._mcp_selected
        await pilot.press("down")
        await pilot.press("space")           # science → выбирается
        await pilot.pause()
        assert "science" in scr._mcp_selected
        blob = " ".join(str(w.render()) for w in scr.query(".onb-option"))
        assert "подключён" in blob


@pytest.mark.asyncio
async def test_scaffold_shows_logo_stepper_and_esc_exits(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        # логотип и степпер отрисованы
        assert scr.query_one("#onb-logo")
        assert scr.query_one("#onb-stepper")
        assert scr._step == 0
        # Esc → модалка подтверждения (не мгновенный выход); подтверждение → app.exit
        exited = []
        app.exit = lambda *a, **k: exited.append(True)
        await pilot.press("escape")
        await pilot.pause()
        from gc_tui.screens.confirm import ConfirmScreen
        assert isinstance(app.screen, ConfirmScreen)
        assert exited == []
        await pilot.press("y")
        await pilot.pause()
        assert exited == [True]


@pytest.mark.asyncio
async def test_address_invalid_shows_error_valid_advances(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        inp = scr.query_one("#onb-endpoint", Input)
        inp.value = "ttp://bad"            # опечатка → невалид
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 0               # остались
        assert scr.query("#onb-error")      # ошибка показана
        inp.value = "https://h/v1/chat/completions"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1               # перешли на авторизацию
        assert scr._values["completions_url"] == "https://h/v1/chat/completions"


@pytest.mark.asyncio
async def test_auth_no_auth_writes_empty(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 1; scr._render_step(); await pilot.pause()
        await pilot.press("down")          # → «без авторизации»
        await pilot.pause()
        await pilot.press("enter"); await pilot.pause()
        assert scr._values["Authorization"] == ""
        assert scr._step == 2


@pytest.mark.asyncio
async def test_onboarding_auth_bearer_requires_token(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr.query_one("#onb-endpoint", Input).value = "https://h/v1/chat/completions"
        await pilot.press("enter")                  # → авторизация (Bearer, поле пусто)
        await pilot.press("enter")                  # попытка вперёд без токена
        await pilot.pause()
        assert scr._step == 1                        # НЕ продвинулся
        await pilot.press("down"); await pilot.press("down")   # Bearer → без авт → пропустить
        await pilot.press("enter")                  # «пропустить» → отложить, вперёд
        await pilot.pause()
        assert scr._step == 2


@pytest.mark.asyncio
async def test_auth_token_bearer(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 1; scr._render_step(); await pilot.pause()
        scr.query_one("#onb-token", Input).value = "sk-1"   # опция 0 = токен по умолчанию
        await pilot.press("enter"); await pilot.pause()
        assert scr._values["Authorization"] == "Bearer sk-1"


@pytest.mark.asyncio
async def test_model_list_marks_and_pick(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_core.model_picker import ModelInfo
    from tests.tui.conftest import wait_until
    from gc_tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    fake = lambda *a, **k: [ModelInfo("Qwen", tool_capable=True),
                            ModelInfo("Flash", tool_capable=False)]
    async with app.run_test() as pilot:
        scr = _screen(fetch=fake)
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 2; scr._render_step()
        # дождаться worker-загрузки в ModelEditor
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor)) and scr.query(ModelEditor).first().models_loaded, "models loaded")
        # ⚠ есть (Flash не tool-capable)
        assert any("⚠" in s.renderable if hasattr(s, "renderable") else "⚠" in str(s.render())
                   for s in scr.query(".onb-option"))
        await pilot.press("enter"); await pilot.pause()   # курсор на 0 = Qwen
        assert scr._values["model"] == "Qwen"


@pytest.mark.asyncio
async def test_model_manual_fallback(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    from tests.tui.conftest import wait_until
    from gc_tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: None)   # список не получен
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 2; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor)) and scr.query(ModelEditor).first().models_loaded, "models loaded")
        scr.query_one("#onb-model", Input).value = "my-model"
        await pilot.press("enter"); await pilot.pause()
        assert scr._values["model"] == "my-model"


@pytest.mark.asyncio
async def test_onboarding_model_step_pgup_goes_back_with_filter(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    from gc_core.model_picker import ModelInfo
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: [ModelInfo(id="glm-5.2-fp8", tool_capable=True),
                                             ModelInfo(id="openai/gpt-5.6-terra", tool_capable=True)])
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr.query_one("#onb-endpoint", Input).value = "https://h/v1/chat/completions"
        await pilot.press("enter")                      # → авторизация
        scr.query_one("#onb-token", Input).value = "tok"   # Bearer требует токен (Task 5) → вводим
        await pilot.press("enter")                      # → модель
        await pilot.pause(); await pilot.pause()
        assert scr._step == 2
        scr.query_one("#model-filter", Input).value = "gpt"   # текст в фильтре
        await pilot.pause()
        scr.query_one("#model-filter", Input).focus()   # фокус в фильтре (в pilot автофокус после mount не встаёт)
        await pilot.pause()
        assert scr.query_one("#model-filter", Input).has_focus   # PgUp работает, пока фокус в поле фильтра
        await pilot.press("pageup")                     # PgUp должен вернуть на шаг авторизации
        await pilot.pause()
        assert scr._step == 1


@pytest.mark.asyncio
async def test_onboarding_model_step_filter_and_pick(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    from gc_core.model_picker import ModelInfo
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: [ModelInfo(id="glm-5.2-fp8", tool_capable=True),
                                             ModelInfo(id="openai/gpt-5.6-terra", tool_capable=True),
                                             ModelInfo(id="deepseek-v3-base", tool_capable=True)])
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr.query_one("#onb-endpoint", Input).value = "https://h/v1/chat/completions"
        await pilot.press("enter")
        scr.query_one("#onb-token", Input).value = "tok"       # Bearer требует токен (Task 5)
        await pilot.press("enter")
        await pilot.pause(); await pilot.pause()
        scr.query_one("#model-filter", Input).value = "gpt"
        await pilot.pause()
        rows = [str(w.render()) for w in scr.query(".onb-option")]   # фильтр реально сужает список
        assert any("gpt-5.6-terra" in r for r in rows)              # совпадение осталось
        assert not any("deepseek" in r for r in rows)               # не-совпадение убрано
        await pilot.press("down")                       # ↑↓ двигают выбор
        await pilot.press("enter")                      # выбрать → шаг MCP
        await pilot.pause()
        assert scr._step == 3
        assert scr._values.get("model") == "openai/gpt-5.6-terra"


@pytest.mark.asyncio
async def test_mcp_space_toggles(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 3; scr._render_step(); await pilot.pause()
        await pilot.press("space")          # отметить первый (duckduckgo)
        await pilot.pause()
        assert "duckduckgo" in scr._mcp_selected
        await pilot.press("enter"); await pilot.pause()
        assert scr._step == 4


@pytest.mark.asyncio
async def test_summary_line_fills_as_steps_complete(tmp_path):
    """Строка сводки под степпером заполняется по ходу; токен маскируется."""
    from gc_tui.screens.onboarding import OnboardingScreen
    from textual.widgets import Static
    from gc_tui import app as app_mod

    app = app_mod.TuiApp(config={"completions_url": "", "chat_completion_headers": {},
                                 "model": ""})
    async with app.run_test() as pilot:
        scr = OnboardingScreen(config=app._config, config_path=str(tmp_path / "c.yml"),
                               review_all=True, on_commit=lambda: None)
        app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = "https://46.243.227.147:15000/v1/chat/completions"
        scr._values["Authorization"] = "Bearer sk-SECRET"
        scr._render_step()
        await pilot.pause()
        line = str(scr.query_one("#onb-summary", Static).render())
        assert "46.243.227.147:15000" in line
        assert "sk-SECRET" not in line and "токен Bearer" in line


@pytest.mark.asyncio
async def test_final_commit_writes_and_calls_on_commit(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from tests.tui.conftest import wait_until
    from gc_core import onboarding_config_writer as w
    written = {}
    monkeypatch.setattr(w, "write_values", lambda path, vals, **kw: written.update(vals) or [])
    committed = []
    installed = []
    mcp = _FakeMcp(); mcp.install = lambda name=None: installed.append(name) or 0
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(on_commit=lambda: committed.append(True), mcp=mcp)
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values = {"completions_url": "https://h/v1", "Authorization": "Bearer sk", "model": "m"}
        scr._mcp_selected = {"office"}
        scr._step = 4; scr._render_step(); await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        assert written == {
            "backend.completions_url": "https://h/v1",
            "backend.chat_completion_headers.Authorization": "Bearer sk",
            "agent.model": "m",
        }
        assert installed == ["office"]
        assert committed == [True]
