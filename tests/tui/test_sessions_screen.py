"""SessionsScreen: карточки (2 строки), пагинация, фильтр веток, поиск, rename, preview."""
import pytest

import json
from pathlib import Path

from thread_store import append_new_thread_messages
from tui.services.stores.file_store import FileTaskStore


def _seed(threads_dir, tid, prompt, *, branch=None, title=None,
          updated_at="2026-05-30T10:00:00+00:00", msgs=1):
    """Сессия = data-файл (реальный writer) + meta с messages_count/updated_at
    (как пишет thread_store), плюс tui.branch/title при необходимости."""
    append_new_thread_messages(str(threads_dir), "cli", tid,
                               [{"role": "user", "content": prompt, "id": f"m_{tid}"}], set())
    meta = {"thread_id": tid, "system": "cli", "status": "stopped",
            "updated_at": updated_at, "messages_count": msgs}
    tui = {}
    if branch is not None:
        tui["branch"] = branch
    if title is not None:
        tui["title"] = title
    if tui:
        meta["tui"] = tui
    (Path(threads_dir) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _rows_text(pilot):
    return "\n".join(str(s.render()) for s in pilot.app.screen.query("#sessions-list Static"))


@pytest.mark.asyncio
async def test_long_prompt_card_stays_two_lines(tmp_path):
    """Длинный (в т.ч. skill-) промпт: карточка остаётся РОВНО 2 строки —
    заголовок усечён `…`, а не растёт под весь промпт. Регресс: при height:auto
    без text-wrap:nowrap длинная строка переносилась и занимала десятки строк."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    long_prompt = "Используй skill `qa-req-validation`. " + ("очень длинный текст " * 80)
    _seed(tmp_path, "aaaaaaaa", long_prompt)
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        card = list(pilot.app.screen.query_one(
            "#sessions-list", VerticalScroll).query(".card"))[0]
        assert card.size.height == 2


@pytest.mark.asyncio
async def test_card_shows_title_branch_msgs(tmp_path):
    pytest.importorskip("textual")
    _seed(tmp_path, "aaaaaaaa", "первый вопрос", branch="feat/x")
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "первый вопрос" in text
        assert "feat/x" in text
        assert "1 сообщ." in text
        assert "aaaaaaaa" in text  # id8


@pytest.mark.asyncio
async def test_lists_more_than_200_sessions(tmp_path):
    """Кап отображения поднят выше 200: при 201 сессии экран видит реальное число."""
    pytest.importorskip("textual")
    for i in range(201):
        tid = f"{i:032d}"
        (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
            json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                        "updated_at": f"2026-05-{(i % 28) + 1:02d}T10:00:00+00:00",
                        "messages_count": 1}), encoding="utf-8")
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        assert len(pilot.app.screen._all_tasks()) == 201


@pytest.mark.asyncio
async def test_custom_title_overrides_prompt(tmp_path):
    pytest.importorskip("textual")
    _seed(tmp_path, "bbbbbbbb", "длинный промпт", title="Мой заголовок")
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "Мой заголовок" in text


@pytest.mark.asyncio
async def test_branch_filter_ctrl_b(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _seed(tmp_path, "onbranch0", "на ветке", branch="feature-x")
    _seed(tmp_path, "offbranch", "другая ветка", branch="master")
    import tui.screens.sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "current_branch", lambda cwd: "feature-x")
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        assert "другая ветка" in _rows_text(pilot)  # дефолт: все
        await pilot.press("ctrl+b")
        await pilot.pause()
        text = _rows_text(pilot)
        assert "на ветке" in text
        assert "другая ветка" not in text


@pytest.mark.asyncio
async def test_search_filters(tmp_path):
    pytest.importorskip("textual")
    _seed(tmp_path, "find1111", "уникальный маркер альфа")
    _seed(tmp_path, "find2222", "совсем другое")
    from tui.screens.sessions import SessionsScreen
    from textual.widgets import Input
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        pilot.app.screen.query_one("#sessions-filter", Input).value = "альфа"
        await pilot.pause()
        text = _rows_text(pilot)
        assert "альфа" in text
        assert "совсем другое" not in text


@pytest.mark.asyncio
async def test_rename_saves_title(tmp_path):
    pytest.importorskip("textual")
    _seed(tmp_path, "ren00000", "исходный промпт")
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._apply_rename("ren00000", "Переименовано")  # прямой вызов колбэка
        await pilot.pause()
        meta = FileTaskStore(threads_dir=str(tmp_path), system="cli").get_task_meta("ren00000")
        assert meta["tui"]["title"] == "Переименовано"


@pytest.mark.asyncio
async def test_dialog_summary_skipped_in_title(tmp_path):
    """Суммаризированный тред: первая запись live-файла — dialog_summary
    (technical user-message). Заголовок должен быть РЕАЛЬНЫМ промптом, а не
    дампом саммари (регрессия на реальном payload)."""
    pytest.importorskip("textual")
    from message_policy.user_message_policy import (
        TECHNICAL_TYPE_DIALOG_SUMMARY, wrap_technical_user_content,
    )
    tid = "summ0001"
    summary = {"role": "user", "id": "m_sum",
               "content": wrap_technical_user_content(
                   "Summarized context from previous dialogue.\n"
                   "<summary>огромный технический дамп</summary>",
                   technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY)}
    real = {"role": "user", "content": "реальный вопрос после саммари", "id": "m_real"}
    append_new_thread_messages(str(tmp_path), "cli", tid, [summary, real], set())
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                    "updated_at": "2026-05-30T10:00:00+00:00", "messages_count": 2}),
        encoding="utf-8")
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "реальный вопрос после саммари" in text
        assert "Summarized context" not in text
        assert "огромный технический дамп" not in text


@pytest.mark.asyncio
async def test_first_prompt_from_archive(tmp_path):
    """Суммаризированный тред: исходный промпт в __pre_summary__ архиве,
    live-файл начинается с dialog_summary. Заголовок = исходный промпт, а не
    «(без заголовка)»."""
    pytest.importorskip("textual")
    from message_policy.user_message_policy import (
        TECHNICAL_TYPE_DIALOG_SUMMARY, wrap_technical_user_content,
    )
    tid = "arch0001"
    # Старый архив = исходная сырая история (тут первый реальный промпт).
    append_new_thread_messages(str(tmp_path), "cli", tid,
                               [{"role": "user", "content": "исходный вопрос из архива",
                                 "id": "m_o"}], set())
    live = Path(tmp_path) / f"data_cli_{tid}.jsonl"
    live.replace(Path(tmp_path) / f"data_cli_{tid}__pre_summary__20260530_101200.jsonl")
    # Live начинается с dialog_summary (как после компакции).
    summary = {"role": "user", "id": "m_sum",
               "content": wrap_technical_user_content(
                   "Summarized context from previous dialogue.\n<summary>x</summary>",
                   technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY)}
    append_new_thread_messages(str(tmp_path), "cli", tid, [summary], set())
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                    "updated_at": "2026-05-30T10:00:00+00:00", "messages_count": 5}),
        encoding="utf-8")
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "исходный вопрос из архива" in text
        assert "(без заголовка)" not in text


@pytest.mark.asyncio
async def test_pages_replace_not_grow(tmp_path):
    pytest.importorskip("textual")
    from tui.screens.sessions import SessionsScreen, _PAGE_SIZE
    from tui.app import TuiApp
    total = _PAGE_SIZE + 5
    rest = total - _PAGE_SIZE
    for i in range(total):
        _seed(tmp_path, f"pg{i:06d}", f"промпт {i}")
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        # страница 0: _PAGE_SIZE карточек, без «показать ещё»
        cards = pilot.app.screen.query("#sessions-list .card")
        assert len(cards) == _PAGE_SIZE
        hdr = "\n".join(str(s.render()) for s in pilot.app.screen.query("#sessions-header-left"))
        assert f"1–{_PAGE_SIZE} из {total}" in hdr
        assert "показать ещё" not in _rows_text(pilot)
        await pilot.press("pagedown")
        await pilot.pause()
        cards = pilot.app.screen.query("#sessions-list .card")
        assert len(cards) == rest
        hdr = "\n".join(str(s.render()) for s in pilot.app.screen.query("#sessions-header-left"))
        assert f"{_PAGE_SIZE + 1}–{total} из {total}" in hdr
        await pilot.press("pageup")
        await pilot.pause()
        assert f"1–{_PAGE_SIZE} из {total}" in "\n".join(
            str(s.render()) for s in pilot.app.screen.query("#sessions-header-left"))


@pytest.mark.asyncio
async def test_home_end_within_page(tmp_path):
    pytest.importorskip("textual")
    from tui.screens.sessions import SessionsScreen, _PAGE_SIZE
    from tui.app import TuiApp
    for i in range(_PAGE_SIZE + 5):
        _seed(tmp_path, f"he{i:06d}", f"промпт {i}")
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("end")
        await pilot.pause()
        assert scr._selected_idx == _PAGE_SIZE - 1   # последняя на полной странице
        await pilot.press("home")
        await pilot.pause()
        assert scr._selected_idx == 0


@pytest.mark.asyncio
async def test_filter_resets_to_first_page(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Input
    from tui.screens.sessions import SessionsScreen, _PAGE_SIZE
    from tui.app import TuiApp
    for i in range(_PAGE_SIZE + 5):
        _seed(tmp_path, f"fp{i:06d}", f"промпт {i}")
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._page == 1
        scr.query_one("#sessions-filter", Input).value = "промпт 1"
        await pilot.pause()
        assert scr._page == 0


@pytest.mark.asyncio
async def test_ctrl_p_opens_preview(tmp_path):
    """Ctrl+P открывает превью сессии (встроенная палитра Textual отключена и
    не перехватывает Ctrl+P)."""
    pytest.importorskip("textual")
    _seed(tmp_path, "prev0001", "промпт для превью")
    from tui.screens.sessions import SessionsScreen
    from tui.screens.preview import PreviewScreen
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(pilot.app.screen, PreviewScreen)


@pytest.mark.asyncio
async def test_single_delete_removes_archives_too(tmp_path):
    pytest.importorskip("textual")
    from pathlib import Path
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    _seed(tmp_path, "del0001", "промпт")
    archive = Path(tmp_path) / "data_cli_del0001__pre_summary__20260101_000000.jsonl"
    archive.write_text("x", encoding="utf-8")
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._delete_task("del0001")
        await pilot.pause()
        assert not archive.exists()
        assert not (Path(tmp_path) / "meta_cli_del0001.json").exists()
        assert len(scr._tasks) == 0


@pytest.mark.asyncio
async def test_ctrl_d_opens_mass_delete(tmp_path):
    pytest.importorskip("textual")
    from tui.screens.sessions import SessionsScreen
    from tui.screens.mass_delete import MassDeleteScreen
    from tui.app import TuiApp
    _seed(tmp_path, "m0000001", "промпт")
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert isinstance(pilot.app.screen, MassDeleteScreen)


@pytest.mark.asyncio
async def test_mass_delete_keep_last_cycle(tmp_path):
    pytest.importorskip("textual")
    from datetime import datetime, timezone
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    for i in range(3):
        _seed(tmp_path, f"c{i:07d}", f"промпт {i}",
              updated_at=f"2026-06-{3 - i:02d}T00:00:00+00:00")
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        now = datetime(2026, 6, 6, tzinfo=timezone.utc)
        scr._confirm_mass_delete(scr._store.list_tasks(limit=100), {"keep_last": 1}, now)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert len(scr._store.list_tasks(limit=100)) == 1
