"""Левая часть футера — две строки: режим и хвост команд сверху, временное
снизу.

Строка 1: пилюля режима разрешений («▸▸ edit mode (Shift+Tab)») и хвост
команд (Ctrl+C/stop во время хода). Строка 2 (слот): бейдж уведомлений или
временная подсказка. F-подсказок в футере нет ни в каком состоянии
(решение владельца): постоянный индикатор режима важнее списка клавиш, клавиши
перечислены в справке (F1), а место нужно правой колонке — там живут стрелка
перехода и сегмент «запрос».
(/skills и /export из футера убраны — доступны через палитру; /mcp не дублируем —
подсказка /mcp в бренд-карточке.)

[Esc] back/выход убраны — на main Esc контекстуален (clear/exit/stop),
поведение досказывается confirm-hint в footer-right.
"""

from gc_testkit.tui_support import tui_settings
from footer_text import footer_left, footer_rows
import json
from pathlib import Path

import pytest

_PILL = "▸▸ edit mode (Shift+Tab)"
_F_KEYS = (
    "F1/help",
    "F2/sessions",
    "F3/diff",
    "F5/config",
    "F6/settings",
    "F7/buffer",
)


def _write_task(tmp_path, tid):
    meta = {
        "thread_id": tid,
        "status": "stopped",
        "updated_at": "2026-05-28T12:00:00+00:00",
        "messages_count": 0,
    }
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

    async with TuiApp().run_test() as pilot:
        rendered = footer_left(pilot.app)
        assert rendered == _PILL
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

    tid = "idle1"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.active_runner = None
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause()

        rendered = footer_left(pilot.app)
        assert rendered == _PILL
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

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    tid = "run1"
    _write_task(tmp_path, tid)
    for width in (200, 64):
        app = TuiApp(
            threads_dir=str(tmp_path),
            system="cli",
            settings=tui_settings({"agent.model": "M"}),
        )
        async with app.run_test(size=(width, 30)) as pilot:
            pilot.app._update_available = True  # непустой бейдж уведомлений
            pilot.app.screen.attach_active_task(tid)
            pilot.app.active_runner = _FakeRunner()
            # Footer обновляется в _poll_active каждые 250ms — подождём один тик.
            await pilot.pause(0.4)

            rendered = footer_left(pilot.app)
            mode, slot = footer_rows(pilot.app)
            assert "Ctrl+C/stop" in mode, width
            assert "F4/notices" in slot, width  # бейдж виден всегда
            assert mode.startswith("▸▸"), width  # режим виден всегда
            for key in _F_KEYS:
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

    class _FakeRunner:
        def __init__(self) -> None:
            self._running = True

        def is_running(self) -> bool:
            return self._running

        def stop(self) -> None:
            self._running = False

    tid = "trans1"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        runner = _FakeRunner()
        pilot.app.active_runner = runner
        await pilot.pause(0.4)
        rendered = footer_left(pilot.app)
        assert "/stop" in rendered

        runner.stop()
        await pilot.pause(0.4)
        rendered = footer_left(pilot.app)
        assert "/stop" not in rendered
        assert rendered == _PILL
        assert "Ctrl+a" not in rendered


@pytest.mark.asyncio
async def test_footer_badge_counts_update_notice():
    """«Доступна новая версия» попадает в счётчик ФУТЕРА: он виден всегда.

    В карточке этот же факт показан отдельной строкой — там в счётчик он не идёт,
    иначе одно и то же утверждалось бы дважды. Футер такой строки не имеет: без
    счётчика признак исчезает вместе с карточкой при скролле."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app._update_available = True
        screen = pilot.app.screen_stack[0]
        screen._refresh_notices_footer()
        await pilot.pause()
        footer = footer_left(pilot.app)
        assert "notices" in footer  # счётчик появился


@pytest.mark.asyncio
async def test_footer_mode_row_and_badge_slot_live_apart():
    """Пилюля режима — в строке 1, бейдж уведомлений — в строке 2 (слот):
    ни один не вытесняет другой, и бейдж не встаёт в строку режима."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.notices_ui import INFO_GLYPH, WARN_GLYPH

    async with TuiApp().run_test() as pilot:
        pilot.app._update_available = True  # даёт непустой счётчик футера
        screen = pilot.app.screen_stack[0]
        screen._refresh_notices_footer()
        await pilot.pause()
        mode, slot = footer_rows(pilot.app)
        assert mode == _PILL
        assert slot == f"{INFO_GLYPH} 1 · F4/notices"
        # счётчики парой, без точки между собой
        assert f"{WARN_GLYPH} 1 · {INFO_GLYPH}" not in slot


@pytest.mark.asyncio
async def test_footer_slot_is_empty_without_notices():
    """Без уведомлений слот пуст: подсказки «F4» без счётчиков нет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.pause()
        assert footer_rows(pilot.app) == (_PILL, "")

    # набор для футера содержит уведомление об обновлении, для карточки — нет
    async with TuiApp().run_test() as pilot:
        pilot.app._update_available = True
        footer_codes = {n.code for n in pilot.app.collect_footer_notices()}
        card_codes = {n.code for n in pilot.app.collect_unread_notices()}
        assert "update_available" in footer_codes
        assert "update_available" not in card_codes


@pytest.mark.asyncio
async def test_hint_takes_the_slot_and_mode_row_keeps_stop_key(tmp_path):
    """Подсказка и отказ занимают только слот: строка 1 по-прежнему держит
    пилюлю и `Ctrl+C/stop`. Подсказка гаснет обратно в бейдж."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"agent.model": "M"}))
    async with app.run_test(size=(120, 30)) as pilot:
        pilot.app._update_available = True
        pilot.app.tui_state.active_task_id = "t"
        pilot.app.active_runner = _RunningRunner()
        scr = pilot.app.screen
        await pilot.pause(0.4)
        scr._show_footer_hint("ПОДСКАЗКА", seconds=60)
        await pilot.pause()
        assert footer_rows(scr) == (f"{_PILL}  Ctrl+C/stop", "ПОДСКАЗКА")
        scr._show_footer_refusal("✗ не сохранено: причина")
        await pilot.pause(0.4)  # тик опроса подсказку не затирает
        mode, slot = footer_rows(scr)
        assert mode == f"{_PILL}  Ctrl+C/stop"
        assert slot == "✗ не сохранено: причина"
        scr._clear_footer_hint()
        await pilot.pause()
        assert footer_rows(scr)[1] == "ℹ 1 · F4/notices"


@pytest.mark.asyncio
async def test_long_slot_text_is_one_row_with_ellipsis_at_min_width():
    """Минимальная ширина футера: длинная справка ручки не переносится —
    одна строка с многоточием, высота футера прежняя."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.footer_slot import (
        MAIN_FOOTER_MIN_COLS,
        toggle_info_line,
    )

    text = toggle_info_line("answers", "free_text_with_long_value")
    async with TuiApp().run_test(size=(MAIN_FOOTER_MIN_COLS, 24)) as pilot:
        scr = pilot.app.screen
        scr._show_footer_hint(text, seconds=60)
        await pilot.pause()
        slot = scr.query_one("#footer-slot")
        assert slot.region.height == 1
        assert scr.query_one("#footer").region.height == 2
        strips = scr._compositor.render_strips()
        row = "".join(seg.text for seg in strips[slot.region.y])
        shown = row[slot.region.x : slot.region.right].rstrip()
        assert shown.endswith("…"), shown
        assert shown[:-1] in text


@pytest.mark.asyncio
async def test_footer_columns_never_touch_and_badge_sits_in_row_two():
    """Между колонками футера ВСЕГДА минимум 2 пробела, а бейдж уведомлений
    стоит во второй строке левой части (футер height:2).

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
        app = TuiApp(settings=tui_settings({"agent.model": model}))
        async with app.run_test(size=(width, 30)) as pilot:
            # счётчик уведомлений — содержимое второй строки левой части
            pilot.app._update_available = True
            pilot.app.screen._refresh_notices_footer()
            await pilot.pause()
            foot = pilot.app.screen.query_one("#footer")
            right = pilot.app.screen.query_one("#footer-right")
            strips = [
                "".join(seg.text for seg in strip)
                for strip in pilot.app.screen._compositor.render_strips()
            ]
            lines = [
                strips[foot.region.y + i] for i in range(foot.region.height)
            ]
            for i, line in enumerate(lines):
                head = line[: right.region.x]
                if not head.strip():
                    continue
                gap = len(head) - len(head.rstrip())
                assert gap >= 2, (
                    f"ширина {width}, строка {i + 1}: зазор {gap} — колонки слиплись\n  [{line.rstrip()}]"
                )
            if len(lines) > 1 and lines[1][: right.region.x].strip():
                wrapped_somewhere = True
    assert wrapped_somewhere, "бейдж не встал во вторую строку"


class _RunningRunner:
    def is_running(self) -> bool:
        return True

    def stop(self) -> None:
        pass


async def _running_footer(width: int, *, badge: bool) -> str:
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"agent.model": "some-model-name"}))
    async with app.run_test(size=(width, 30)) as pilot:
        pilot.app._update_available = badge
        pilot.app.tui_state.active_task_id = "t"
        pilot.app.active_runner = _RunningRunner()
        await pilot.pause(0.6)  # тик опроса пересобирает футер по месту
        return footer_left(pilot.app)


@pytest.mark.asyncio
async def test_footer_pill_shrinks_to_glyph_when_stop_key_needs_the_room():
    """Узкое окно во время хода: имя режима и подпись хоткея уходят, глиф и
    команда остановки остаются в одном ряду."""
    pytest.importorskip("textual")
    assert await _running_footer(70, badge=False) == f"{_PILL}  Ctrl+C/stop"
    assert await _running_footer(44, badge=False) == "▸▸  Ctrl+C/stop"
    assert await _running_footer(44, badge=True) == (
        "▸▸  Ctrl+C/stop\nℹ 1 · F4/notices"
    )


@pytest.mark.asyncio
async def test_footer_stop_key_outlives_the_mode_glyph():
    """Места нет даже на глиф рядом с командой остановки — уступает глиф:
    другого способа узнать, чем остановить агента, на экране нет. Шире —
    раньше снимается имя модели (справа остаётся точка уровня), и глиф
    остаётся."""
    pytest.importorskip("textual")
    rendered = await _running_footer(20, badge=True)
    assert rendered.split("\n")[0] == "Ctrl+C/stop"
    assert "▸" not in rendered
    for width in (22, 32, 36):
        wider = await _running_footer(width, badge=True)
        assert wider.split("\n")[0] == "▸▸  Ctrl+C/stop", width
