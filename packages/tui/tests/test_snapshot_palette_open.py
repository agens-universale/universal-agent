"""Command palette overlay renders correctly when typing a slash command."""

import pytest
from gc_testkit.tui_support import tui_settings, wait_until

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


# Скиллы фиксированы тестом (спека P5 §5.6): палитра показывает их строками
# после команд; `pack:status` скрыт — имя занято командой /status.
_SKILLS = [
    {
        "id": "superpowers:systematic-debugging",
        "pack_id": "superpowers",
        "description": "Поиск причины ошибки по шагам",
    },
    {"id": "skill-creator", "description": "Создать новый скилл"},
    {"id": "pack:status", "pack_id": "pack", "description": "скрыт"},
]


def test_palette_open_snapshot(
    snap_compare,
    tmp_path,
    monkeypatch,
    skills_warm,  # noqa: ARG001  # фикстура-прогрев
):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.services import skills_scan

    # Папку в шапке фиксируем — иначе снапшот стал бы машинно-зависимым.
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
    # Подменяется только обход скиллов прогрева палитры: подмена всего
    # реестра доставалась бы и рантайму агента, и тот падал бы строкой
    # `! runtime: …` в ленте кадра.
    monkeypatch.setattr(skills_scan, "_scan", lambda settings: list(_SKILLS))

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
    )

    async def setup(pilot):
        await wait_until(
            pilot, lambda: pilot.app.screen._skills_cache, "прогрев скиллов"
        )
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/s"  # triggers palette open with filter "s"
        # Палитра сжимает ленту, и та встаёт на якорь внизу не сразу — ждём
        # этого, а не фиксированную паузу. Ползунок Textual получает размеры
        # посреди компоновки кадра и порой остаётся нарисованным по прошлой
        # позиции до следующей перерисовки — перерисовываем его явно.
        trace = pilot.app.query_one("#trace")
        await wait_until(
            pilot,
            lambda: (
                pilot.app.screen._palette is not None
                and trace.max_scroll_y > 0
                and trace.scroll_y == trace.max_scroll_y
                and trace.vertical_scrollbar.position == trace.scroll_y
            ),
            "лента на якоре под палитрой",
        )
        trace.vertical_scrollbar.refresh()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 24), run_before=setup)
