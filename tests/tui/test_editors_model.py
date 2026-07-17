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


def _models3():
    return [ModelInfo(id="gigacode", tool_capable=True),
            ModelInfo(id="glm-5.2-fp8", tool_capable=True),
            ModelInfo(id="openai/gpt-5.6-terra", tool_capable=True)]


async def test_order_current_then_manual_then_rest():
    """Порядок строк: текущая → вписать вручную → остальные."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="gigacode", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models3(), active="gigacode")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert "gigacode" in rows[0] and "текущая" in rows[0]   # 1: текущая
        assert "вписать вручную" in rows[1]                     # 2: ручной ввод
        assert "glm-5.2-fp8" in rows[2]                         # 3: остальные
        assert "gpt-5.6-terra" in rows[3]
        # текущая НЕ дублируется в «остальных»
        assert sum("gigacode" in r for r in rows) == 1


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
        assert ed.submit() == "good-model"       # нет текущей → курсор на первой модели


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
        ed.move(-1)                               # с первой модели ↑ → «вписать вручную…»
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


async def test_filter_narrows_others_keeps_anchors():
    """Фильтр сужает только «остальные»; текущая и «вписать вручную» закреплены."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="gigacode", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models3(), active="gigacode")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#model-filter", Input).value = "gpt"
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert "gigacode" in rows[0]               # якорь-текущая закреплён
        assert "вписать вручную" in rows[1]        # якорь-manual закреплён
        assert any("gpt-5.6-terra" in r for r in rows[2:])
        assert not any("glm-5.2-fp8" in r for r in rows)   # не совпало — скрыто


async def test_filter_lifts_cap():
    """При пустом фильтре — кап 20 + подсказка; при активном — все совпадения."""
    from tui.editors.model import ModelEditor
    many = [ModelInfo(id=f"m-{i:02d}") for i in range(30)]
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: many)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("и ещё" in h for h in hints)                 # кап при пустом фильтре
        ed.query_one("#model-filter", Input).value = "m-2"
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option") if "m-2" in str(w.render())]
        assert len(rows) == 10                                  # m-20..m-29, кап снят


async def test_filter_no_match_shows_hint():
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="gigacode", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models3(), active="gigacode")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#model-filter", Input).value = "zzz"
        await pilot.pause()
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("ничего не найдено" in h for h in hints)


async def test_focus_filter_on_entry_and_manual_switch():
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="gigacode", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models3(), active="gigacode")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.query_one("#model-filter", Input).has_focus         # вход → фильтр
        ed.move(1)                                                    # ▸ на «вписать вручную»
        await pilot.pause()
        assert ed.query_one(f"#{ed._input_id}", Input).has_focus      # ручное поле
        ed.move(1)                                                    # ▸ ушёл на модель
        await pilot.pause()
        assert ed.query_one("#model-filter", Input).has_focus         # назад в фильтр


async def test_model_warn_banner_shown_when_only_current_is_non_tool_capable():
    """Task 1 review (Minor): легенда ⚠ должна учитывать и «текущую» — если действующая
    модель единственная не-tool-capable, баннер всё равно обязан появиться."""
    from tui.editors.model import ModelEditor
    from onboarding_fields import MODEL_WARN
    ed = ModelEditor(current="bad-model", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models(), active="bad-model")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in ed.query(".onb-option")]
        assert any("bad-model" in r and "текущая" in r for r in rows)   # active == строка "current"
        assert not any("good-model" in r and "⚠" in r for r in rows)    # остальные — без ⚠
        warns = [str(w.render()) for w in ed.query(".onb-warn")]
        assert any(MODEL_WARN in w for w in warns)                      # баннер всё равно смонтирован


async def test_filter_reselects_survivor_by_identity_across_index_shift():
    """§4.4/identity: выбранная модель пережила фильтр, но сместилась по индексу —
    ▸ остаётся на НЕЙ, не на модели, занявшей её старый индекс (баг clamp по позиции)."""
    from tui.editors.model import ModelEditor
    models = [ModelInfo(id="alpha"), ModelInfo(id="keep-me"),
              ModelInfo(id="beta"), ModelInfo(id="gamma")]
    ed = ModelEditor(current="gigacode", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: [ModelInfo(id="gigacode"), *models],
                     active="gigacode")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(3)                                   # ▸ на keep-me (current,manual,alpha,keep-me,…)
        assert ed.submit() == "keep-me"
        ed.query_one("#model-filter", Input).value = "e"   # keep-me, beta остаются; alpha,gamma — нет
        await pilot.pause()
        assert ed.submit() == "keep-me"              # identity сохранён (clamp по позиции вернул бы beta)
        assert ed.submit() != "beta"


async def test_filter_moves_cursor_to_anchor_when_selection_filtered_out():
    """§4.4: строка под ▸ пропала после сужения → ▸ на первую видимую (якорь);
    submit НЕ отдаёт молча другую модель, вставшую на тот же индекс."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="gigacode", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models3(), active="gigacode")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(2)                                   # ▸ на glm-5.2-fp8 (current,manual,glm,gpt)
        assert ed.submit() == "glm-5.2-fp8"
        ed.query_one("#model-filter", Input).value = "gpt"   # исключает glm-*
        await pilot.pause()
        assert ed.submit() != "openai/gpt-5.6-terra"  # не подменяет выбор по позиции
        assert ed.submit() == "gigacode"              # первая видимая = якорь «текущая»


async def test_filter_no_match_keeps_focus_in_filter_without_popping_manual():
    """Seam: без якоря «текущая» фильтр без совпадений сворачивает список к [manual];
    ▸ садится на manual, но ПРИ ФИЛЬТРАЦИИ ручной ввод не всплывает и фокус остаётся в
    поле фильтра. Амбер-поле активирует только ↑↓-навигация (move), не схлопывание списка."""
    from tui.editors.model import ModelEditor
    ed = ModelEditor(current="", endpoint="http://x", auth=None, verify=True,
                     fetch=lambda *a, **k: _models3())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#model-filter", Input).value = "zzz-nomatch"   # не совпадает ни с одной
        await pilot.pause()
        assert ed.query_one("#model-filter", Input).has_focus            # фокус остался в фильтре
        assert ed.query_one(ed._manual_selector(), Input).display is False  # ручной ввод не всплыл
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("ничего не найдено" in h for h in hints)
        ed.move(1)                                                       # ↑↓-навигация → на manual
        await pilot.pause()
        assert ed.query_one(ed._manual_selector(), Input).display is True   # теперь ручной ввод виден
        assert ed.query_one(ed._manual_selector(), Input).has_focus         # и сфокусирован
