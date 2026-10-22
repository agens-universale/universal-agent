"""Липкая реплика сверху и плашка «в конец» над лентой (спека P3 §2.6).

Накладки появляются только «не внизу» (`not _stick_bottom`), высоты ленты не
занимают, фокус не берут и выделение ленты не перекрывают."""

import json
from pathlib import Path

import pytest
from gc_testkit.tui_support import tui_settings

from gc_tui.palette import (
    ACCENT,
    DIM,
    FOCUS_BG,
    INK,
    MUTED,
    QUEUE_BG,
    USER_BAND_BG,
)

LONG = "подними таймаут клиента и проверь тесты, " * 6


def _write_task(tmp_path, tid):
    meta = {
        "thread_id": tid,
        "status": "running",
        "updated_at": "2026-06-15T12:00:00+00:00",
        "messages_count": 0,
    }
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


def _agent(scr, mid, n):
    big = "\n".join(f"строка {mid} {i}" for i in range(n))
    scr._render_record(
        {"payload": {"id": mid, "role": "assistant", "content": big}},
        live=True,
    )


async def _tall(pilot, tid):
    """Лента: реплика «первая», 40 строк, длинная реплика, 40 строк."""
    scr = pilot.app.screen
    scr.attach_active_task(tid)
    scr._mount_user_reply("первая")
    _agent(scr, "a1", 40)
    scr._mount_user_reply(LONG)
    _agent(scr, "a2", 40)
    await pilot.pause(0.4)
    return scr


def _trace(scr):
    from textual.containers import VerticalScroll

    return scr.query_one("#trace", VerticalScroll)


def _overlays(scr):
    from gc_tui.widgets.trace_overlays import ScrollEndPill, StickyReply

    return scr.query_one(StickyReply), scr.query_one(ScrollEndPill)


def _replies(scr):
    from gc_tui.widgets.user_reply import UserReplyContainer

    return list(_trace(scr).query(UserReplyContainer))


async def _scroll(pilot, scr, y):
    """Прокрутка «рукой»: позднее прижатие опроса её не перебивает."""
    _trace(scr).scroll_to(y=y, animate=False)
    await pilot.pause()
    await pilot.pause()


def _line(widget):
    return "".join(s.text for s in widget.render_line(0)).rstrip()


@pytest.mark.asyncio
async def test_at_bottom_no_overlays(tmp_path):
    _write_task(tmp_path, "o1")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o1")
        sticky, pill = _overlays(scr)
        assert scr._stick_bottom is True
        assert sticky.display is False
        assert pill.display is False


@pytest.mark.asyncio
async def test_pill_label_follows_input(tmp_path):
    from gc_tui.widgets.trace_overlays import PILL_CLICK, PILL_ENTER

    _write_task(tmp_path, "o2")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o2")
        _, pill = _overlays(scr)
        inp = scr.query_one("#cmdinput")
        inp.focus()
        await _scroll(pilot, scr, 10)
        assert scr._stick_bottom is False
        assert pill.display is True
        assert pill.label == PILL_ENTER
        inp.insert("и добавь тест")
        await pilot.pause()
        assert pill.label == PILL_CLICK
        inp.text = ""
        await pilot.pause()
        assert pill.label == PILL_ENTER
        _trace(scr).focus()
        await pilot.pause()
        assert pill.label == PILL_CLICK


def _assert_pill_clear_of_indicator(scr, pill):
    """Плашка видна на экране и не задевает ни строку индикатора, ни
    строки причины под ним. Центр — по ширине подложки реплики."""
    box = _trace(scr).scrollable_content_region
    region = pill.region
    assert pill.display is True
    assert region.height == 1
    assert 0 <= region.y < scr.size.height
    assert not region.overlaps(scr._thinking.region)
    left = region.x - box.x
    right = box.right - region.right
    assert abs(left - right) <= 1
    assert _line(pill).strip() == pill.label


class _Running:
    """Идёт прогон открытого треда: индикатор хода держится."""

    _thread_kind = "run"

    def __init__(self, tid):
        self._last_target = (tid, "cli")

    def is_running(self):
        return True

    def stop(self):
        pass


async def _relayout(pilot, scr):
    """Хром сменился: после раскладки накладки переставляет опрос — тот же
    вызов, что и в его тике."""
    await pilot.pause()
    scr._refresh_trace_overlays()
    await pilot.pause()


async def _pill_with_indicator(pilot, tid):
    scr = await _tall(pilot, tid)
    pilot.app.active_runner = _Running(tid)
    scr._show_thinking_indicator()
    await _scroll(pilot, scr, 10)
    return scr, _overlays(scr)[1]


@pytest.mark.asyncio
async def test_pill_clear_of_indicator_without_banner(tmp_path):
    _write_task(tmp_path, "o3")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr, pill = await _pill_with_indicator(pilot, "o3")
        _assert_pill_clear_of_indicator(scr, pill)


@pytest.mark.asyncio
async def test_pill_clear_of_error_reason_rows(tmp_path):
    _write_task(tmp_path, "o3e")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr, pill = await _pill_with_indicator(pilot, "o3e")
        scr._thinking.set_error("RuntimeError: " + "сбой " * 60)
        scr._thinking.set_state("failed")
        scr._sync_indicator_reserve()
        await _relayout(pilot, scr)
        assert scr._thinking.error_rows == 3
        _assert_pill_clear_of_indicator(scr, pill)


@pytest.mark.asyncio
async def test_pill_clear_of_indicator_with_confirm_banner(tmp_path):
    _write_task(tmp_path, "o3c")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr, pill = await _pill_with_indicator(pilot, "o3c")
        scr.show_confirm_banner("Действие", "Разрешить?")
        await _relayout(pilot, scr)
        _assert_pill_clear_of_indicator(scr, pill)


@pytest.mark.asyncio
async def test_pill_clear_of_indicator_with_plan_banner(tmp_path):
    _write_task(tmp_path, "o3p")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr, pill = await _pill_with_indicator(pilot, "o3p")
        scr._show_plan_banner()
        await _relayout(pilot, scr)
        _assert_pill_clear_of_indicator(scr, pill)


@pytest.mark.asyncio
async def test_pill_clear_of_indicator_with_ask_human_banner(tmp_path):
    _write_task(tmp_path, "o3a")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr, pill = await _pill_with_indicator(pilot, "o3a")
        scr._show_child_ask_human(parent_call_id="p1", prose="Какой путь?")
        await _relayout(pilot, scr)
        banner = scr._ask_human_banner
        assert banner is not None
        _assert_pill_clear_of_indicator(scr, pill)
        assert not pill.region.overlaps(banner.region)


@pytest.mark.asyncio
async def test_sticky_shows_nearest_reply_above_top(tmp_path):
    _write_task(tmp_path, "o4")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o4")
        sticky, _ = _overlays(scr)
        first, second = _replies(scr)
        await _scroll(pilot, scr, second.virtual_region.y + 2)
        assert sticky.display is True
        assert sticky.reply is second
        # Решение 27: ширина подложки реплики — без колонки скроллбара.
        box = _trace(scr).scrollable_content_region
        assert sticky.region == box._replace(height=1)
        line = _line(sticky)
        assert line.startswith("› подними таймаут клиента")
        assert line.endswith("…")
        assert len(line) == box.width
        await _scroll(pilot, scr, second.virtual_region.y)
        assert sticky.reply is first
        assert _line(sticky) == "› первая"


@pytest.mark.asyncio
async def test_sticky_colours(tmp_path):
    _write_task(tmp_path, "o5")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o5")
        sticky, _ = _overlays(scr)
        first, _ = _replies(scr)
        await _scroll(pilot, scr, first.virtual_region.y + 1)
        segments = [s for s in sticky.render_line(0) if s.text.strip()]
        marker, text = segments[0], segments[1]
        assert marker.text.strip() == "›"
        assert marker.style.color.name.lower() == MUTED.lower()
        assert text.style.color.name.lower() == DIM.lower()
        assert text.style.bgcolor.name.lower() == QUEUE_BG.lower()
        assert QUEUE_BG.lower() != USER_BAND_BG.lower()


@pytest.mark.asyncio
async def test_sticky_skill_full_id_in_reply_colour(tmp_path):
    from textual.containers import VerticalScroll

    from gc_tui.widgets.user_reply import UserReplyContainer

    _write_task(tmp_path, "o5s")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o5s")
        scr.query_one("#trace", VerticalScroll).mount(
            UserReplyContainer("почини тест", skill="p:coder"), before=0
        )
        await pilot.pause()
        sticky, _ = _overlays(scr)
        reply = next(
            r for r in _replies(scr) if r.plain_text.startswith("/p:coder")
        )
        await _scroll(pilot, scr, reply.virtual_region.y + 1)
        assert sticky.reply is reply
        segments = [s for s in sticky.render_line(0) if s.text.strip()]
        label = next(s for s in segments if "/p:coder" in s.text)
        assert label.style.color.name.lower() == DIM.lower()
        assert "/p:coder почини тест" in label.text


@pytest.mark.asyncio
async def test_sticky_hover_lightens_background(tmp_path):
    """Ховер липкой реплики — `OVERLAY_HOVER_BG` на всю строку (не
    `USER_BAND_BG`), цвет текста прежний."""
    from gc_tui import palette

    _write_task(tmp_path, "o5h")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o5h")
        sticky, _ = _overlays(scr)
        first, _ = _replies(scr)
        await _scroll(pilot, scr, first.virtual_region.y + 1)
        assert sticky.rich_style.bgcolor.name.lower() == QUEUE_BG.lower()
        await pilot.hover(sticky)
        await pilot.pause()
        hover_bg = palette.OVERLAY_HOVER_BG.lower()
        assert sticky.rich_style.bgcolor.name.lower() == hover_bg
        assert hover_bg != USER_BAND_BG.lower()
        segments = [s for s in sticky.render_line(0) if s.text.strip()]
        assert segments[1].style.color.name.lower() == DIM.lower()
        assert segments[1].style.bgcolor.name.lower() == hover_bg


@pytest.mark.asyncio
async def test_no_sticky_without_reply_above(tmp_path):
    _write_task(tmp_path, "o6")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o6")
        sticky, pill = _overlays(scr)
        await _scroll(pilot, scr, 0)
        assert scr._stick_bottom is False
        assert pill.display is True
        assert sticky.display is False


@pytest.mark.asyncio
async def test_click_sticky_brings_reply_to_top(tmp_path):
    _write_task(tmp_path, "o7")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o7")
        sticky, _ = _overlays(scr)
        _, second = _replies(scr)
        inp = scr.query_one("#cmdinput")
        inp.focus()
        await _scroll(pilot, scr, second.virtual_region.y + 5)
        await pilot.click(sticky)
        await pilot.pause()
        await pilot.pause()
        # Верх реплики (с её отступом) — первая строка видимой области.
        top = _trace(scr).content_region.y + second.styles.margin.top
        assert second.region.y == top
        assert inp.has_focus  # накладка фокус не берёт


@pytest.mark.asyncio
async def test_click_pill_goes_to_bottom(tmp_path):
    _write_task(tmp_path, "o8")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o8")
        _, pill = _overlays(scr)
        inp = scr.query_one("#cmdinput")
        inp.focus()
        await _scroll(pilot, scr, 10)
        await pilot.click(pill)
        await pilot.pause()
        await pilot.pause()
        trace = _trace(scr)
        assert trace.scroll_offset.y == trace.max_scroll_y
        assert scr._stick_bottom is True
        assert pill.display is False
        assert inp.has_focus


@pytest.mark.asyncio
async def test_overlays_take_no_trace_height(tmp_path):
    _write_task(tmp_path, "o9")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o9")
        sticky, pill = _overlays(scr)
        trace = _trace(scr)
        height = trace.content_region.height
        await _scroll(pilot, scr, 50)
        assert sticky.display is True and pill.display is True
        assert trace.content_region.height == height


@pytest.mark.asyncio
async def test_pill_hover_lightens_background(tmp_path):
    """Ховер плашки — светлее фон, цвет текста прежний (исключение из
    `.trace-action:hover`, решение владельца)."""
    from gc_tui import palette

    _write_task(tmp_path, "o10")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o10")
        _, pill = _overlays(scr)
        await _scroll(pilot, scr, 10)
        assert pill.rich_style.color.name.lower() == INK.lower()
        assert pill.rich_style.bgcolor.name.lower() == FOCUS_BG.lower()
        await pilot.hover(pill)
        await pilot.pause()
        assert pill.rich_style.color.name.lower() == INK.lower()
        hover_bg = palette.OVERLAY_HOVER_BG.lower()
        assert pill.rich_style.bgcolor.name.lower() == hover_bg
        assert hover_bg != FOCUS_BG.lower()


def test_pill_hover_bg_is_lighter_and_derived():
    from gc_tui import palette

    def lum(c):
        return sum(palette._rgb(c))

    assert lum(palette.OVERLAY_HOVER_BG) > lum(palette.FOCUS_BG)
    assert (
        palette.css_variables()["overlay-hover-bg"] == palette.OVERLAY_HOVER_BG
    )


@pytest.mark.asyncio
async def test_drag_beside_pill_selects_trace_text(tmp_path):
    """Протяжка с последней строки ленты рядом с плашкой выделяет ленту."""
    _write_task(tmp_path, "o11")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o11")
        _, pill = _overlays(scr)
        await _scroll(pilot, scr, 10)
        box = _trace(scr).content_region
        y = box.bottom - 1
        await pilot.mouse_down(offset=(box.x + 1, y))
        await pilot.hover(offset=(box.x + 8, y - 3))
        await pilot.mouse_up(offset=(box.x + 8, y - 3))
        await pilot.pause()
        assert pill.region.x > box.x + 1
        assert "строка" in (scr.get_selected_text() or "")


@pytest.mark.asyncio
async def test_drag_across_pill_row_skips_overlays(tmp_path):
    """Протяжка через строку плашки и липкой реплики: в копию идёт лента,
    подписи накладок — нет."""
    _write_task(tmp_path, "o12")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o12")
        first, _ = _replies(scr)
        await _scroll(pilot, scr, first.virtual_region.y + 1)
        box = _trace(scr).content_region
        # Первая строка под липкой репликой: выделение — со второй.
        await pilot.mouse_down(offset=(box.x + 1, box.y + 1))
        await pilot.hover(offset=(box.right - 1, box.bottom - 1))
        await pilot.mouse_up(offset=(box.right - 1, box.bottom - 1))
        await pilot.pause()
        selected = scr.get_selected_text() or ""
        assert "строка" in selected
        assert "в конец" not in selected


def _blocks_around(scr, reply):
    """Самые высокие дети ленты над репликой (до предыдущей) и под ней."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    children = list(_trace(scr).children)
    at = children.index(reply)
    before = []
    for child in reversed(children[:at]):
        if isinstance(child, UserReplyContainer):
            break
        before.append(child)

    def tallest(widgets):
        return max(widgets, key=lambda w: w.region.height)

    return tallest(before), tallest(children[at + 1 :])


@pytest.mark.asyncio
async def test_sticky_follows_moved_reply(tmp_path):
    """Реплика сдвинулась без смены числа детей и высоты ленты — липкая
    строка считает по живому верху, не по старому."""
    _write_task(tmp_path, "o13")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o13")
        sticky, _ = _overlays(scr)
        first, second = _replies(scr)
        old_top = second.virtual_region.y
        await _scroll(pilot, scr, old_top + 2)
        assert sticky.reply is second
        above, below = _blocks_around(scr, second)
        above.styles.height = above.region.height + 10
        below.styles.height = below.region.height - 10
        await pilot.pause()
        await pilot.pause()
        assert second.virtual_region.y == old_top + 10
        await _scroll(pilot, scr, old_top + 4)
        assert sticky.reply is first


@pytest.mark.asyncio
async def test_wheel_over_overlays_scrolls_trace(tmp_path):
    from textual import events

    _write_task(tmp_path, "o14")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o14")
        sticky, pill = _overlays(scr)
        trace = _trace(scr)
        await _scroll(pilot, scr, 40)
        await pilot._post_mouse_events([events.MouseScrollUp], pill)
        assert trace.scroll_offset.y < 40
        y = trace.scroll_offset.y
        await pilot._post_mouse_events([events.MouseScrollDown], sticky)
        assert trace.scroll_offset.y > y


@pytest.mark.asyncio
async def test_click_on_detached_reply_is_ignored(tmp_path):
    from gc_tui.messages import ScrollToReply

    _write_task(tmp_path, "o15")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o15")
        first, _ = _replies(scr)
        await _scroll(pilot, scr, 30)
        await first.remove()
        scr.on_scroll_to_reply(ScrollToReply(first))
        await pilot.pause()
        assert _trace(scr).scroll_offset.y == 30


@pytest.mark.asyncio
async def test_wheel_burst_over_overlay_keeps_every_step(tmp_path):
    """Три тика колеса подряд, без обновления экрана между ними, — три
    шага ленты, а не один."""
    from textual import events

    _write_task(tmp_path, "o16")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o16")
        _, pill = _overlays(scr)
        trace = _trace(scr)
        await _scroll(pilot, scr, 10)
        x, y = pill.region.offset
        for _ in range(3):
            pill.on_mouse_scroll_down(
                events.MouseScrollDown(
                    pill, 0, 0, 0, 0, 0, False, False, False, x, y
                )
            )
        await pilot.pause()
        step = pilot.app.scroll_sensitivity_y
        assert trace.scroll_offset.y == 10 + round(3 * step)


@pytest.mark.asyncio
async def test_replies_cache_sees_remove_and_mount_in_one_tick(tmp_path):
    """Реплику сняли и в том же такте смонтировали другую строку — число
    детей прежнее, но состав реплик другой."""
    _write_task(tmp_path, "o17")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o17")
        first, second = _replies(scr)
        trace = _trace(scr)
        assert scr._trace_replies(trace) == [first, second]
        await second.remove()
        _agent(scr, "a3", 3)
        assert scr._trace_replies(trace) == [first]


class _BusyRunner:
    """Идущий ход: очередь живёт, пока он не кончился."""

    def is_running(self) -> bool:
        return True

    def is_child_input_active(self) -> bool:
        return False


async def _tall_with_queue(pilot, tid):
    """Лента `_tall` и две реплики в очереди у низа идущего хода."""
    scr = await _tall(pilot, tid)
    pilot.app.active_runner = _BusyRunner()
    for i, text in enumerate(("в очереди 1", "в очереди 2")):
        scr._mount_user_reply(text, state="queued", client_id=f"c{i}")
    await pilot.pause(0.2)
    return scr


@pytest.mark.asyncio
async def test_sticky_and_pill_with_queue_below(tmp_path):
    """Очередь у низа — не «выше края»: липкая реплика — ближайшая выше
    видимой области, плашка «в конец» на месте."""
    _write_task(tmp_path, "o20")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall_with_queue(pilot, "o20")
        sticky, pill = _overlays(scr)
        _, second, *queued = _replies(scr)
        assert [r.state for r in queued] == ["queued", "queued"]
        await _scroll(pilot, scr, second.virtual_region.y + 2)
        assert sticky.reply is second
        assert pill.display is True


@pytest.mark.asyncio
async def test_selection_survives_queued_reply_mount(tmp_path):
    """Реплика встаёт в очередь посреди выделения мышью: выделенный
    текст ленты тот же."""
    _write_task(tmp_path, "o21")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o21")
        await _scroll(pilot, scr, 10)
        box = _trace(scr).content_region
        await pilot.mouse_down(offset=(box.x + 2, box.y + 2))
        await pilot.hover(offset=(box.x + 10, box.y + 6))
        await pilot.pause()
        before = scr.get_selected_text()
        assert before
        pilot.app.active_runner = _BusyRunner()
        scr._mount_user_reply("в очереди", state="queued", client_id="c1")
        await pilot.pause(0.2)
        assert scr.get_selected_text() == before
        await pilot.mouse_up(offset=(box.x + 10, box.y + 6))
        await pilot.pause()
        assert _replies(scr)[-1].state == "queued"


@pytest.mark.parametrize(
    ("n", "word"),
    [
        (1, "новое сообщение"),
        (2, "новых сообщения"),
        (4, "новых сообщения"),
        (5, "новых сообщений"),
        (11, "новых сообщений"),
        (12, "новых сообщений"),
        (14, "новых сообщений"),
        (20, "новых сообщений"),
        (21, "новое сообщение"),
        (22, "новых сообщения"),
        (111, "новых сообщений"),
    ],
)
def test_new_messages_plural(n, word):
    from gc_tui.widgets.trace_overlays import pill_label

    assert pill_label(n, enter=False) == f"↓ {n} {word} · клик"
    assert pill_label(n, enter=True) == f"↓ {n} {word} · клик или Enter"


def test_pill_label_without_new():
    from gc_tui.widgets.trace_overlays import (
        PILL_CLICK,
        PILL_ENTER,
        pill_label,
    )

    assert pill_label(0, enter=False) == PILL_CLICK
    assert pill_label(0, enter=True) == PILL_ENTER


@pytest.mark.asyncio
async def test_pill_counts_new_agent_replies(tmp_path):
    _write_task(tmp_path, "o20")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o20")
        _, pill = _overlays(scr)
        _trace(scr).focus()
        await _scroll(pilot, scr, 10)
        assert pill.label == "↓ в конец · клик"
        _agent(scr, "n1", 3)
        _agent(scr, "n2", 3)
        await pilot.pause()
        assert pill.label == "↓ 2 новых сообщения · клик"
        scr._render_record(
            {
                "payload": {
                    "id": "c1",
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "x1",
                            "type": "function",
                            "function": {
                                "name": "fs_read",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            },
            live=True,
        )
        await pilot.pause()
        assert pill.label == "↓ 2 новых сообщения · клик"
        _agent(scr, "n3", 3)
        await pilot.pause()
        assert pill.label == "↓ 3 новых сообщения · клик"


@pytest.mark.asyncio
async def test_bridge_echo_not_mounted_not_counted(tmp_path):
    from gc_tui.widgets.trace_row import TraceRow

    _write_task(tmp_path, "o21b")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o21b")
        _, pill = _overlays(scr)
        _trace(scr).focus()
        await _scroll(pilot, scr, 10)
        rows = len(scr.query(TraceRow))
        scr._render_record(
            {
                "payload": {
                    "id": "e1",
                    "role": "assistant",
                    "content": "(tool results received)",
                }
            },
            live=True,
        )
        await pilot.pause()
        assert len(scr.query(TraceRow)) == rows
        assert scr._new_replies == 0
        assert pill.label == "↓ в конец · клик"
        mention = "Ответ: фраза (tool results received) служебная."
        scr._render_record(
            {"payload": {"id": "e2", "role": "assistant", "content": mention}},
            live=True,
        )
        await pilot.pause()
        assert scr._new_replies == 1


def _finish(scr, mid, report):
    scr._render_record(
        {
            "payload": {
                "id": mid,
                "type": "function_call",
                "call_id": f"call-{mid}",
                "name": "turn_finish",
                "arguments": json.dumps({"task_report": report}),
            }
        },
        live=True,
    )


@pytest.mark.asyncio
async def test_pill_counts_only_visible_finish_attempt(tmp_path):
    """Отклонённые попытки turn_finish лента убирает: счётчик плашки
    равен числу видимых новых ответов — попытка, попытка, итог → 1."""
    _write_task(tmp_path, "o25")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o25")
        _, pill = _overlays(scr)
        _trace(scr).focus()
        await _scroll(pilot, scr, 10)
        _finish(scr, "f1", "первая попытка")
        _finish(scr, "f2", "вторая попытка")
        _finish(scr, "f3", "итог")
        await pilot.pause()
        assert scr._new_replies == 1
        assert pill.label == "↓ 1 новое сообщение · клик"


@pytest.mark.asyncio
async def test_pill_attempt_repeated_by_prose_counts_once(tmp_path):
    """Проза повторила отчёт попытки: попытка уходит, ответ остаётся —
    одно новое сообщение."""
    _write_task(tmp_path, "o26")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o26")
        _trace(scr).focus()
        await _scroll(pilot, scr, 10)
        _finish(scr, "f1", "отчёт")
        scr._render_record(
            {"payload": {"id": "p1", "role": "assistant", "content": "отчёт"}},
            live=True,
        )
        await pilot.pause()
        assert scr._new_replies == 1


@pytest.mark.asyncio
async def test_pill_counter_resets_at_bottom(tmp_path):
    _write_task(tmp_path, "o21")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o21")
        _, pill = _overlays(scr)
        _trace(scr).focus()
        await _scroll(pilot, scr, 10)
        _agent(scr, "n1", 3)
        await pilot.pause()
        assert pill.label == "↓ 1 новое сообщение · клик"
        scr._scroll_to_bottom()
        await pilot.pause()
        await pilot.pause()
        assert scr._stick_bottom is True
        await _scroll(pilot, scr, 10)
        assert pill.label == "↓ в конец · клик"


@pytest.mark.asyncio
async def test_pill_counter_resets_on_task_switch(tmp_path):
    _write_task(tmp_path, "o22")
    _write_task(tmp_path, "o23")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o22")
        await _scroll(pilot, scr, 10)
        _agent(scr, "n1", 3)
        await pilot.pause()
        assert scr._new_replies == 1
        scr.attach_active_task("o23")
        await pilot.pause(0.3)
        assert scr._new_replies == 0


@pytest.mark.asyncio
async def test_pill_label_stable_on_app_blur(tmp_path):
    from gc_tui.widgets.trace_overlays import PILL_ENTER

    _write_task(tmp_path, "o24")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o24")
        _, pill = _overlays(scr)
        scr.query_one("#cmdinput").focus()
        await _scroll(pilot, scr, 10)
        assert pill.label == PILL_ENTER
        pilot.app.app_focus = False
        await pilot.pause()
        assert pill.label == PILL_ENTER
        pilot.app.app_focus = True
        await pilot.pause()
        assert pill.label == PILL_ENTER


@pytest.mark.asyncio
async def test_quiet_reply_look_is_shared(tmp_path):
    """Реплика в очереди и липкая реплика — один «тихий» вид: подложка,
    текст, маркер, цвет текста и имя скилла совпадают посимвольно."""
    from rich.console import Console
    from textual.color import Color

    from gc_tui import palette
    from gc_tui.widgets.user_reply import UserReplyContainer

    _write_task(tmp_path, "o30")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o30")
        queued = UserReplyContainer(
            "почини тест", state="queued", skill="supercoder:brainstorming"
        )
        _trace(scr).mount(queued, before=0)
        await pilot.pause()
        sticky, _ = _overlays(scr)
        await _scroll(pilot, scr, queued.virtual_region.y + 1)
        assert sticky.reply is queued
        band = queued.query_one(".user-band")
        assert band.styles.background == sticky.styles.background
        assert sticky.styles.background == Color.parse(palette.QUEUE_BG)
        queued_text = queued._band.render()
        sticky_text = sticky.render()
        expected = "› /supercoder:brainstorming почини тест"
        assert queued_text.plain == expected
        assert sticky_text.plain == expected

        console = Console()

        def colour(text, at):
            color = text.get_style_at_offset(console, at).color
            return color.name.lower() if color else None

        looks = [
            (colour(queued_text, at), colour(sticky_text, at))
            for at in range(len(expected))
        ]
        assert all(q == st for q, st in looks)
        mark, ink = palette.QUIET_REPLY_MARK, palette.QUIET_REPLY_INK
        assert colour(sticky_text, 0) == mark.lower()
        assert colour(sticky_text, 3) == ink.lower()  # имя скилла
        assert colour(sticky_text, len(expected) - 1) == ink.lower()


@pytest.mark.asyncio
async def test_sticky_matches_reply_band_with_scrollbar(tmp_path):
    """Решение 27: липкая реплика — той же ширины и с того же x, что
    подложка реплики в ленте; на колонку скроллбара не заходит."""
    _write_task(tmp_path, "o27")
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _tall(pilot, "o27")
        sticky, _ = _overlays(scr)
        _, second = _replies(scr)
        await _scroll(pilot, scr, second.virtual_region.y + 1)
        assert _trace(scr).show_vertical_scrollbar
        band = second.children[0].region
        assert sticky.display is True
        assert sticky.region.x == band.x
        assert sticky.region.right == band.right
