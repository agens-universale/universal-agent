"""ModelEditor: fetch-инъекция, список с ⚠, ручной ввод, поведение без списка."""

import pytest

pytest.importorskip("textual")

from gc_core.model_picker import ModelInfo
from textual.app import App, ComposeResult
from textual.widgets import Input


def _options(ed) -> list:
    """Строки списка НА ЭКРАНЕ. Фильтр гасит их через `display`, а не сносит (общая
    механика `FieldListController`), — «видно» и значит `w.display`."""
    return [w for w in ed.query(".onb-option") if w.display]


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor

    return _App()


def _models():
    return [
        ModelInfo(id="good-model", tool_capable=True),
        ModelInfo(id="bad-model", tool_capable=False),
    ]


def _models3():
    return [
        ModelInfo(id="gigacode", tool_capable=True),
        ModelInfo(id="glm-5.2-fp8", tool_capable=True),
        ModelInfo(id="openai/gpt-5.6-terra", tool_capable=True),
    ]


async def test_order_current_then_rest():
    """Порядок строк: текущая → остальные. Строки «вписать вручную» больше нет —
    вписать своё можно прямо в поле (смок владельца 2026-08-04)."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="gigacode",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models3(),
        active="gigacode",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in _options(ed)]
        assert "gigacode" in rows[0] and "текущая" in rows[0]  # 1: текущая
        assert "glm-5.2-fp8" in rows[1]  # 2: остальные
        assert "gpt-5.6-terra" in rows[2]
        assert not any("вручную" in r for r in rows)
        # текущая НЕ дублируется в «остальных»
        assert sum("gigacode" in r for r in rows) == 1


async def test_single_mark_across_rows_and_moves():
    """● ровно одна в любом положении курсора: пока выбора в этой сессии не было, она
    на действующей модели, и ручная строка её не дублирует."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="gigacode",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models3(),
        active="gigacode",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(len(ed._rows) + 1):
            rows = [str(w.render()) for w in _options(ed)]
            assert sum(r.count("●") for r in rows) == 1, rows
            ed.move(1)


async def test_typed_name_gets_no_mark_and_enter_means_next():
    """Набранное своё имя пометки не получает: строки-посредника больше нет, а `mark()`
    без моделей под фильтром отдаёт None — Enter там сразу значит «далее», и значение
    уезжает через submit(). ● при этом остаётся на действующей модели."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="gigacode",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models3(),
        active="gigacode",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one(
            "#onb-model", Input
        ).value = "my/own"  # ни одна модель не совпала
        await pilot.pause()
        assert ed.mark() is None  # метить нечего — Enter значит «далее»
        assert ed.submit() == "my/own"  # но значение доезжает
        rows = [str(w.render()) for w in _options(ed) if w.display]
        assert sum(r.count("●") for r in rows) == 1  # ● одна — на действующей
        assert "gigacode" in next(r for r in rows if "●" in r)


async def test_model_editor_lists_models_and_marks_warn(wait_for=None):
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models(),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()  # воркер fetch отработал (fake мгновенный)
        assert ed.models_loaded
        rows = [str(w.render()) for w in _options(ed)]
        assert any("good-model" in r for r in rows)
        assert any("bad-model" in r and "⚠" in r for r in rows)
        assert (
            ed.submit() == "good-model"
        )  # нет текущей → курсор на первой модели


async def test_model_editor_manual_entry_when_fetch_fails():
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="prev-model",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = ed.query_one(Input)
        assert inp.value == "prev-model"  # префилл текущей
        inp.value = "typed-model"
        assert ed.submit() == "typed-model"


async def test_typed_value_is_taken_when_nothing_matches():
    """Своё имя — это набранное в поле. Отдельной строки под него нет: как только
    набранное не совпало ни с одной строкой, submit() отдаёт текст поля."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models(),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.submit() is not None  # на строке списка — её id
        ed.query_one(f"#{ed._input_id}", Input).value = "my/model"
        await pilot.pause()
        assert ed.submit() == "my/model"


async def test_model_editor_source_line_when_list_loaded():
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models(),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("список получен с сервера" in h for h in hints)


async def test_model_editor_active_gets_current_label_and_preselects_cursor():
    """v2: метка «текущая» у строки active; initial-курсор — на ней, не на первой."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models(),
        active="bad-model",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in _options(ed)]
        assert any("bad-model" in r and "текущая" in r for r in rows)
        assert not any("good-model" in r and "текущая" in r for r in rows)
        assert ed.submit() == "bad-model"  # курсор сразу на active


async def test_model_editor_synthetic_list_when_fetch_fails_with_active():
    """v2: fetch не дал список, но active задан → псевдо-список [active] + ручной ввод,
    источник-строка сигналит об отсутствии списка, управление обычное."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")),
        active="active-model",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in _options(ed)]
        assert any("active-model" in r and "текущая" in r for r in rows)
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any(
            "не удалось получить список — можно вписать вручную" in h
            for h in hints
        )
        assert ed.submit() == "active-model"  # submit по умолчанию → active
        ed.move(1)  # управление обычное — движение работает
        assert (
            ed.submit() == "active-model"
        )  # список из одной строки — с неё и не уйти


async def test_model_editor_no_list_no_active_shows_source_line():
    """v2: без списка и без active — как раньше (только ручной Input), но с
    новой унифицированной источник-строкой вместо старого MODEL_NO_LIST-хинта."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: None,
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.query_one(Input)
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any(
            "не удалось получить список — можно вписать вручную" in h
            for h in hints
        )


def _many_models(n):
    return [
        ModelInfo(id=f"model-{i:03d}", tool_capable=True) for i in range(n)
    ]


async def test_model_editor_shows_all_models_with_counter():
    """Сервер отдал сотни моделей → лимита нет (список крутится в .dropdown-scroll),
    показаны все, счётчик над списком — «N моделей»."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _many_models(50),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        options = _options(ed)  # все модели + «вписать вручную…»
        assert len(options) == 50
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("50 моделей" in h for h in hints)


async def test_model_editor_active_beyond_cap_still_shown():
    """Действующая модель за пределами топа — всё равно в списке (с «текущая»),
    предвыбор курсора на неё работает."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _many_models(50),
        active="model-045",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in _options(ed)]
        assert any("model-045" in r and "текущая" in r for r in rows)
        assert (
            ed.submit() == "model-045"
        )  # курсор сразу на action, submit — active


async def test_model_editor_calls_on_loaded_callback_after_fetch():
    """v2: on_loaded — хук для пересчёта dirty в SettingsScreen после async fetch."""
    from gc_tui.editors.model import ModelEditor

    calls = []
    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models(),
        on_loaded=lambda: calls.append(1),
    )
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
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="gigacode_agent_v1.6_rc",
        endpoint="http://x",
        auth=None,
        verify=True,
        active="gigacode_agent_v1.6_rc",
        fetch=lambda *a, **k: [ModelInfo(id="glm-5.2-fp8", tool_capable=True)],
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in _options(ed)]
        assert any("gigacode_agent_v1.6_rc" in r for r in rows), (
            f"действующая модель пропала из списка: {rows}"
        )
        assert any("gigacode_agent_v1.6_rc" in r and "●" in r for r in rows), (
            "действующая модель не помечена как текущая"
        )
        # предвыбор — на действующей, а не на чужой
        assert ed.submit() == "gigacode_agent_v1.6_rc"
        # экран считает «изменено» как peek() != значение в файле → правкой это не считается
        assert ed.peek() == "gigacode_agent_v1.6_rc"


async def test_active_model_present_in_list_keeps_previous_behaviour():
    """Регресс: если сервер вернул действующую модель — поведение прежнее (дублей нет)."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="bad-model",
        endpoint="http://x",
        auth=None,
        verify=True,
        active="bad-model",
        fetch=lambda *a, **k: _models(),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in _options(ed)]
        assert sum("bad-model" in r for r in rows) == 1  # без дубля
        assert ed.submit() == "bad-model"
        assert ed.peek() == "bad-model"


async def test_filter_narrows_others_keeps_anchors():
    """Фильтр сужает только «остальные»; текущая и «вписать вручную» закреплены."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="gigacode",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models3(),
        active="gigacode",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#onb-model", Input).value = "gpt"
        await pilot.pause()
        rows = [str(w.render()) for w in _options(ed)]
        assert "gigacode" in rows[0]  # якорь-текущая закреплён
        assert any("gpt-5.6-terra" in r for r in rows[1:])
        assert not any("glm-5.2-fp8" in r for r in rows)  # не совпало — скрыто


async def test_filter_narrows_without_cap():
    """Без фильтра — все модели, без капа; фильтр по-прежнему сужает по подстроке."""
    from gc_tui.editors.model import ModelEditor

    many = [ModelInfo(id=f"m-{i:02d}") for i in range(30)]
    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: many,
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        options = _options(ed)
        assert len(options) == 30  # все 30, строки «вручную» нет
        ed.query_one("#onb-model", Input).value = "m-2"
        await pilot.pause()
        rows = [
            str(w.render()) for w in _options(ed) if "m-2" in str(w.render())
        ]
        assert len(rows) == 10  # m-20..m-29


async def test_filter_no_match_shows_hint():
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="gigacode",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models3(),
        active="gigacode",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#onb-model", Input).value = "zzz"
        await pilot.pause()
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("ничего не найдено" in h for h in hints)


async def test_focus_stays_in_the_field_across_moves():
    """Поле одно на обе роли (§3.1), и фокус из него не уходит: и на входе, и на
    ручной строке, и обратно на модели печать продолжает фильтровать список.
    Раньше фокус прыгал между `#model-filter` и всплывающим ручным вводом."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="gigacode",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models3(),
        active="gigacode",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        field = ed.query_one(f"#{ed._input_id}", Input)
        assert field.has_focus  # вход → поле
        ed.move(1)  # ▸ на следующую модель
        await pilot.pause()
        assert field.has_focus
        ed.move(1)  # ▸ ушёл на модель
        await pilot.pause()
        assert field.has_focus
        assert (
            ed.query_one(f"#{ed._input_id}", Input) is field
        )  # и поле то же самое


async def test_model_warn_banner_shown_when_only_current_is_non_tool_capable():
    """Task 1 review (Minor): легенда ⚠ должна учитывать и «текущую» — если действующая
    модель единственная не-tool-capable, баннер всё равно обязан появиться."""
    from gc_core.onboarding_fields import MODEL_WARN
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="bad-model",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models(),
        active="bad-model",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [str(w.render()) for w in _options(ed)]
        assert any(
            "bad-model" in r and "текущая" in r for r in rows
        )  # active == строка "current"
        assert not any(
            "good-model" in r and "⚠" in r for r in rows
        )  # остальные — без ⚠
        warns = [str(w.render()) for w in ed.query(".onb-warn")]
        assert any(
            MODEL_WARN in w for w in warns
        )  # баннер всё равно смонтирован


async def test_filter_reselects_survivor_by_identity_across_index_shift():
    """§4.4/identity: выбранная модель пережила фильтр, но сместилась по индексу —
    ▸ остаётся на НЕЙ, не на модели, занявшей её старый индекс (баг clamp по позиции)."""
    from gc_tui.editors.model import ModelEditor

    models = [
        ModelInfo(id="alpha"),
        ModelInfo(id="keep-me"),
        ModelInfo(id="beta"),
        ModelInfo(id="gamma"),
    ]
    ed = ModelEditor(
        current="gigacode",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: [ModelInfo(id="gigacode"), *models],
        active="gigacode",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(2)  # ▸ на keep-me (current, alpha, keep-me, …)
        assert ed.submit() == "gigacode"  # курсор не подтверждает выбор
        assert ed.mark() == "keep-me"
        ed.query_one(
            "#onb-model", Input
        ).value = "e"  # keep-me, beta остаются; alpha,gamma — нет
        await pilot.pause()
        assert (
            ed.submit() == "keep-me"
        )  # identity сохранён (clamp по позиции вернул бы beta)
        assert ed._list.selected_item.model.id == "keep-me"
        assert ed.submit() != "beta"


async def test_filter_moves_cursor_to_anchor_when_selection_filtered_out():
    """§4.4: строка под ▸ пропала после сужения → ▸ на первую видимую (якорь);
    submit НЕ отдаёт молча другую модель, вставшую на тот же индекс."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="gigacode",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models3(),
        active="gigacode",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)  # ▸ на glm-5.2-fp8 (current, glm, gpt)
        assert ed.submit() == "gigacode"
        assert ed.mark() == "glm-5.2-fp8"
        ed.query_one("#onb-model", Input).value = "gpt"  # исключает glm-*
        await pilot.pause()
        assert (
            ed.submit() != "openai/gpt-5.6-terra"
        )  # не подменяет выбор по позиции
        assert ed._list.selected_item.model.id == "gigacode"
        assert ed.submit() == "glm-5.2-fp8"  # фильтр не меняет подтверждённый выбор
        assert ed.mark() == "gigacode"
        assert ed.submit() == "gigacode"


def _M(id_: str, tool_capable: bool = True) -> ModelInfo:
    return ModelInfo(id=id_, tool_capable=tool_capable)


async def _mount_editor(models, **kwargs):
    """Собрать ModelEditor с фиктивным fetch и приложение-харнес вокруг него."""
    from gc_tui.editors.model import ModelEditor

    kwargs.setdefault("current", "")
    kwargs.setdefault("endpoint", "http://x")
    kwargs.setdefault("auth", None)
    kwargs.setdefault("verify", True)
    ed = ModelEditor(fetch=lambda *a, **k: models, **kwargs)
    return _harness(ed), ed


async def test_rows_live_in_a_scroll_container():
    """Строки — во вложенном .dropdown-scroll: длинный список крутится внутри,
    страница не раскатывается."""
    from textual.containers import VerticalScroll

    models = [_M(f"m-{i}") for i in range(50)]
    app, ed = await _mount_editor(models)
    async with app.run_test() as pilot:
        await pilot.pause()
        scroll = ed.query_one(".onb-model-scroll", VerticalScroll)
        assert "dropdown-scroll" in scroll.classes
        assert scroll.can_focus is False
        rows = scroll.query(".onb-option")
        assert len(rows) >= 50  # все модели, а не топ-20


async def test_no_truncation_hint():
    """Лимита нет — значит и обрубка «…и ещё N» быть не должно."""
    models = [_M(f"m-{i}") for i in range(50)]
    app, ed = await _mount_editor(models)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = " ".join(str(w.render()) for w in ed.query(".onb-hint"))
        assert "уточните фильтром" not in text
        assert "50 моделей" in text


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "1 модель"),
        (2, "2 модели"),
        (4, "4 модели"),
        (5, "5 моделей"),
        (11, "11 моделей"),
        (14, "14 моделей"),
        (21, "21 модель"),
        (22, "22 модели"),
        (100, "100 моделей"),
        (365, "365 моделей"),
    ],
)
def test_models_counter_plural(n, expected):
    """Счётчик над списком склоняется по-русски: «2 модели», а не «2 моделей»."""
    from gc_tui.services.probe_presentation import plural_models

    assert f"{n} {plural_models(n)}" == expected


async def test_filter_no_match_falls_back_to_manual_entry():
    """Seam: без якоря «текущая» фильтр без совпадений сворачивает список к [manual];
    ▸ садится на manual, фокус остаётся в поле, а под рамкой появляется «ничего не
    найдено». Всплывающего ручного ввода под списком больше нет: напечатанное и не
    совпавшее ни с одной строкой И ЕСТЬ ручной ввод (§3.2.6)."""
    from gc_tui.editors.model import ModelEditor

    ed = ModelEditor(
        current="",
        endpoint="http://x",
        auth=None,
        verify=True,
        fetch=lambda *a, **k: _models3(),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        field = ed.query_one(ed._manual_selector(), Input)
        field.focus()
        await pilot.pause()
        field.value = "zzz-nomatch"  # не совпадает ни с одной
        await pilot.pause()
        assert field.has_focus  # фокус остался в поле
        hints = [str(w.render()) for w in ed.query(".onb-hint")]
        assert any("ничего не найдено" in h for h in hints)
        assert (
            ed.submit() == "zzz-nomatch"
        )  # ▸ на manual → набранное и есть выбор


# --- композиция §3.1: поле само себе фильтр, список под ним в одной рамке ---


async def test_model_field_is_the_filter():
    """Отдельной строки фильтра нет: печать в поле модели фильтрует список."""
    from textual.css.query import NoMatches

    app, ed = await _mount_editor([_M("a/one"), _M("a/two"), _M("b/three")])
    async with app.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(NoMatches):
            ed.query_one("#model-filter")  # отдельного фильтра больше нет
        before = len(_options(ed))
        ed.query_one("#onb-model", Input).value = "b/"
        await pilot.pause()
        after = len(_options(ed))
        assert after < before, "печать в поле должна сузить список"


async def test_filtering_does_not_move_the_mark():
    """Печать фильтрует, но ● остаётся на прежней строке: иначе обещание Enter
    в футере мигает «выбрать» ↔ «далее» на каждую букву.

    Метим ручную строку: у строк списка пометка и так сверяется по id модели, а
    вот ручная сверялась по тексту поля — и после слияния поля с фильтром этот
    текст меняется на каждую букву.

    Пометка ставится вызовом mark(), а не клавишей: ModelEditor клавиш не
    обрабатывает вовсе — их ловит экран-хозяин и зовёт mark(). pilot.press("space")
    ушёл бы в поле-фильтр, и тест был бы зелёным и до, и после правки.
    """
    app, ed = await _mount_editor([_M("a/one"), _M("a/two"), _M("b/three")])
    async with app.run_test() as pilot:
        await pilot.pause()
        field = ed.query_one("#onb-model", Input)
        field.value = "a/"  # напечатанное = и фильтр, и ручной ввод
        await pilot.pause()
        ed.move(-1)  # ▸ на последнюю видимую строку
        assert ed.mark() == "a/two"  # пометить строку под курсором
        await pilot.pause()
        assert ed.is_marked, (
            "предусловие: пометка стоит на строке под курсором"
        )
        marked_before = ed._marked
        field.value = "b/"
        await pilot.pause()
        # `is_marked` — про строку ПОД КУРСОРОМ, а курсор фильтр уводит на другую строку.
        # Сама пометка при этом обязана уцелеть: она переезжает только явным действием.
        assert ed._marked == marked_before, (
            "фильтрация не должна снимать пометку"
        )


async def test_filtering_keeps_focus_and_text_in_field():
    """Фильтрация не пересобирает поле: фокус и набранное остаются на месте."""
    app, ed = await _mount_editor([_M("a/one"), _M("a/two"), _M("b/three")])
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = ed.query_one("#onb-model", Input)
        inp.focus()
        await pilot.pause()
        before = len(_options(ed))
        inp.value = "b/"
        await pilot.pause()
        assert len(_options(ed)) < before, (
            "предусловие: печать отфильтровала список"
        )
        again = ed.query_one("#onb-model", Input)
        assert again is inp, "поле не должно пересоздаваться при фильтрации"
        assert again.value == "b/"
        assert pilot.app.focused is again, "фокус обязан остаться в поле"


async def test_field_and_list_live_in_one_group():
    """Поле и строки — потомки одной рамки-группы, счётчик и статусы — под ней (§3.1)."""
    from textual.containers import VerticalScroll

    app, ed = await _mount_editor([_M("a/one"), _M("a/two")])
    async with app.run_test() as pilot:
        await pilot.pause()
        group = ed.query_one(f"#{ed._input_id}-group")
        assert "field-group" in group.classes
        inp = ed.query_one(f"#{ed._input_id}", Input)
        assert inp.parent is group, "поле должно жить внутри группы"
        assert "group-field" in inp.classes  # воздух внутри рамки — на поле
        assert (
            "set-framed" not in inp.classes
        )  # своей рамки у поля нет — она одна, на группе
        scroll = group.query_one(VerticalScroll)
        assert "dropdown-scroll" in scroll.classes
        assert list(scroll.query(".onb-option")), (
            "строки монтируются внутрь группы"
        )
        assert not list(group.query(".onb-hint")), (
            "счётчик и статусы — снаружи рамки"
        )
        assert not list(group.query(".onb-warn"))


# --- Alt+↓: перечитать список с сервера (§5.1 канона, роль ушла с Ctrl+Space — спека
# 2026-08-06 §2, §4.1) ---
#
# Монтаж — общий хелпер `mounted_model_editor`: его fetch снимает список СНИМКОМ
# (`list(models)`) на каждый вызов, поэтому «сервер отдаёт уже другое» выражается
# правкой того же списка между загрузками — своего фейка не нужно.


def _row_texts(ed) -> list[str]:
    return [str(w.render()) for w in _options(ed)]


async def test_reload_asks_the_server_again(tmp_path):
    """Перечитывание — настоящий поход на сервер: раскрывать у этого списка нечего
    (свёртывания у него нет вовсе, §9.1), и появившаяся на сервере модель обязана
    доехать до строк."""
    from form_harness import mounted_model_editor
    from gc_testkit.tui_support import wait_until

    models = [_M("a/one")]
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        assert not any("b/two" in r for r in _row_texts(ed))
        models.append(_M("b/two"))  # сервер отдаёт уже другое
        ed.reload()
        await wait_until(
            pilot,
            lambda: any("b/two" in r for r in _row_texts(ed)),
            "перечитанный список доехал до строк",
        )


async def test_reload_mounts_the_list_when_the_server_revives(tmp_path):
    """Сервер молчал — на экране одинокое поле в рамке (форма «сервер не отдал»);
    ожил — перечитывание собирает форму со списком. Это тот самый случай, ради
    которого клавиша и заведена."""
    from form_harness import mounted_model_editor
    from gc_testkit.tui_support import wait_until

    models = []
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        assert not ed.has_models and not _row_texts(
            ed
        )  # предусловие: списка нет
        models.append(_M("a/one"))
        ed.reload()
        await wait_until(
            pilot,
            lambda: bool(_row_texts(ed)),
            "список смонтировался после перечитывания",
        )
        ed.query_one(f"#{ed._input_id}-group")  # рамка-группа появилась
        assert any("a/one" in r for r in _row_texts(ed))


async def test_reload_keeps_the_mark(tmp_path):
    """§7.1: пометка переезжает только явным действием — перечитывание её не трогает,
    иначе ● молча уедет и обещание Enter в футере мигнёт."""
    from form_harness import mounted_model_editor
    from gc_testkit.tui_support import wait_until

    models = [_M("a/one"), _M("b/two")]
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        assert ed.mark() == "a/one"  # пометили строку под курсором
        await pilot.pause()
        assert [r for r in _row_texts(ed) if "●" in r][0].endswith("a/one")
        models.append(_M("c/three"))
        ed.reload()
        await wait_until(
            pilot, lambda: len(_row_texts(ed)) == 3, "список перечитан"
        )
        marked = [r for r in _row_texts(ed) if "●" in r]
        assert len(marked) == 1 and "a/one" in marked[0], (
            "● уехала с перечитыванием"
        )


async def test_reload_keeps_the_typed_filter(tmp_path):
    """Набранное в поле — фильтр (§3.2.6), и перечитывание его не теряет: список и
    поле обязаны показывать одно и то же (канон `voice_endpoint.ModelListController.
    reload`)."""
    from form_harness import mounted_model_editor
    from gc_testkit.tui_support import wait_until

    models = [_M("a/one"), _M("b/two")]
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        ed.query_one(f"#{ed._input_id}", Input).value = "b/"
        await pilot.pause()
        assert len(_row_texts(ed)) == 1  # только b/two
        models.append(_M("b/three"))
        ed.reload()
        await wait_until(
            pilot, lambda: len(_row_texts(ed)) == 2, "список перечитан"
        )
        assert not any("a/one" in r for r in _row_texts(ed)), "фильтр потерян"
        assert ed.query_one(f"#{ed._input_id}", Input).value == "b/"


async def test_reload_keeps_what_was_typed_while_the_server_is_silent(
    tmp_path,
):
    """Списка нет — в поле НЕ фильтр, а имя модели, набранное руками (`has_models`
    гасит `on_input_changed`). Правило донора «фильтр — то, что разошлось с
    baseline»: здесь baseline и есть само значение, значит фильтра нет, а набранное
    обязано пережить второе молчание сервера."""
    from form_harness import mounted_model_editor
    from gc_testkit.tui_support import wait_until

    models = []
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        field = ed.query_one(f"#{ed._input_id}", Input)
        field.value = "my-own-model"
        await pilot.pause()
        ed.reload()
        sel = f"#{ed._input_id}"
        await wait_until(
            pilot,
            lambda: bool(ed.query(sel)) and ed.query(sel).first() is not field,
            "поле пересобрано перечитыванием",
        )
        assert ed.query_one(sel, Input).value == "my-own-model"


async def test_a_stale_answer_does_not_remount_over_a_fresh_list(tmp_path):
    """Generation-guard. `exclusive=True` уже бегущий ПОТОК не останавливает (Textual
    этого не умеет), и без поколения ответ прошлой загрузки перемонтировал бы список
    поверх свежего."""
    from form_harness import mounted_model_editor
    from gc_testkit.tui_support import wait_until

    models = [_M("a/one")]
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        stale_gen = ed._gen  # поколение уже осевшей загрузки
        models.append(_M("b/two"))
        ed.reload()
        await wait_until(
            pilot, lambda: len(_row_texts(ed)) == 2, "список перечитан"
        )
        await ed._handle_fetch_result([_M("stale/model")], stale_gen)
        await pilot.pause()
        rows = _row_texts(ed)
        assert not any("stale" in r for r in rows), (
            "протухший ответ перемонтировал список"
        )
        assert any("b/two" in r for r in rows)


async def test_reload_keeps_the_row_under_the_cursor(tmp_path):
    """Перечитывание сохраняет курсор и явно подтверждённый выбор.
    Навигация до подтверждения сохраняемое значение не меняет."""
    from form_harness import mounted_model_editor
    from gc_testkit.tui_support import wait_until

    models = [_M("a/one"), _M("b/two")]
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        ed.move(1)  # с a/one на b/two
        assert ed.peek() == "a/one"  # навигация не фиксирует выбор
        assert ed.mark() == "b/two"
        assert ed.peek() == "b/two"
        models.append(_M("c/three"))
        ed.reload()
        await wait_until(
            pilot, lambda: len(_row_texts(ed)) == 3, "список перечитан"
        )
        assert ed._list.selected_item.model.id == "b/two", "курсор уехал на якорь"
        assert ed.peek() == "b/two", "выбор уехал на якорь"


async def test_a_generation_going_stale_during_the_teardown_does_not_mount_twice(
    tmp_path,
):
    """Гейт поколения обязан стоять и ПОСЛЕ `await`. `call_from_thread` не кладёт
    колбэк в очередь виджета, а запускает отдельной корутиной: пока обработчик
    поколения N висит на `remove_children`, цикл свободен и успевает прокрутить
    следующее нажатие. Проснувшись, N смонтировал бы поверх смонтированного —
    DuplicateIds, исключение в воркере, падение приложения."""
    from form_harness import mounted_model_editor

    models = [_M("a/one")]
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        original = ed.remove_children

        def teardown(*a, **kw):
            ed._gen += 1  # «пока сносили тело, пришло следующее нажатие»
            return original(*a, **kw)

        ed.remove_children = teardown
        await ed._handle_fetch_result([_M("b/two")], ed._gen)
        # Смотрим на ПРЯМЫХ детей: группа монтируется синхронно, а поле внутри неё —
        # внук, и по нему «не смонтировал» было бы верно и у сломанной реализации.
        assert not ed.children, "протухший обработчик всё-таки собрал тело"
        ed.remove_children = original
        await ed._handle_fetch_result(
            [_M("c/three")], ed._gen
        )  # свежий — монтирует
        await pilot.pause()
        assert len(ed.query(f"#{ed._input_id}")) == 1, (
            "тело собрано ровно один раз"
        )
        assert any("c/three" in r for r in _row_texts(ed))


@pytest.mark.asyncio
async def test_no_manual_row_in_the_list(tmp_path):
    """Строки «вписать вручную» в списке нет: поле само себе фильтр, вписать своё
    можно прямо в нём (смок владельца 2026-08-04)."""
    pytest.importorskip("textual")
    from form_harness import mounted_model_editor
    from gc_core.model_picker import ModelInfo

    models = [
        ModelInfo(id="a/one", tool_capable=True),
        ModelInfo(id="b/two", tool_capable=True),
    ]
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        texts = _row_texts(ed)
        assert not any("вручную" in t for t in texts), texts


@pytest.mark.asyncio
async def test_typed_name_still_reaches_submit_without_the_manual_row(
    tmp_path,
):
    """Вписанное в поле по-прежнему доезжает до сохранения — теперь без отдельной
    строки-посредника."""
    pytest.importorskip("textual")
    from form_harness import mounted_model_editor
    from gc_core.model_picker import ModelInfo
    from textual.widgets import Input

    models = [
        ModelInfo(id="a/one", tool_capable=True),
        ModelInfo(id="b/two", tool_capable=True),
    ]
    async with mounted_model_editor(tmp_path, models=models) as (pilot, ed):
        ed.query_one("#onb-model", Input).value = "my/own-model"
        await pilot.pause()
        assert ed.submit() == "my/own-model"
