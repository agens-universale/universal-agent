"""Input-оверлей (дропдаун) при длинном трейсе: #trace ужимается под ЖЕЛАЕМУЮ
высоту оверлея (по его контенту, virtual_size), а оверлей показывается целиком —
не вжимается в остаток места.

Тонкость: оверлей — VerticalScroll; его region (зажатая высота) зависит от того,
сколько места оставил #trace, а #trace (через _sync) — от region оверлея → петля
(оверлей остаётся зажатым). Разрываем её, считая по virtual_size (стабильна).
На закрытии #trace растёт обратно."""
import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "running",
            "updated_at": "2026-06-15T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _app(tmp_path):
    from gc_tui.app import TuiApp
    return TuiApp(threads_dir=str(tmp_path), system="cli",
                  config={"model": "M", "backend_api_mode": "chat_completions"})


async def _tall(pilot, tid):
    scr = pilot.app.screen
    scr.attach_active_task(tid)
    big = "\n".join(f"строка {i}" for i in range(80))   # трейс на весь экран
    scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                    "content": big}}, live=True)
    await pilot.pause(0.4)
    return scr


@pytest.mark.asyncio
async def test_palette_not_squished_when_trace_tall(tmp_path):
    pytest.importorskip("textual")
    tid = "ov1"; _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        full_max = scr._trace_max_height
        scr._open_palette(prefix="")               # ~13 команд + хинт
        await pilot.pause(0.4)
        palette = scr._palette
        footer = pilot.app.query_one("#footer")
        assert palette is not None
        assert footer.region.bottom <= 24          # футер на экране (не выдавлен)
        assert palette.region.height >= 10         # палитра НЕ зажата (показывает контент)
        assert scr._trace_max_height < full_max    # #trace ужался под палитру


@pytest.mark.asyncio
async def test_trace_regrows_after_palette_close(tmp_path):
    pytest.importorskip("textual")
    tid = "ov2"; _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        full_max = scr._trace_max_height
        scr._open_palette(prefix="")
        await pilot.pause(0.4)
        assert scr._trace_max_height < full_max    # ужался
        scr._close_palette()
        await pilot.pause(0.4)
        assert scr._trace_max_height == full_max    # вырос обратно на весь экран


@pytest.mark.asyncio
async def test_overlay_capped_at_half_screen(tmp_path):
    """Длинный список не занимает больше половины экрана — дальше его скролл.
    На 28-строчном экране палитра из 13 команд = 16 строк > половины (14) → режется."""
    pytest.importorskip("textual")
    tid = "ov4"; _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 28)) as pilot:
        scr = await _tall(pilot, tid)
        scr._open_palette(prefix="")
        await pilot.pause(0.4)
        palette = scr._palette
        assert palette.region.height == 28 // 2   # ровно половина (контент 16 > 14 → cap)


@pytest.mark.asyncio
async def test_trace_regrows_when_palette_filtered_down(tmp_path):
    """Фильтр сужает палитру → #trace растёт обратно. desired считаем по len(children)
    (обновляется сразу), а не по virtual_size (залипает после rebuild)."""
    pytest.importorskip("textual")
    tid = "ov3"; _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 40)) as pilot:
        scr = await _tall(pilot, tid)
        scr._open_palette(prefix="")                # ~13 команд
        await pilot.pause(0.4)
        full_palette = scr._trace_max_height        # ужат под полную палитру
        scr._palette.set_filter("co")               # → 2 команды (compact, config)
        await pilot.pause(0.4)                       # poll пересчитает desired
        assert scr._trace_max_height > full_palette  # палитра сузилась → #trace вырос


@pytest.mark.asyncio
async def test_palette_border_fits_on_screen_with_toggle_footer(tmp_path):
    """Геометрический баг: у CommandPalette дети overlay = N строк + ОДИН докнутый
    `.palette-footer`-контейнер (внутри — разделитель + toggle-строки + hint, т.е.
    3-5 строк, не 1). `_sync_trace_max_height` считал desired = len(children)+2,
    недосчитывая высоту подвала → #trace получал лишние строки, выдавливая нижнюю
    рамку палитры за экран (полный список команд, трейс — почти весь экран)."""
    pytest.importorskip("textual")
    tid = "ov5"; _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 60)) as pilot:
        scr = await _tall(pilot, tid)
        scr._open_palette(prefix="")                 # полный список + toggle-подвал
        await pilot.pause(0.4)
        palette = scr._palette
        footer = pilot.app.query_one("#footer")
        assert palette is not None
        assert palette.region.bottom <= 60             # нижняя рамка палитры на экране
        assert footer.region.bottom <= 60               # футер не выдавлен под экран


@pytest.mark.asyncio
async def test_palette_border_fits_on_screen_filtered_to_toggle_only(tmp_path):
    """Тот же геометрический баг, но фильтр отсекает все обычные команды (верхняя
    зона пуста, виден только toggle-подвал) — «/g» матчит /gate в подвале."""
    pytest.importorskip("textual")
    tid = "ov6"; _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        scr._open_palette(prefix="g")                 # только /gate (toggle) матчит
        await pilot.pause(0.4)
        palette = scr._palette
        footer = pilot.app.query_one("#footer")
        assert palette is not None
        assert palette.region.bottom <= 24
        assert footer.region.bottom <= 24
