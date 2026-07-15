"""ModelEditor: fetch-инъекция, список с ⚠, ручной ввод, поведение без списка."""
import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult
from textual.widgets import Input

from model_picker import ModelInfo


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor
    return _App()


def _models():
    return [ModelInfo(id="good-model", tool_capable=True),
            ModelInfo(id="bad-model", tool_capable=False)]


async def test_model_editor_lists_models_and_marks_warn(wait_for=None):
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()                      # воркер fetch отработал (fake мгновенный)
        assert ed.models_loaded
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert any("good-model" in r for r in rows)
        assert any("bad-model" in r and "⚠" in r for r in rows)
        assert ed.submit() == "good-model"       # курсор на первой


async def test_model_editor_manual_entry_when_fetch_fails():
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="prev-model", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = ed.query_one(Input)
        assert inp.value == "prev-model"          # префилл текущей
        inp.value = "typed-model"
        assert ed.submit() == "typed-model"


async def test_model_editor_move_to_manual_row_shows_input():
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1); ed.move(1)                    # → «вписать вручную…»
        assert ed.query_one(Input).display
        assert ed.submit() is None                # ручная строка пуста → не менять


async def test_model_editor_source_line_when_list_loaded():
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("список получен с сервера" in h for h in hints)


async def test_model_editor_active_gets_current_label_and_preselects_cursor():
    """v2: метка «текущая» у строки active; initial-курсор — на ней, не на первой."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models(), active="bad-model")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert any("bad-model" in r and "текущая" in r for r in rows)
        assert not any("good-model" in r and "текущая" in r for r in rows)
        assert ed.submit() == "bad-model"          # курсор сразу на active


async def test_model_editor_synthetic_list_when_fetch_fails_with_active():
    """v2: fetch не дал список, но active задан → псевдо-список [active] + ручной ввод,
    источник-строка сигналит об отсутствии списка, управление обычное."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")),
                     active="active-model")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert any("active-model" in r and "текущая" in r for r in rows)
        assert any("вписать вручную" in r for r in rows)
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("сервер не отдаёт список моделей" in h for h in hints)
        assert ed.submit() == "active-model"       # submit по умолчанию → active
        ed.move(1)                                  # управление обычное — движение работает
        assert ed.query_one(Input).display


async def test_model_editor_no_list_no_active_shows_source_line():
    """v2: без списка и без active — как раньше (только ручной Input), но с
    новой унифицированной источник-строкой вместо старого MODEL_NO_LIST-хинта."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: None)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.query_one(Input)
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("сервер не отдаёт список моделей" in h for h in hints)


def _many_models(n):
    return [ModelInfo(id=f"model-{i:03d}", tool_capable=True) for i in range(n)]


async def test_model_editor_caps_long_list_with_manual_hint():
    """Сервер отдал сотни моделей → показываем топ _MODEL_LIMIT + строку-подсказку
    «…и ещё N — впишите вручную»; остальное доступно ручным вводом."""
    from tui.editors.model import ModelEditor, _MODEL_LIMIT
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _many_models(50))
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        options = list(ed.query(".onb-option"))       # топ моделей + «вписать вручную…»
        assert len(options) == _MODEL_LIMIT + 1
        assert ed._truncated == 50 - _MODEL_LIMIT
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any(f"ещё {50 - _MODEL_LIMIT}" in h for h in hints)


async def test_model_editor_active_beyond_cap_still_shown():
    """Действующая модель за пределами топа — всё равно в списке (с «текущая»),
    предвыбор курсора на неё работает."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _many_models(50), active="model-045")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert any("model-045" in r and "текущая" in r for r in rows)
        assert ed.submit() == "model-045"             # курсор сразу на action, submit — active


async def test_model_editor_calls_on_loaded_callback_after_fetch():
    """v2: on_loaded — хук для пересчёта dirty в SettingsScreen после async fetch."""
    from tui.editors.model import ModelEditor
    calls = []
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models(), on_loaded=lambda: calls.append(1))
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert calls == [1]


async def test_active_model_missing_from_server_list_is_still_offered():
    """Инвариант: действующая модель ВСЕГДА в списке выбора.

    Сервер может её не вернуть (сменили эндпоинт, модель снята с раздачи). Без неё
    предвыбор встаёт на чужую модель: секция читается как «изменено» при входе, а Enter
    молча подменяет модель в конфиге на первую попавшуюся.
    """
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="gigacode_agent_v1.6_rc", endpoint="http://x", auth=None,
                     verify=True, active="gigacode_agent_v1.6_rc",
                     fetch=lambda *a, **k: [ModelInfo(id="glm-5.2-fp8", tool_capable=True)])
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert any("gigacode_agent_v1.6_rc" in r for r in rows), (
            f"действующая модель пропала из списка: {rows}")
        assert any("gigacode_agent_v1.6_rc" in r and "●" in r for r in rows), (
            "действующая модель не помечена как текущая")
        # предвыбор — на действующей, а не на чужой
        assert ed.submit() == "gigacode_agent_v1.6_rc"
        # экран считает «изменено» как peek() != значение в файле → правкой это не считается
        assert ed.peek() == "gigacode_agent_v1.6_rc"


async def test_active_model_present_in_list_keeps_previous_behaviour():
    """Регресс: если сервер вернул действующую модель — поведение прежнее (дублей нет)."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="bad-model", endpoint="http://x", auth=None, verify=True,
                     active="bad-model", fetch=lambda *a, **k: _models())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert sum("bad-model" in r for r in rows) == 1     # без дубля
        assert ed.submit() == "bad-model"
        assert ed.peek() == "bad-model"
