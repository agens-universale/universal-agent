"""Footer-left контекстно зависит от наличия задачи и активного run.

Idle: F1/help  F2/sessions  F3/diff  F5/config  F6/settings  F7/buffer
Running: Ctrl+C/stop — и больше ничего (Task 17, решение юзера): F-подсказки во
время хода не показываются никогда, место нужно правой колонке — там живут
стрелка перехода и сегмент «запрос».
(/skills и /export из футера убраны — доступны через палитру; /mcp не дублируем —
подсказка /mcp в бренд-карточке.)

[Esc] back/выход убраны — на main Esc контекстуален (clear/exit/stop),
поведение досказывается confirm-hint в footer-right.
"""

import json
from pathlib import Path

import pytest


def _notices(*, warnings=0, infos=0):
    """Уведомления под бейдж — ровно столько, сколько просят по уровням."""
    from gc_core.startup_notices import Notice

    return [Notice("warning", f"w{i}", "предупреждение") for i in range(warnings)] + [
        Notice("info", f"i{i}", "сообщение") for i in range(infos)
    ]


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "stopped", "updated_at": "2026-05-28T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta),
        encoding="utf-8",
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_footer_left_empty_state_no_back_no_stop_no_export():
    """Empty state: F-hotkeys chrome; без /mcp, [Esc], /stop, /export, Ctrl+a."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    async with TuiApp().run_test() as pilot:
        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "F1/help" in rendered
        assert "F2/sessions" in rendered
        assert "F3/diff" in rendered
        assert "F5/config" in rendered
        assert "F6/settings" in rendered
        assert "F7/buffer" in rendered
        assert "[F1]" not in rendered
        assert "/mcp" not in rendered
        assert "/skills" not in rendered
        assert "/stop" not in rendered
        assert "/export" not in rendered
        assert "Esc" not in rendered
        assert "back" not in rendered
        assert "выход" not in rendered
        assert "Ctrl+a" not in rendered


@pytest.mark.asyncio
async def test_footer_left_task_idle_no_stop(tmp_path):
    """Task active, no active_runner → тот же набор команд, /stop НЕТ."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    tid = "idle1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.active_runner = None
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause()

        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "F2/sessions" in rendered
        assert "F1/help" in rendered
        assert "F5/config" in rendered
        assert "F3/diff" in rendered
        assert "F6/settings" in rendered
        assert "F7/buffer" in rendered
        assert "/mcp" not in rendered
        assert "/skills" not in rendered
        assert "/export" not in rendered
        assert "/stop" not in rendered
        assert "Esc" not in rendered
        assert "back" not in rendered
        assert "Ctrl+a" not in rendered


@pytest.mark.asyncio
async def test_footer_left_task_running_shows_only_stop_at_any_width(tmp_path):
    """Task active + active_runner.is_running() → слева ТОЛЬКО Ctrl+C/stop.

    Task 17 (решение юзера): F-подсказки во время хода не показываются никогда —
    ни на широком окне, ни на узком. Это безусловное правило, а не ступень
    деградации по ширине: пользователь клавиши уже знает, а место во время хода
    нужно правой колонке (стрелка перехода, сегмент «запрос»). Бейдж
    уведомлений — исключение: он виден всегда."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    tid = "run1"
    _write_task(tmp_path, tid)
    for width in (200, 60):
        app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
        async with app.run_test(size=(width, 30)) as pilot:
            pilot.app._update_available = True  # непустой бейдж уведомлений
            pilot.app.screen.attach_active_task(tid)
            pilot.app.active_runner = _FakeRunner()
            # Footer обновляется в _poll_active каждые 250ms — подождём один тик.
            await pilot.pause(0.4)

            rendered = str(pilot.app.query_one("#footer-left", Static).render())
            assert "Ctrl+C/stop" in rendered, width
            assert "F4/notices" in rendered, width  # бейдж виден всегда
            for key in ("F1/help", "F2/sessions", "F3/diff", "F5/config", "F6/settings", "F7/buffer"):
                assert key not in rendered, (width, key)
            assert "/mcp" not in rendered
            assert "/skills" not in rendered
            assert "/export" not in rendered
            assert "Esc" not in rendered
            assert "back" not in rendered


@pytest.mark.asyncio
async def test_footer_left_drops_stop_after_runner_finishes(tmp_path):
    """Когда runner перестаёт is_running() → /stop исчезает в следующий тик."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    class _FakeRunner:
        def __init__(self) -> None:
            self._running = True

        def is_running(self) -> bool:
            return self._running

        def stop(self) -> None:
            self._running = False

    tid = "trans1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        runner = _FakeRunner()
        pilot.app.active_runner = runner
        await pilot.pause(0.4)
        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "/stop" in rendered

        runner.stop()
        await pilot.pause(0.4)
        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "/stop" not in rendered
        assert "/sessions" in rendered
        assert "F5/config" in rendered
        assert "F7/buffer" in rendered
        assert "Ctrl+a" not in rendered


@pytest.mark.asyncio
async def test_footer_badge_counts_update_notice():
    """«Доступна новая версия» попадает в счётчик ФУТЕРА: он виден всегда.

    В карточке этот же факт показан отдельной строкой — там в счётчик он не идёт,
    иначе одно и то же утверждалось бы дважды. Футер такой строки не имеет: без
    счётчика признак исчезает вместе с карточкой при скролле."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    async with TuiApp().run_test() as pilot:
        pilot.app._update_available = True
        screen = pilot.app.screen_stack[0]
        screen._refresh_notices_footer()
        await pilot.pause()
        footer = str(pilot.app.query_one("#footer-left", Static).render())
        assert "notices" in footer  # счётчик появился


@pytest.mark.asyncio
async def test_footer_shows_notices_badge_and_buffer_key_together():
    """Суффикс уведомлений и F7/buffer живут в одной строке одновременно.

    Синк 2026-08-06: суффикс — наша сторона, F7/buffer пришёл из develop, и до этого
    теста ни один прогон не проверял их ВМЕСТЕ. Рефакторинг любой из половин мог
    незаметно вытеснить вторую."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    async with TuiApp().run_test() as pilot:
        pilot.app._update_available = True  # даёт непустой счётчик футера
        screen = pilot.app.screen_stack[0]
        screen._refresh_notices_footer()
        await pilot.pause()
        footer = str(pilot.app.query_one("#footer-left", Static).render())
        assert "F7/buffer" in footer  # клавиша develop не вытеснена
        assert "F4/notices" in footer  # наш суффикс на месте
        # счётчики парой, без точки между собой (решение владельца против варианта develop)
        from gc_tui.services.notices_ui import INFO_GLYPH, WARN_GLYPH

        assert f"{WARN_GLYPH} 1 · {INFO_GLYPH}" not in footer


@pytest.mark.asyncio
async def test_footer_badge_wraps_whole_and_dotless_on_narrow():
    """Узкое окно: блок уведомлений уходит вниз ЦЕЛИКОМ и без ведущей точки.

    Частичный перенос (счётчик наверху, «F4/notices» внизу) читался как два обрывка,
    а точка в начале строки ничего не разделяет (живая приёмка 2026-08-07)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    async with TuiApp().run_test(size=(48, 24)) as pilot:
        pilot.app._update_available = True  # непустой счётчик футера
        screen = pilot.app.screen_stack[0]
        screen._refresh_notices_footer()
        await pilot.pause()
        text = str(pilot.app.query_one("#footer-left", Static).render())
        assert "F4/notices" in text
        lines = text.split("\n")
        assert len(lines) == 2, f"блок не уехал вниз целиком: {text!r}"
        assert "F4/notices" in lines[1] and "notices" not in lines[0]
        assert not lines[1].lstrip().startswith("·"), "ведущая точка на строке без соседа"

    # набор для футера содержит уведомление об обновлении, для карточки — нет
    async with TuiApp().run_test() as pilot:
        pilot.app._update_available = True
        footer_codes = {n.code for n in pilot.app.collect_footer_notices()}
        card_codes = {n.code for n in pilot.app.collect_unread_notices()}
        assert "update_available" in footer_codes
        assert "update_available" not in card_codes


@pytest.mark.asyncio
async def test_footer_columns_never_touch_and_left_wraps_instead():
    """Между колонками футера ВСЕГДА минимум 2 пробела, а что не влезло — левая
    колонка переносит на вторую строку (футер height:2).

    Зазор не был зарезервирован вовсе: правая колонка (width:auto) забирает своё по
    содержимому, левая (1fr) получает остаток — и на длинном имени модели её хвост со
    счётчиками уведомлений упирался в модель вплотную (живая приёмка). Арифметика
    `_footer_right_available_width` про «≥1 пробел» решала только вопрос сворачивания
    правой части и на раскладку колонок не влияла."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    model = "qwen/qwen3.7-flash-super-long-model-name"
    wrapped_somewhere = False
    for width in (140, 110, 96, 90, 84):
        app = TuiApp(config={"model": model})
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            foot = pilot.app.screen.query_one("#footer")
            right = pilot.app.screen.query_one("#footer-right")
            strips = ["".join(seg.text for seg in strip) for strip in pilot.app.screen._compositor.render_strips()]
            lines = [strips[foot.region.y + i] for i in range(foot.region.height)]
            for i, line in enumerate(lines):
                head = line[: right.region.x]
                if not head.strip():
                    continue
                gap = len(head) - len(head.rstrip())
                assert gap >= 2, f"ширина {width}, строка {i + 1}: зазор {gap} — колонки слиплись\n  [{line.rstrip()}]"
            if len(lines) > 1 and lines[1][: right.region.x].strip():
                wrapped_somewhere = True
    assert wrapped_somewhere, "ни на одной ширине хвост левой части не перенёсся вниз"


def _rendered_rows(text: str, width: int) -> int:
    from rich.console import Console
    from rich.text import Text

    console = Console(width=width)
    return sum(max(1, len(Text(line).wrap(console, width))) for line in text.split("\n"))


def test_footer_left_never_renders_three_rows():
    """Инвариант Task 17: сборка левой колонки НИКОГДА не занимает три ряда.

    Футер высотой 2 — третий ряд просто обрезается, и обрезался бы именно бейдж
    уведомлений: он выселяется отдельной строкой ПОД команды, а команды на узкой
    ширине сами переносятся Rich'ем на два ряда. Раньше не всплывало, потому что
    левую колонку никогда не сужали.

    Перебор идёт с ОДНОЙ колонки (фикс-раунд 2 по ревью): ширины уже бейджа —
    не теория, туда левую загоняет безусловная последняя ступень лесенки правой
    колонки, и там сборка возвращала голый бейдж на три и больше рядов.

    Порядок жертв: подсказка «F4/notices» уходит раньше счётчиков, счётчики —
    раньше команд."""
    from gc_tui.services.notices_ui import footer_left_with_badge, format_notices_badge
    from rich.cells import cell_len

    commands = "F1/help  F2/sessions  F3/diff  F5/config  F6/settings  F7/buffer"
    # Бейдж собирает прод (`format_notices_badge`), а не тест руками: склеенное
    # «⚠3 ℹ2» разошлось бы с «⚠ 3  ℹ 2» прода молча — сторож ширины остался бы
    # зелёным на строке, которой в футере не бывает.
    notices = _notices(warnings=3, infos=2)
    counts = format_notices_badge(notices, with_hint=False)
    badge = format_notices_badge(notices)
    for width in range(1, 91):
        text = footer_left_with_badge(commands, badge, width=width)
        rows = _rendered_rows(text, width)
        assert rows <= 2, f"ширина {width}: {rows} ряда — третий обрежется: {text!r}"
        if width >= cell_len(counts):
            assert counts in text, f"ширина {width}: счётчики потеряны: {text!r}"


def test_footer_left_guard_keeps_stop_key_at_any_width():
    """`Ctrl+C/stop` виден ВСЕГДА — инвариант юзера, без оговорок про ширину.

    Регресс, найденный ревью: сторож двух рядов выбрасывал команды ЦЕЛИКОМ,
    когда бейдж не влезал в один ряд. Во время хода это оставляло пользователя
    без единственного способа остановить агента — и было хуже, чем на базе, где
    терялся хвост бейджа. Ужимается бейдж; команды — последнее, что снимается
    слева."""
    from gc_tui.services.notices_ui import footer_left_with_badge, format_notices_badge
    from rich.cells import cell_len

    commands = "Ctrl+C/stop"
    badge = format_notices_badge(_notices(warnings=4, infos=2))
    # Ниже собственной ширины стоп-клавишу не показать целиком ни при какой
    # раскладке — там остаётся только потолок в два ряда (тест выше).
    for width in range(cell_len(commands), 91):
        text = footer_left_with_badge(commands, badge, width=width)
        assert commands in text, f"ширина {width}: стоп-клавиша потеряна: {text!r}"
        assert _rendered_rows(text, width) <= 2, f"ширина {width}: {text!r}"
