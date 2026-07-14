"""Авто-скролл трейса: прилипаем к низу только если пользователь уже внизу.

Пока агент работает и юзер отскроллил вверх читать — новые события НЕ дёргают
вид вниз. Отправка своего сообщения (pending) форсит скролл вниз (re-engage)."""
import pytest


@pytest.mark.asyncio
async def test_at_bottom_true_when_no_overflow(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        assert scr._trace_is_at_bottom() is True   # пустой трейс — тривиально внизу


@pytest.mark.asyncio
async def test_autoscroll_skipped_when_scrolled_up(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        trace.scroll_end = lambda **kw: calls.append(kw)   # spy
        scr._stick_bottom = False                            # юзер отскроллил вверх
        scr._mount_trace_row(prefix="x ", content="новое", kind_class="line-meta")
        await pilot.pause(0.1)
        assert calls == []     # вид не дёрнулся вниз


@pytest.mark.asyncio
async def test_autoscroll_follows_when_at_bottom(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        trace.scroll_end = lambda **kw: calls.append(kw)
        scr._trace_is_at_bottom = lambda: True
        scr._mount_trace_row(prefix="x ", content="новое", kind_class="line-meta")
        await pilot.pause(0.1)                            # скролл отложен (call_after_refresh)
        assert calls    # прилипли к низу


@pytest.mark.asyncio
async def test_user_send_pending_forces_scroll(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        trace.scroll_end = lambda **kw: calls.append(kw)
        scr._trace_is_at_bottom = lambda: False          # даже если отскроллен вверх
        scr._mount_user_reply("привет", state="pending")  # отправка своего → форс
        await pilot.pause(0.1)                            # скролл отложен (call_after_refresh)
        assert calls    # отправка форсит скролл вниз


@pytest.mark.asyncio
async def test_thinking_indicator_unhide_does_not_scroll_trace(tmp_path):
    """Индикатор докнут в #indicator-bar (вне скролла), всегда виден — его unhide
    больше НЕ двигает вид #trace (раньше доводил вид до плашки в скролл-потоке)."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_thinking_indicator(state="thinking")   # смонтирован в #indicator-bar
        scr._hide_thinking_indicator()                    # .hidden (height 0)
        await pilot.pause()
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        trace.scroll_end = lambda **kw: calls.append(kw)
        scr._trace_is_at_bottom = lambda: True
        scr._show_thinking_indicator(state="validating")  # unhide — вне скролла трейса
        await pilot.pause()
        assert calls == []    # #trace не дёргается


@pytest.mark.asyncio
async def test_thinking_indicator_unhide_skipped_when_scrolled_up(tmp_path):
    """Разворот скрытого индикатора НЕ дёргает вид, если юзер отскроллен вверх."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_thinking_indicator(state="thinking")
        scr._hide_thinking_indicator()
        await pilot.pause()
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        trace.scroll_end = lambda **kw: calls.append(kw)
        scr._trace_is_at_bottom = lambda: False           # читает историю
        scr._show_thinking_indicator(state="validating")
        await pilot.pause()
        assert calls == []     # вид не сбит


@pytest.mark.asyncio
async def test_thinking_indicator_reshow_visible_does_not_scroll(tmp_path):
    """Повторный показ уже видимого индикатора (обычный тик «думаю») не скроллит —
    высота не меняется, вид не дёргаем даже у низа."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_thinking_indicator(state="thinking")   # видим, не скрыт
        await pilot.pause()
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        trace.scroll_end = lambda **kw: calls.append(kw)
        scr._trace_is_at_bottom = lambda: True
        scr._show_thinking_indicator(state="thinking")   # уже видим → без скролла
        await pilot.pause()
        assert calls == []


@pytest.mark.asyncio
async def test_command_output_forces_scroll(tmp_path):
    """Команда, которая что-то пишет в трейс (неизвестная команда → ошибка),
    форсит скролл вниз, даже если юзер отскроллен вверх — это его действие."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        trace.scroll_end = lambda **kw: calls.append(kw)
        scr._stick_bottom = False                          # отскроллен вверх читать
        scr._handle_command("такой-команды-нет")           # пишет error-строку в трейс
        await pilot.pause(0.1)                             # скролл отложен (call_after_refresh)
        assert calls    # результат команды (force) форсит скролл вниз


@pytest.mark.asyncio
async def test_command_opening_screen_does_not_scroll(tmp_path):
    """Команда, которая открывает экран (/help) и НЕ пишет в трейс, не дёргает
    скролл — позиция чтения истории сохраняется."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        trace.scroll_end = lambda **kw: calls.append(kw)
        scr._stick_bottom = False                          # отскроллен вверх читать
        scr._trace_is_at_bottom = lambda: False
        scr._handle_command("help")                        # push_screen, трейс не растёт
        await pilot.pause()
        assert calls == []     # экран-команда не трогает скролл трейса


@pytest.mark.asyncio
async def test_user_send_force_scrolls_to_very_bottom(tmp_path):
    """Регресс (#trace height:auto): отправка юзера (force_scroll) должна доезжать
    до САМОГО низа, чтобы свежий запрос был виден. scroll_end сразу после mount()
    не достаёт (max_scroll_y ещё не учёл новый виджет) — нужен call_after_refresh."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        for i in range(40):                                # заполнить трейс (скроллится)
            scr._mount_trace_row(prefix="x ", content=f"строка {i}", kind_class="line-meta")
        await pilot.pause(0.4)
        trace = scr.query_one("#trace", VerticalScroll)
        assert trace.max_scroll_y > 0                      # трейс реально скроллится
        trace.scroll_to(y=0, animate=False)                # юзер отскроллил вверх
        await pilot.pause()
        scr._mount_user_reply("мой свежий запрос", state="pending")   # отправка → force
        await pilot.pause(0.4)
        assert trace.scroll_offset.y >= trace.max_scroll_y - 1        # (1) доехали до низа
        # старт прогона: индикатор появляется → _sync_trace_max_height меняет потолок
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause(0.5)                                        # poll → пересчёт потолка
        assert trace.scroll_offset.y >= trace.max_scroll_y - 1        # (2) остались у низа
