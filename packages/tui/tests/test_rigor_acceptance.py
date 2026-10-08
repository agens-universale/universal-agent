"""Гвозди приёмки /rigor (спека §8): резолвер-матрица, custom-правило, эфемерная
позиция, smart-гейт, сессионность пары. Живые UI-гейты — в смоуке (см. план §смоук)."""

from gc_testkit.tui_support import tui_settings
import pytest
from gc_testkit import run_coro
from gc_testkit.paths import REPO


def _app(tmp_path, yaml_text):
    from gc_tui.app import TuiApp

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    app = TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()
    app._threads_dir = str(tmp_path / "threads")
    app.tui_state.active_task_id = "t1"
    return app


@pytest.mark.parametrize(
    "gate,answers,expected",
    [
        ("off", "allow", "low"),
        ("smart", "judge", "medium"),
        ("on", "deny", "high"),
        ("on", "allow", "custom"),
        ("off", "deny", "custom"),
    ],
)
def test_resolver_matrix(gate, answers, expected):
    from gc_tui.toggles import resolve_preset

    assert (
        resolve_preset(
            {"tui.finish_gate_mode": gate, "tui.text_answer_mode": answers}
        )
        == expected
    )


def test_custom_unreachable_by_stepping(tmp_path):
    """Ревизия 4 (спека §2, §8, глобальный гейт): custom не выбирается ничем —
    /rigor крутит строго по трём значениям в обе стороны, из любой отправной
    точки, включая отображаемый custom (idx вне names -> 0); is_preset_custom
    и перехват в роутинге main.py упразднены вместе с выбираемой custom-
    позицией. apply_toggle на custom по-прежнему падает ValueError, не трогая
    сессию (см. test_apply_toggle_rejects_custom в test_palette_rigor.py)."""
    pytest.importorskip("textual")
    from gc_tui.toggles import PRESET_CUSTOM, apply_toggle, next_toggle_value

    b = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "allow"\n',
    )  # custom с самого начала

    for direction in (1, -1):
        for _ in range(7):  # больше одного полного оборота по трём значениям
            nxt = next_toggle_value(b, "rigor", direction)
            assert nxt != PRESET_CUSTOM
            apply_toggle(b, "rigor", nxt)

    with pytest.raises(ValueError):
        apply_toggle(b, "rigor", PRESET_CUSTOM)


def test_session_pair_not_preset_survives(tmp_path):
    """Сессионность работает для ПАРЫ значений, не для пресета (пресет не хранится)."""
    pytest.importorskip("textual")
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.session_settings import read_from_meta

    a = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "deny"\n',
    )
    from gc_tui.toggles import apply_toggle

    apply_toggle(a, "rigor", "low")
    store = FileTaskStore(threads_dir=a._threads_dir, system=a._system)
    saved = read_from_meta(store.get_task_meta("t1"))
    assert saved == {
        "tui.finish_gate_mode": "off",
        "tui.text_answer_mode": "allow",
    }
    assert "rigor" not in str(saved)  # пресет как значение не хранится


def test_apply_rigor_preset_never_clears_session_overrides(tmp_path):
    """§8, Г-А: «никакой выбор в палитре не снимает сессионные оверрайды» —
    прибиваем буквально, не чтением кода. `SESSION_KEYS` сейчас содержит РОВНО
    `RIGOR_KEYS` (`tui/session_settings.py`) — третьего, истинно-несвязанного
    ключа для оверрайда нет, поэтому честная проверка: до шага сессия несёт
    ЧАСТИЧНЫЙ оверрайд одного ключа пары (нестандартная, «обрезанная» custom-
    ситуация) — после `apply_toggle(rigor, preset)` оверрайды полностью
    замещены целевой парой (не остались частичными, не были обнулены)."""
    pytest.importorskip("textual")
    from gc_tui.toggles import apply_toggle

    app = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n',
    )
    app.apply_session_overrides(
        {"tui.finish_gate_mode": "on"}
    )  # частичный оверрайд

    apply_toggle(app, "rigor", "low")

    assert app._session_overrides == {
        "tui.finish_gate_mode": "off",
        "tui.text_answer_mode": "allow",
    }


def test_smart_gate_counts_failed_and_subagent_calls():
    """§8: судья зовётся при >=1 вызове, включая упавший; счётчик через State-поле.
    (Инкремент упавшего проверяется размещением ДО try в process_tool_calls — здесь
    фиксируем гейт-логику guard на прямом значении счётчика.)"""
    from types import SimpleNamespace

    from gc_core.intent_guard.guard import FinishGateGuard
    from gc_testkit.settings import fake_ctx

    cfg = {
        "backend.api_mode": "chat_completions",
        "agent.strict_turn_finish_validation": True,
        "finish_gate_smart": True,
    }
    g0 = FinishGateGuard(
        ctx=fake_ctx(cfg),
        state=SimpleNamespace(parent_state_id=None, turn_tool_calls=0),
    )
    g1 = FinishGateGuard(
        ctx=fake_ctx(cfg),
        state=SimpleNamespace(parent_state_id=None, turn_tool_calls=1),
    )
    assert g0.enabled is False and g1.enabled is True


def test_turn_tool_calls_counter_survives_summary_compaction():
    """§3, §8: счётчик — поле `State`, не сообщение; компакция
    (`apply_summary_state_patch`) переписывает `state.messages`, `state.shared`,
    `state.dialog_steps_offset`, `state.previous_response_id` — но не
    `turn_tool_calls`: поле хода её не касается, переживает суммаризацию
    (сброс — только на границе хода, см.
    `test_state_counter_starts_zero_and_resets_on_flush`)."""
    from types import SimpleNamespace

    from gc_core.summarize.compaction import apply_summary_state_patch

    state = SimpleNamespace(
        messages=[{"role": "user", "content": "hi"}],
        shared=None,
        user_intent_log=[],
        current_user_intent=[],
        dialog_steps_offset=0,
        previous_response_id="resp1",
        turn_tool_calls=3,
    )
    apply_summary_state_patch(
        state=state,
        raw_summary="summary text",
        last_dialog_steps="",
        summarized_messages_count=1,
    )
    assert state.turn_tool_calls == 3


@pytest.mark.asyncio
async def test_settings_rigor_row_flat_children_dim(tmp_path):
    """§8, §4.2 (ревизия 7 — flat-группы): пресет-строка «rigor · строгость
    завершения хода» — целиком bold INK, без DIM. Ребёнок — БЕЗ маркера «· »
    перед head (`_modes_tree_glyph` упразднён вместе с маркером — DIM
    начинается СРАЗУ после курсора, ничего не съедает место перед именем):
    имя+описание (head) И подпись значения — целиком DIM, иерархия несётся
    только цветом, не позицией/маркером (DEEP_DIM у пустых позиций трека
    ("○") — семантический цвет незакрашенного глифа, не маркер, и остаётся у
    всех строк одинаково — трек этот тест не проверяет). Текст строк уже
    прибит `test_modes_subscreen_writes_global` в test_settings_screen.py;
    здесь — стиль, мирроринг test_palette_toggles.py::
    test_child_rows_always_dim_flat_and_track_aligned_with_parent)."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui.screens.settings import SettingsScreen

    app = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "deny"\n',
    )
    async with app.run_test() as pilot:
        scr = SettingsScreen(
            settings=app.base_settings(),
            config_path=app._config_path,
            on_apply=app.reload_config,
        )
        app.push_screen(scr)
        await pilot.pause()

        rigor_text = scr._mode_row_text("rigor", selected=False)
        assert any(
            "bold" in str(s.style).lower()
            and p.INK.lower() in str(s.style).lower()
            for s in rigor_text.spans
        )
        assert not any(
            p.DIM.lower() in str(s.style).lower() for s in rigor_text.spans
        )

        gate_text = scr._mode_row_text("gate", selected=False)

        # Имя+описание ребёнка (head, включая имя команды) — DIM.
        gate_plain = gate_text.plain
        dim_spans = [
            s for s in gate_text.spans if p.DIM.lower() in str(s.style).lower()
        ]
        assert (
            dim_spans and dim_spans[0].start == 2
        )  # сразу после курсора — БЕЗ отступа/маркера
        assert dim_spans[0].end >= 2 + len(
            "gate · самопроверка выполнения задачи"
        )

        # Подпись значения ребёнка ("on") — тоже DIM (трек между head и
        # подписью — семантические цвета, не задет).
        value_start = gate_plain.rindex("on")
        assert any(
            p.DIM.lower() in str(s.style).lower()
            and s.start <= value_start < s.end
            for s in gate_text.spans
        )


def test_exactly_one_preset_group_today():
    """Guard (ревью W1, теперь уместен — ревизия 7 ввела родовой разделитель
    групп): ровно ОДНА preset-группа в TOGGLES сегодня. Когда появится вторая
    независимая группа (§9 «не в скоупе» — режимы доступа, задел готов) — этот
    тест падает, fail-closed указатель на разделитель-механизм
    (`command_palette.py::_group_sep_count` + `SettingsScreen._mount_modes_section`,
    оба покрыты юнитом на синтетическом 2-group реестре — см.
    `test_palette_toggles.py::test_group_separator_between_two_preset_groups`
    и `test_settings_screen.py::test_modes_section_group_separator_between_two_preset_groups`)."""
    from gc_tui.toggles import TOGGLES

    n_groups = sum(1 for spec in TOGGLES.values() if spec.preset_keys)
    assert n_groups == 1


def test_shipped_templates_resolve_to_medium():
    """§8: «дефолт поставки: схема + оба шаблона = smart+judge → свежая установка
    резолвится в medium» — половина (схема) уже прибита в test_rigor_presets.py
    (`resolve_rigor_state(make_settings()) == "medium"`) и в test_config_default_template.py
    (config.default.yml не расходится с pydantic-дефолтами); здесь — прямой
    гвоздь по ОБОИМ файлам поставки (demo_config.yml, config.default.yml):
    парсим и валидируем схемой, как настоящий загрузчик, без файловых
    side-effect'ов загрузки (XDG/project-оверлеи не при чём)."""
    from pathlib import Path

    import yaml
    from gc_core.config import _default_config_path
    from gc_core.config_modules.root import AppConfig
    from gc_tui.toggles import resolve_rigor_state

    demo_path = REPO / "demo_config.yml"
    for path in (Path(_default_config_path()), demo_path):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        app_cfg = AppConfig.model_validate(raw)
        assert resolve_rigor_state(app_cfg) == "medium", (
            f"{path.name}: не резолвится в medium"
        )


@pytest.mark.asyncio
async def test_help_screen_lists_gate_and_answers_details():
    """§8: «/help и Settings перечисляют детальные» — HelpScreen строится из
    `CommandRegistry.list_commands()` целиком, без фильтра по `palette_default`
    (тот фильтр — только у подвала палитры, спека §4.1) — детальные `/gate`,
    `/answers` остаются в `/help` всегда, даже когда в подвале скрыты по
    умолчанию."""
    pytest.importorskip("textual")
    from gc_tui.screens.help import HelpScreen
    from textual.app import App

    class _A(App):
        def on_mount(self):
            self.push_screen(HelpScreen())

    async with _A().run_test() as pilot:
        cmds = "\n".join(
            str(s.render()) for s in pilot.app.screen.query(".help-row-cmd")
        )
        assert "/gate" in cmds and "/answers" in cmds and "/rigor" in cmds


@pytest.mark.asyncio
async def test_help_detail_toggles_carry_rigor_setting_cue():
    """Дочерние тогглы пресета (/gate,/answers) в /help несут текстовый хвост
    «настройка /rigor» — связь с родителем словом, а не цветом (серый в плоском
    каталоге читался бы как «выключено»). Родитель /rigor хвоста не несёт.
    Generic по `owning_preset_name` — без хардкода имён."""
    pytest.importorskip("textual")
    from gc_tui.screens.help import HelpScreen
    from textual.app import App

    class _A(App):
        def on_mount(self):
            self.push_screen(HelpScreen())

    async with _A().run_test() as pilot:
        rows = {}
        for row in pilot.app.screen.query(".help-row"):
            cells = list(row.query("Static"))
            if len(cells) >= 2:
                rows[str(cells[0].render())] = str(cells[1].render())
        assert "настройка /rigor" in rows.get("/gate", "")
        assert "настройка /rigor" in rows.get("/answers", "")
        assert "настройка /" not in rows.get("/rigor", "")


def test_process_tool_calls_increments_counter_even_on_tool_failure(
    monkeypatch,
):
    """§8 (закрывает пробел, оставленный комментарием в тесте выше): счётчик
    инкрементится ДО try в process_tool_calls — упавший вызов (ошибка аргументов)
    считается наравне с успешным. Инкремент полностью generic по имени тула (не
    завершитель хода, см. TURN_FINALIZING_TOOLS) — тем же путём идёт и вызов
    сабагента, без спецкейса по имени тула."""
    from types import SimpleNamespace

    from gc_core import processing_responses as pr

    # Класс исключения берём ОТТУДА, где его ловят. Соседние тесты подменяют
    # `sys.modules["gc_core.tools"]`, и свежий импорт по строковому пути даёт
    # ДРУГОЙ объект модуля: `except` в проде тогда не совпадает с тем, что
    # бросил дабл, и отказ одного вызова рвёт весь батч. В изоляции тест это
    # не показывает — только на всём файле.
    WrongFunctionArgumentsException = pr.WrongFunctionArgumentsException

    async def _boom(**kw):
        raise WrongFunctionArgumentsException(
            "bad args", "{}", tool_name=kw["tool_name"]
        )

    monkeypatch.setattr(pr, "aapply_tool", _boom)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(
        pr, "amaybe_update_intent_scope_from_tool", lambda **kw: None
    )

    class _State:
        messages = []

        def __init__(self):
            self.session_compact_applied = False
            self.turn_tool_calls = 0
            # Per-run счётчик для agent.max_tool_calls — контрактное поле State
            # (не getattr-фоллбэк), поэтому дабл обязан его инициализировать.
            self.tool_calls = 0

    state = _State()
    from gc_testkit.settings import fake_ctx

    ctx = fake_ctx(logger=None)
    messages = [
        {
            "type": "function_call",
            "name": "run_subagent",
            "call_id": "c1",
            "arguments": "{}",
        },
    ]
    # Ошибка аргументов — output этого вызова, батч не прерывается.
    outs = run_coro(
        pr.aprocess_tool_calls(messages, state, ctx, run_agent_func=None)
    )
    assert [o["call_id"] for o in outs] == ["c1"]

    assert state.turn_tool_calls == 1
