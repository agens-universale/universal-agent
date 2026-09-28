"""/rigor в палитре: применение пары через generic apply_toggle, custom —
только отображение (не выбирается стрелкой): резолвер сам показывает custom,
когда пара нестандартна; дети /gate,/answers видны в подвале всегда, flat и
серые (спека ревизии 7, §2, §4.1)."""

import pytest


def _app(tmp_path, yaml_text):
    from gc_tui.app import TuiApp

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    app = TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()
    app._threads_dir = str(tmp_path / "threads")
    app.tui_state.active_task_id = "t1"
    return app, cfg_file


def test_apply_rigor_preset_writes_pair_to_session(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.toggles import apply_toggle, toggle_state

    app, cfg_file = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "deny"\n',
    )
    got = apply_toggle(app, "rigor", "low")
    assert got == "low"
    eff = app.effective_config()
    assert (
        eff["tui_finish_gate_mode"] == "off"
        and eff["tui_text_answer_mode"] == "allow"
    )
    assert toggle_state(app, "rigor") == "low"
    # config.yml не тронут (сессия)
    assert 'finish_gate_mode: "on"' in cfg_file.read_text(encoding="utf-8")


def test_apply_toggle_rejects_custom(tmp_path):
    """Ревизия 3 (Г-А): custom — не применяемое значение (режим настройки, не
    пара) — apply_toggle(..., "custom") обязан упасть ValueError, роутинг
    (main.py) обязан перехватывать её ДО apply_toggle через is_preset_custom."""
    pytest.importorskip("textual")
    from gc_tui.toggles import apply_toggle

    app, cfg_file = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "allow"\n',
    )
    original = cfg_file.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        apply_toggle(app, "rigor", "custom")

    assert app._session_overrides == {}
    assert cfg_file.read_text(encoding="utf-8") == original


def test_nonstandard_pair_shows_custom_step_onto_preset_replaces_it(tmp_path):
    """Пара нестандартна (накручена детальными /gate,/answers) → резолвер сам
    показывает custom (extra=True, спека §2); шаг стрелкой на /rigor не может
    попасть на custom (она не позиция трека, ревизия 4) — цикл идёт от idx 0
    (эффективное вне spec.names) и заменяет пару целиком."""
    pytest.importorskip("textual")
    from gc_tui.toggles import (
        apply_toggle,
        next_toggle_value,
        preset_track_extra,
        toggle_state,
    )

    # глобал СТАНДАРТНЫЙ (medium), сессия накручена в дикую пару on+allow через детальные
    app, _ = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n',
    )
    app.apply_session_overrides(
        {"tui.finish_gate_mode": "on", "tui.text_answer_mode": "allow"}
    )
    assert toggle_state(app, "rigor") == "custom"
    assert (
        preset_track_extra(app, "rigor") is True
    )  # пара нестандартна — 4-я горит
    # шаг стрелкой на пресет: "custom" вне spec.names -> idx 0, +1 -> idx 1 -> medium
    nxt = next_toggle_value(app, "rigor", 1)
    assert nxt == "medium"
    apply_toggle(app, "rigor", nxt)
    assert toggle_state(app, "rigor") == nxt
    assert (
        preset_track_extra(app, "rigor") is False
    )  # пара снова стандартна — 4-я не горит


@pytest.mark.asyncio
async def test_rigor_palette_cycles_three_values(tmp_path):
    """Живой прогон: /rigor в палитре, ←/→ ходит low/medium/high, сессия меняется,
    config.yml не тронут, палитра открыта."""
    pytest.importorskip("textual")
    app, cfg_file = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n',
    )
    original = cfg_file.read_text(encoding="utf-8")
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/rigor"
        await pilot.pause(0.1)
        await pilot.press("right")  # medium -> high
        await pilot.pause(0.05)
        assert app.effective_config()["tui_finish_gate_mode"] == "on"
        assert cfg_file.read_text(encoding="utf-8") == original
        assert pilot.app.query(".palette")
        await pilot.press("left")  # high -> medium
        await pilot.press("left")  # medium -> low
        await pilot.pause(0.05)
        assert app.effective_config()["tui_finish_gate_mode"] == "off"
        assert app.effective_config()["tui_text_answer_mode"] == "allow"


def _toggle_row_text(palette, name):
    """Найти живую (смонтированную) toggle-строку по имени команды и вернуть её
    plain-текст — вспомогательная функция для e2e-прогонов ниже (только
    pilot.press ведёт взаимодействие, эта функция только читает результат)."""
    for row in palette.query(".palette-toggle-row"):
        plain = row.render().plain
        tokens = plain.split()
        if tokens and tokens[0] == f"/{name}":
            return plain
    return None


@pytest.mark.asyncio
async def test_child_spin_shows_custom_without_moving_rows_then_preset_step_clears_it(
    tmp_path,
):
    """Гвоздь ревизии 4, обновлён под ревизию 7 (спека §2, §4.1, §8): состав
    подвала статичен (дети /gate,/answers видны ВСЕГДА под /rigor, независимо
    от выделения — видимость-по-выделению ревизий 5/6 снесена). Крутка
    ребёнка НА МЕСТЕ (на /gate) меняет ОДИН ключ сессии, /rigor сам
    резолвится в custom (● BRAND в зарезервированном месте), а состав и
    геометрия подвала (число строк, колонки трека/подписи) НЕ меняются вовсе
    — не только «пока в группе», а всегда, по построению. Шаг /rigor с
    отображаемого custom заменяет пару и уводит на пресет — дети остаются
    видны. Весь роутинг — через `pilot.press`, как делает пользователь."""
    pytest.importorskip("textual")
    from gc_tui.toggles import toggle_state

    app, cfg_file = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n',
    )
    original = cfg_file.read_text(encoding="utf-8")
    async with app.run_test() as pilot:
        await pilot.press("/")
        await pilot.pause(0.1)
        palette = pilot.app.query_one(".palette")

        # Ревизия 7: подвал уже показывает все строки, до всякой навигации.
        # level/profiles — группа «чем работаем» перед rigor (спека §12.1).
        rows_outside = [
            row.render().plain for row in palette.query(".palette-toggle-row")
        ]
        assert [r.split()[0] for r in rows_outside] == [
            "/level",
            "/profiles",
            "/rigor",
            "/gate",
            "/answers",
        ]

        # Спускаемся до /gate (level, profiles, rigor — впереди, +2 шага).
        regular_count = len(palette._visible_rows())
        for _ in range(
            regular_count + 3
        ):  # /level -> /profiles -> /rigor -> /gate
            await pilot.press("down")
        await pilot.pause(0.05)
        assert palette.selected_command().name == "gate"

        rows_before = [
            row.render().plain for row in palette.query(".palette-toggle-row")
        ]
        assert [r.split()[0] for r in rows_before] == [
            "/level",
            "/profiles",
            "/rigor",
            "/gate",
            "/answers",
        ]
        columns_before = palette._toggle_columns()

        await pilot.press(
            "right"
        )  # gate: smart -> on (single-key цикл на месте)
        await pilot.pause(0.05)

        # (a) сессионная пара изменилась ТОЛЬКО по этому ключу.
        assert app.effective_config()["tui_finish_gate_mode"] == "on"
        assert (
            app.effective_config()["tui_text_answer_mode"] == "judge"
        )  # answers не тронут
        assert (
            cfg_file.read_text(encoding="utf-8") == original
        )  # всё ещё сессия, не файл

        # (b) /rigor сам резолвится в custom: база тусклая, 4-я — применённая
        # ● BRAND в зарезервированном месте.
        rigor_row_tuned = _toggle_row_text(palette, "rigor")
        assert rigor_row_tuned is not None
        assert rigor_row_tuned.rstrip().endswith("custom")
        assert "○ ○ ○  ●" in rigor_row_tuned

        # (c) состав и геометрия подвала не изменились — состав статичен,
        # не зависит от выделения (ревизия 7).
        rows_after = [
            row.render().plain for row in palette.query(".palette-toggle-row")
        ]
        assert [r.split()[0] for r in rows_after] == [
            "/level",
            "/profiles",
            "/rigor",
            "/gate",
            "/answers",
        ]
        assert len(rows_after) == len(rows_before)
        assert palette._toggle_columns() == columns_before

        # --- шаг /rigor с отображаемого custom — уводит на пресет ----------
        await pilot.press("up")  # /gate -> /rigor (остаёмся в группе)
        await pilot.pause(0.05)
        assert palette.selected_command().name == "rigor"

        await pilot.press("right")  # custom (idx0) +1 -> medium
        await pilot.pause(0.05)

        assert app.effective_config()["tui_finish_gate_mode"] == "smart"
        assert app.effective_config()["tui_text_answer_mode"] == "judge"
        assert cfg_file.read_text(encoding="utf-8") == original
        assert toggle_state(app, "rigor") == "medium"

        # Состав по-прежнему статичен — дети видны, как и всегда.
        rows_final = [
            row.render().plain for row in palette.query(".palette-toggle-row")
        ]
        assert [r.split()[0] for r in rows_final] == [
            "/level",
            "/profiles",
            "/rigor",
            "/gate",
            "/answers",
        ]


def test_filtered_gate_row_is_dimmed_child_flat():
    """Ревизия 7 (спека §2): flat — нет отступа НИГДЕ, включая одиночного
    ребёнка, найденного фильтром при невидимом родителе (спец-случай
    ревизии 6 «одиночный = обычная строка» отменён — серый сигнал ребёнка
    действует ВЕЗДЕ). Набранный фильтр «gate», под который родитель /rigor
    не подходит (не попадает в базу), не мешает `/gate` рисоваться DIM —
    принадлежность пресету («owning preset», `_owning_preset`) — чистые
    данные, видимость родителя её больше не гейтит."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    state = {"rigor": "high", "gate": "on", "answers": "deny"}
    pal = CommandPalette(
        reg, initial_filter="", toggle_state=lambda n: state[n]
    )

    pal.set_filter(
        "gate"
    )  # /rigor фильтру «gate» не матчит -> родитель не в базе
    assert [c.name for c in pal._visible_toggle_rows()] == ["gate"]
    gate_row = pal._format_toggle_row(reg.lookup("gate"))
    assert gate_row.plain.startswith(
        " /gate"
    ) and not gate_row.plain.startswith("  /gate")  # flat
    assert any(
        p.DIM.lower() in str(s.style).lower() for s in gate_row.spans
    )  # но серый (ревизия 7)

    pal.set_filter(
        ""
    )  # фильтр очищен -> /rigor снова в базе, дети видны всегда
    assert [c.name for c in pal._visible_toggle_rows()] == [
        "level",
        "profiles",
        "rigor",
        "gate",
        "answers",
    ]
    gate_row_expanded = pal._format_toggle_row(reg.lookup("gate"))
    assert gate_row_expanded.plain.startswith(
        " /gate"
    ) and not gate_row_expanded.plain.startswith("  /gate")
    assert any(
        p.DIM.lower() in str(s.style).lower() for s in gate_row_expanded.spans
    )


def test_filter_matching_parent_shows_children_flat():
    """Ревизия 7 (спека §2, §4.1): фильтр правит ТОЛЬКО составом базы
    подвала (какие тогглы вообще в неё попадают) — дети пресета, попавшего в
    базу, вставляются ВСЕГДА, без условия выделения (композиция статична).
    «ri» матчит только /rigor (регулярная зона под этим фильтром пуста) —
    дети /gate,/answers появляются сразу, без навигации."""
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    state = {"rigor": "medium", "gate": "smart", "answers": "judge"}
    pal = CommandPalette(
        reg, initial_filter="", toggle_state=lambda n: state[n]
    )

    pal.set_filter("ri")  # startswith -> матчит только /rigor
    assert [c.name for c in pal._visible_toggle_rows()] == [
        "rigor",
        "gate",
        "answers",
    ]
    gate_row = pal._format_toggle_row(reg.lookup("gate"))
    assert gate_row.plain.startswith(
        " /gate"
    ) and not gate_row.plain.startswith("  /gate")  # flat, но ребёнок


def test_preset_custom_hint_two_states(tmp_path):
    """Два состояния хинта отображаемого custom (ревизия 4, спека §5, тексты
    ДОСЛОВНО) — источник нестандартности пары: сессионные оверрайды или сам
    глобал. Состояние «настройка пары: крутите /gate и /answers…» ревизии 3
    (курсор на custom, пара ещё стандартна) умерло вместе с курсором — хинт
    теперь зовётся, только когда custom реально отображён."""
    pytest.importorskip("textual")
    from gc_tui.toggles import preset_custom_hint

    # 1) пара нестандартна ИЗ-ЗА самого глобала (оверрайдов пары нет).
    app1, _ = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "allow"\n',
    )
    assert preset_custom_hint(app1, "rigor") == (
        "ваш дефолт: самопроверка on, формат allow · менять в /settings"
    )

    # 2) пара нестандартна ИЗ-ЗА сессионных оверрайдов (хотя бы один ключ пары
    # в оверрайдах) — глобал при этом стандартный.
    app2, _ = _app(
        tmp_path,
        'tui:\n  finish_gate_mode: "smart"\n  text_answer_mode: "judge"\n',
    )
    app2.apply_session_overrides(
        {"tui.finish_gate_mode": "on", "tui.text_answer_mode": "allow"}
    )
    assert preset_custom_hint(app2, "rigor") == (
        "своя пара этой сессии: самопроверка on, формат allow · шаг на пресет заменит пару · закрепить — в /settings"
    )
