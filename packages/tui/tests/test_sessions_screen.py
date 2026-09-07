"""SessionsScreen: карточки (2 строки), пагинация, фильтр веток, поиск, rename, preview."""
import pytest

import json
from pathlib import Path

from gc_core.thread_store import append_new_thread_messages
from gc_tui.services.stores.file_store import FileTaskStore


def _seed(threads_dir, tid, prompt, *, branch=None, title=None, last_actual=None,
          task=None, updated_at="2026-05-30T10:00:00+00:00", msgs=1, token_usage=None):
    """Сессия = data-файл (реальный writer) + meta с messages_count/updated_at
    (как пишет thread_store), плюс tui.branch/title/last_actual при необходимости."""
    append_new_thread_messages(str(threads_dir), "cli", tid,
                               [{"role": "user", "content": prompt, "id": f"m_{tid}"}], set())
    meta = {"thread_id": tid, "system": "cli", "status": "stopped",
            "updated_at": updated_at, "messages_count": msgs}
    tui = {}
    if branch is not None:
        tui["branch"] = branch
    if title is not None:
        tui["title"] = title
    if last_actual is not None:
        tui["last_actual"] = last_actual
    if task is not None:
        tui["task"] = task
    if tui:
        meta["tui"] = tui
    if token_usage is not None:
        meta["token_usage"] = token_usage
    (Path(threads_dir) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _rows_text(pilot):
    return "\n".join(str(s.render()) for s in pilot.app.screen.query("#sessions-list Static"))


@pytest.mark.asyncio
async def test_active_running_session_shows_running_not_completed(tmp_path):
    """Активная сессия с живым runner — [running], даже если meta.tui.status=completed."""
    pytest.importorskip("textual")
    tid = "run00001"
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "2026-05-30T10:00:00+00:00",
                    "messages_count": 5, "tui": {"status": "completed"}}),
        encoding="utf-8",
    )

    class _FakeRunner:
        def is_running(self):
            return True

    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        pilot.app.tui_state.active_task_id = tid
        pilot.app.active_runner = _FakeRunner()
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "[running]" in text
        assert "[completed]" not in text


@pytest.mark.asyncio
async def test_long_prompt_card_stays_two_lines(tmp_path):
    """Длинный (в т.ч. skill-) промпт: карточка остаётся РОВНО 2 строки —
    заголовок усечён `…`, а не растёт под весь промпт. Регресс: при height:auto
    без text-wrap:nowrap длинная строка переносилась и занимала десятки строк."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    long_prompt = "Используй skill `qa-req-validation`. " + ("очень длинный текст " * 80)
    _seed(tmp_path, "aaaaaaaa", long_prompt)
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        card = list(pilot.app.screen.query_one(
            "#sessions-list", VerticalScroll).query(".card"))[0]
        assert card.size.height == 2


@pytest.mark.asyncio
async def test_card_shows_task_title_and_last_actual_subtitle(tmp_path):
    """При наличии task карточка 3-строчная: task + last_actual + мета."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    _seed(
        tmp_path, "bbbbbbbb", "посмотри логи",
        task="Добавить поле task", last_actual="посмотри логи",
    )
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "Добавить поле task" in text
        assert "посмотри логи" in text
        card = list(pilot.app.screen.query_one(
            "#sessions-list", VerticalScroll).query(".card"))[0]
        assert card.size.height == 3


@pytest.mark.asyncio
async def test_card_shows_title_branch_msgs(tmp_path):
    pytest.importorskip("textual")
    _seed(tmp_path, "aaaaaaaa", "первый вопрос", branch="feat/x", last_actual="первый вопрос")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
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
async def test_card_shows_nonzero_session_cost(tmp_path):
    """Ненулевая стоимость из meta.token_usage — в meta-строке карточки."""
    pytest.importorskip("textual")
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 2_000_000, 0)  # при input=1.0 → 2.0 ₽
    _seed(tmp_path, "cccccccc", "с ценой", token_usage=acc.to_meta())
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    pricing = {
        "enabled": True, "input": 1.0, "output": 2.0,
        "currency": "₽", "per_tokens": 1_000_000,
    }
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"pricing": pricing, "model": "m"})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        # Чисто тарифный отчёт (нет факта провайдера) — тильды нет.
        assert "2.00 ₽" in text
        assert "1 сообщ." in text


@pytest.mark.asyncio
async def test_card_hides_zero_or_disabled_cost(tmp_path):
    """Без тарифа / пустой usage — стоимость в карточке не показываем."""
    pytest.importorskip("textual")
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 2_000_000, 0)
    _seed(tmp_path, "dddddddd", "без цены", token_usage=acc.to_meta())
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"pricing": {"enabled": False}, "model": "m"})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "₽" not in text
        assert "$" not in text


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
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        assert len(pilot.app.screen._all_tasks()) == 201


@pytest.mark.asyncio
async def test_custom_title_overrides_prompt(tmp_path):
    pytest.importorskip("textual")
    _seed(tmp_path, "bbbbbbbb", "длинный промпт", title="Мой заголовок")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "Мой заголовок" in text


@pytest.mark.asyncio
async def test_branch_filter_ctrl_b(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _seed(tmp_path, "onbranch0", "на ветке", branch="feature-x", last_actual="на ветке")
    _seed(tmp_path, "offbranch", "другая ветка", branch="master", last_actual="другая ветка")
    from gc_tui.screens import sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "current_branch", lambda cwd: "feature-x")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
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
    _seed(tmp_path, "find1111", "уникальный маркер альфа", last_actual="уникальный маркер альфа")
    _seed(tmp_path, "find2222", "совсем другое", last_actual="совсем другое")
    from gc_tui.screens.sessions import SessionsScreen
    from textual.widgets import Input
    from gc_tui.app import TuiApp
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
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
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
    """R7: заголовок = display_name(meta.tui) — сканирование логов удалено.
    Суммаризированный тред БЕЗ last_actual → «Без названия» (не дамп саммари,
    не сканирование live-файла). С last_actual → он, приоритет над логами."""
    pytest.importorskip("textual")
    from gc_core.message_policy.user_message_policy import TECHNICAL_TYPE_DIALOG_SUMMARY, wrap_technical_user_content
    tid = "summ0001"
    summary = {"role": "user", "id": "m_sum",
               "content": wrap_technical_user_content(
                   "Summarized context from previous dialogue.\n"
                   "<summary>огромный технический дамп</summary>",
                   technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY)}
    real = {"role": "user", "content": "реальный вопрос после саммари", "id": "m_real"}
    append_new_thread_messages(str(tmp_path), "cli", tid, [summary, real], set())
    # last_actual задан → именно он в заголовке, не саммари и не сканирование
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                    "updated_at": "2026-05-30T10:00:00+00:00", "messages_count": 2,
                    "tui": {"last_actual": "реальный вопрос после саммари"}}),
        encoding="utf-8")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "реальный вопрос после саммари" in text
        assert "Summarized context" not in text
        assert "огромный технический дамп" not in text


@pytest.mark.asyncio
async def test_first_prompt_from_archive(tmp_path):
    """R7: сканирование архивов удалено. Без last_actual в meta → «Без названия»
    (бэкфил логов вне scope спеки §4). С last_actual → он в заголовке."""
    pytest.importorskip("textual")
    from gc_core.message_policy.user_message_policy import TECHNICAL_TYPE_DIALOG_SUMMARY, wrap_technical_user_content
    tid = "arch0001"
    append_new_thread_messages(str(tmp_path), "cli", tid,
                               [{"role": "user", "content": "исходный вопрос из архива",
                                 "id": "m_o"}], set())
    live = Path(tmp_path) / f"data_cli_{tid}.jsonl"
    live.replace(Path(tmp_path) / f"data_cli_{tid}__pre_summary__20260530_101200.jsonl")
    summary = {"role": "user", "id": "m_sum",
               "content": wrap_technical_user_content(
                   "Summarized context from previous dialogue.\n<summary>x</summary>",
                   technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY)}
    append_new_thread_messages(str(tmp_path), "cli", tid, [summary], set())
    # БЕЗ last_actual → «Без названия» (сканирование архивов не выполняется)
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                    "updated_at": "2026-05-30T10:00:00+00:00", "messages_count": 5}),
        encoding="utf-8")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "Без названия" in text
        assert "(без заголовка)" not in text


@pytest.mark.asyncio
async def test_pages_replace_not_grow(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.screens.sessions import SessionsScreen, _PAGE_SIZE
    from gc_tui.app import TuiApp
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
    from gc_tui.screens.sessions import SessionsScreen, _PAGE_SIZE
    from gc_tui.app import TuiApp
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
    from gc_tui.screens.sessions import SessionsScreen, _PAGE_SIZE
    from gc_tui.app import TuiApp
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
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.app import TuiApp
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
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
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
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.screens.mass_delete import MassDeleteScreen
    from gc_tui.app import TuiApp
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
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
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


# ---------------------------------------------------------------------------
# Шаг 6: R7 (_title_of → display_name, без _first_prompt) + R10 (ctrl+к/К)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r7_title_uses_last_actual(tmp_path):
    """R7: meta.tui.last_actual задан → заголовок = last_actual (не сканирует логи)."""
    pytest.importorskip("textual")
    _seed(tmp_path, "la000001", "сырой промпт в логе", last_actual="реализуй спеку X")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "реализуй спеку X" in text
        assert "сырой промпт в логе" not in text  # логи не сканируются


@pytest.mark.asyncio
async def test_r7_manual_title_over_last_actual(tmp_path):
    """R7: title — заголовок; last_actual (без task) — подзаголовок карточки."""
    pytest.importorskip("textual")
    _seed(tmp_path, "prio0001", "лог-промпт", title="parser bugfix",
          last_actual="реализуй спеку")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert text.startswith("parser bugfix")
        assert "реализуй спеку" in text


@pytest.mark.asyncio
async def test_r7_empty_meta_unnamed(tmp_path):
    """R7: пустой tui (нет title, нет last_actual) → «Без названия» (не «(без заголовка)»)."""
    pytest.importorskip("textual")
    _seed(tmp_path, "empty0001", "какой-то промпт")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        text = _rows_text(pilot)
        assert "Без названия" in text
        assert "(без заголовка)" not in text


@pytest.mark.asyncio
async def test_r10_ctrl_cyrillic_k_lower_renames(tmp_path):
    """R10: ctrl+к (русская раскладка) → _rename_selected (паритет с TraceScreen)."""
    pytest.importorskip("textual")
    _seed(tmp_path, "rk000001", "промпт", last_actual="имя сессии")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        called = []
        scr._rename_selected = lambda: called.append(1)  # type: ignore[assignment]
        await pilot.press("ctrl+к")
        assert called == [1], called


@pytest.mark.asyncio
async def test_r10_ctrl_cyrillic_k_upper_renames(tmp_path):
    """R10: ctrl+К (Shift, русская раскладка) → _rename_selected."""
    pytest.importorskip("textual")
    _seed(tmp_path, "rk000002", "промпт", last_actual="имя сессии")
    from gc_tui.screens.sessions import SessionsScreen
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
        await pilot.app.push_screen(scr)
        await pilot.pause()
        called = []
        scr._rename_selected = lambda: called.append(1)  # type: ignore[assignment]
        await pilot.press("ctrl+К")
        assert called == [1], called


@pytest.mark.asyncio
async def test_search_matches_subtitle(tmp_path):
    """Поиск в пикере ищет и по подзаголовку (last_actual под агентским task)."""
    pytest.importorskip("textual")
    _seed(tmp_path, "sub11111", "уникальный маркер бета",
          task="Написать тесты", last_actual="уникальный маркер бета")
    _seed(tmp_path, "sub22222", "совсем другое", task="Починить парсер", last_actual="совсем другое")
    from gc_tui.screens.sessions import SessionsScreen
    from textual.widgets import Input
    from gc_tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        pilot.app.screen.query_one("#sessions-filter", Input).value = "бета"
        await pilot.pause()
        text = _rows_text(pilot)
        assert "Написать тесты" in text
        assert "Починить парсер" not in text
