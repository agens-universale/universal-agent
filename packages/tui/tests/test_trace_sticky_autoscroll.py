"""Sticky-bottom автоскролл трейса (#trace height:auto).

Ядро: липкость к низу различает РОСТ КОНТЕНТА (offset не двигается → остаёмся
липкими, доскролливаем) и РУЧНОЙ УХОД ВВЕРХ (offset стал меньше места, куда мы
сами прижали → отпускаем). Сравнение идёт с `_last_auto_y` (наша последняя
позиция прижатия), а не с `max_scroll_y` (он растёт от контента)."""

import json
from pathlib import Path

import pytest
from gc_core.threads.file_layout import history_line
from gc_testkit.tui_support import tui_settings


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


async def _tall(pilot, tid):
    scr = pilot.app.screen
    scr.attach_active_task(tid)
    big = "\n".join(
        f"строка {i}" for i in range(60)
    )  # трейс упрётся в потолок
    scr._render_record(
        {"payload": {"id": "a1", "role": "assistant", "content": big}},
        live=True,
    )
    await pilot.pause(0.4)
    return scr


@pytest.mark.asyncio
async def test_content_growth_keeps_sticky(tmp_path):
    """Рост контента ниже (offset остаётся на месте прижатия, хотя низ уехал вниз)
    НЕ снимает липкость — сравнение идёт с _last_auto_y, а не с max_scroll_y.

    Эмулируем пост-рост состояние позицией: offset == _last_auto_y, но НИЖЕ offset
    ещё есть контент (offset < max_scroll_y) — будто низ «дорос» после прижатия."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    tid = "st1"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        trace = scr.query_one("#trace", VerticalScroll)
        assert trace.max_scroll_y > 4  # контент реально скроллится
        scr._stick_bottom = False  # на время setup poll не скроллит
        p = trace.max_scroll_y - 3  # ниже offset остаётся контент
        trace.scroll_to(y=p, animate=False)
        await pilot.pause()
        assert trace.scroll_offset.y == p
        assert p < trace.max_scroll_y  # низ «ниже» offset (контент дорос)
        scr._last_auto_y = p  # сюда мы в прошлый раз прижали
        scr._stick_bottom = True
        scr._update_stick_state()  # sync, без await → poll не вмешается
        assert scr._stick_bottom is True  # off == _last_auto_y → рост, не уход


@pytest.mark.asyncio
async def test_user_scroll_up_disengages(tmp_path):
    """Ручной уход вверх (offset < места прижатия) снимает липкость."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    tid = "st2"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        trace = scr.query_one("#trace", VerticalScroll)
        trace.scroll_end(animate=False)
        await pilot.pause()
        scr._last_auto_y = trace.max_scroll_y
        assert scr._last_auto_y > 0
        trace.scroll_to(y=0, animate=False)  # юзер ушёл вверх
        await pilot.pause()
        scr._update_stick_state()
        assert scr._stick_bottom is False


@pytest.mark.asyncio
async def test_return_to_bottom_reengages(tmp_path):
    """Возврат юзера к низу возвращает липкость."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    tid = "st3"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        trace = scr.query_one("#trace", VerticalScroll)
        scr._stick_bottom = False  # юзер был вверху
        trace.scroll_end(animate=False)  # вернулся к низу
        await pilot.pause()
        scr._update_stick_state()
        assert scr._stick_bottom is True


@pytest.mark.asyncio
async def test_send_force_reengages(tmp_path):
    """Отправка своего сообщения (force) возвращает липкость, даже если отскроллен."""
    pytest.importorskip("textual")
    tid = "st4"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        scr._stick_bottom = False  # юзер отскроллен вверх
        scr._mount_user_reply("новый запрос", state="pending")  # force
        assert scr._stick_bottom is True  # отправка вернула липкость


async def _setup_scrolled(pilot, tid):
    from textual.containers import VerticalScroll

    scr = await _tall(pilot, tid)
    trace = scr.query_one("#trace", VerticalScroll)
    trace.scroll_end(animate=False)
    await pilot.pause()
    scr._last_auto_y = trace.max_scroll_y
    scr._stick_bottom = True
    return scr, trace


@pytest.mark.asyncio
async def test_scroll_event_disengages_without_poll(tmp_path):
    """Уход вверх снимает липкость по событию, без _poll_tick."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "ev1")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "ev1")
        scr._poll_tick = lambda: None
        trace.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert scr._stick_bottom is False


@pytest.mark.asyncio
async def test_scroll_event_reengages_at_bottom(tmp_path):
    """Возврат к низу возвращает липкость по событию."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "ev2")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "ev2")
        scr._poll_tick = lambda: None
        trace.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert scr._stick_bottom is False
        trace.scroll_end(animate=False)
        await pilot.pause()
        assert scr._stick_bottom is True


@pytest.mark.asyncio
async def test_own_scroll_end_keeps_sticky(tmp_path):
    """Наш scroll_end при росте ленты не снимает липкость."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "ev3")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "ev3")
        scr._poll_tick = lambda: None
        scr._render_record(
            {
                "payload": {
                    "id": "a2",
                    "role": "assistant",
                    "content": "\n".join(f"ещё {i}" for i in range(40)),
                }
            },
            live=True,
        )
        scr._scroll_trace_end()
        await pilot.pause(0.4)
        assert scr._stick_bottom is True
        assert trace.scroll_offset.y == trace.max_scroll_y


@pytest.mark.asyncio
async def test_user_scroll_beats_pending_scroll_end(tmp_path):
    """Прижатие заказано, пока лента липла; человек ушёл вверх до его
    исполнения — позднее прижатие ленту не возвращает."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "ev4")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "ev4")
        scr._poll_tick = lambda: None
        scr._scroll_trace_end()
        # Колесо прокручивает сразу, до исполнения заказанного прижатия.
        trace.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause(0.2)
        assert scr._stick_bottom is False
        assert trace.scroll_offset.y == 0


@pytest.mark.asyncio
async def test_bottom_moving_up_keeps_sticky(tmp_path):
    """Лента выросла в высоту (снят баннер) — низ поднялся, offset прижат
    к новому низу. Это не уход человека вверх: липкость остаётся."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "ev5")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "ev5")
        scr._poll_tick = lambda: None
        trace.scroll_end(animate=False, immediate=True)
        scr._last_auto_y = trace.max_scroll_y + 8  # прижали до роста
        scr._stick_bottom = True
        scr._update_stick_state()
        assert scr._stick_bottom is True


async def _enter(scr, text=""):
    from gc_tui.screens.main import _SEL_CMDINPUT
    from gc_tui.widgets.command_input import CommandTextArea

    inp = scr.query_one(_SEL_CMDINPUT, CommandTextArea)
    inp.text = text
    await scr.on_command_text_area_submitted(
        CommandTextArea.Submitted(value=text)
    )
    return inp


@pytest.mark.asyncio
async def test_enter_in_empty_input_scrolls_to_bottom(tmp_path):
    """Enter в пустом поле не внизу: к низу, сообщение не уходит."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "en1")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "en1")
        trace.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert scr._stick_bottom is False
        sent = []
        scr._mount_user_reply = lambda *a, **k: sent.append(a)
        await _enter(scr)
        await pilot.pause(0.3)
        assert scr._stick_bottom is True
        assert trace.scroll_offset.y == trace.max_scroll_y
        assert sent == []


@pytest.mark.asyncio
async def test_enter_in_empty_input_at_bottom_does_nothing(tmp_path):
    pytest.importorskip("textual")
    _write_task(tmp_path, "en2")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "en2")
        calls = []
        scr._scroll_to_bottom = lambda: calls.append(1)
        await _enter(scr)
        assert calls == []


@pytest.mark.asyncio
async def test_enter_with_text_sends_not_scrolls(tmp_path):
    pytest.importorskip("textual")
    _write_task(tmp_path, "en3")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "en3")
        scr._stick_bottom = False
        calls = []
        scr._scroll_to_bottom = lambda: calls.append(1)
        await _enter(scr, "привет")
        assert calls == []
        assert scr._stick_bottom is True  # отправка = re-engage, как раньше


@pytest.mark.asyncio
async def test_scroll_event_during_selection_does_not_stick(tmp_path):
    """Выделение (F2): событие прокрутки не трогает липкость."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "en4")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "en4")
        scr._poll_tick = lambda: None
        scr._selecting = True
        trace.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert scr._stick_bottom is True
        scr._selecting = False


@pytest.mark.asyncio
async def test_page_up_disengages_without_poll(tmp_path):
    """PageUp по ленте снимает липкость событием прокрутки, без опроса."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "pg1")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "pg1")
        scr._poll_tick = lambda: None
        trace.focus()
        await pilot.press("pageup")
        await pilot.pause()
        assert trace.scroll_offset.y < trace.max_scroll_y
        assert scr._stick_bottom is False


@pytest.mark.asyncio
async def test_wheel_up_disengages_without_poll(tmp_path):
    """Колесо вверх над лентой снимает липкость событием прокрутки."""
    pytest.importorskip("textual")
    from textual import events

    _write_task(tmp_path, "wh1")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "wh1")
        scr._poll_tick = lambda: None
        for _ in range(3):
            trace.post_message(
                events.MouseScrollUp(
                    trace, 1, 1, 0, -1, 0, False, False, False
                )
            )
        await pilot.pause()
        assert trace.scroll_offset.y < trace.max_scroll_y
        assert scr._stick_bottom is False


@pytest.mark.asyncio
async def test_thread_switch_with_live_watch_sticks_to_new_bottom(tmp_path):
    """Переключение треда, когда старый отскроллен вверх: наблюдатель
    `scroll_y` жив, сброс ленты двигает offset — новый тред всё равно
    липнет к своему низу и остаётся внизу."""
    pytest.importorskip("textual")
    _write_task(tmp_path, "sw1")
    _write_task(tmp_path, "sw2")
    big = "\n".join(f"другая {i}" for i in range(80))
    payload = {"id": "b1", "role": "assistant", "content": big}
    (Path(tmp_path) / "data_cli_sw2.jsonl").write_text(
        history_line("cli", "sw2", 1, "2026-06-15T12:00:00+00:00", payload),
        encoding="utf-8",
    )
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr, trace = await _setup_scrolled(pilot, "sw1")
        trace.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert scr._stick_bottom is False
        scr.attach_active_task("sw2", is_resume=True)
        await pilot.pause(0.4)
        assert "b1" in scr._rendered_ids  # история треда прочитана
        assert trace.max_scroll_y > 0
        assert scr._stick_bottom is True
        assert trace.scroll_offset.y == trace.max_scroll_y
