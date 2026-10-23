"""Тихая сводка правок хода в строке итога (вместо CTA-блока).

Три слоя:
* чистые хелперы сервиса итогов (`format_turn_summary` /
  `compose_status_line`);
* проводка спадающего фронта в TraceScreen (сводка у свежей строки итога в
  ленте, S10; отсутствие CTA в ленте);
* восстановление строки итога на resume (цифры только из meta того же хода).
"""

import json
import re

import pytest
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
    edits_journal_path,
)
from gc_testkit.paths import module_dir
from gc_testkit.tui_support import tui_settings, wait_until
from gc_tui.services.trace_render import build_turn_line
from gc_tui.services.turn_outcome import (
    compose_status_line,
    format_turn_summary,
)
from rich.text import Text


def _write_task(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _done_turn(tmp_path, tid):
    """Закрытый ход: реплика и запись итога ядра «за 12с»."""
    append_new_thread_messages(
        str(tmp_path),
        "cli",
        tid,
        [{"id": "u1", "role": "user", "content": "q"}],
        set(),
    )
    outcome = TurnOutcome(
        status="done",
        reason=None,
        opened_by="u1",
        started_at="2026-10-22T11:32:00Z",
        ended_at="",
        duration_sec=12.0,
        error=None,
    )
    append_turn_outcome_record(str(tmp_path), "cli", tid, outcome, seq=1)


def _append_edit(
    tmp_path, tid, *, path, turn, add, del_, call_id="c1", op="edit"
):
    journal = EditsJournal(edits_journal_path(str(tmp_path), "cli", tid))
    journal.append(
        EditRecord(
            path=path,
            turn_id=turn,
            ts=1.0,
            call_id=call_id,
            op=op,
            hunks="@@ -1 +1 @@\n-a\n+b\n",
            add=add,
            del_=del_,
            before_sha256="x",
            after_sha256="y",
            flags=[],
        )
    )


def _edit_on_disk(tmp_path, tid, *, path, turn, before, after, call_id="c1"):
    """Правка «как в жизни»: turn-снимок + файл на диске + запись в журнале.

    Цифры сводки считаются по итогу (снимок против диска), а не по счётчикам
    записей, поэтому файлы и снапшоты обязаны быть настоящими.

    `before is None` — файла до хода не было: снимать нечего, и fs-тул пишет
    такую правку как `op="create"` (именно по нему сводка и опознаёт, что
    пустая база честная).
    """
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    if before is not None:
        SnapshotStore(
            snapshots_root(str(root)), f"cli_{tid}", tmp_path
        ).capture_turn(path, before.encode("utf-8"), turn)
    (root / path).write_text(after, encoding="utf-8", newline="\n")
    _append_edit(
        tmp_path,
        tid,
        path=path,
        turn=turn,
        add=0,
        del_=0,
        call_id=call_id,
        op="edit" if before is not None else "create",
    )


class _Runner:
    def __init__(self, running):
        self._running = running

    def is_running(self):
        return self._running


# --- чистые хелперы ---------------------------------------------------------


def test_format_turn_summary_files_add_del():
    assert format_turn_summary(3, 120, 15) == "изменено 3 файла · +120 −15"


def test_format_turn_summary_plural_forms():
    assert format_turn_summary(1, 0, 0).startswith("изменено 1 файл ")
    assert format_turn_summary(2, 0, 0).startswith("изменено 2 файла ")
    assert format_turn_summary(5, 0, 0).startswith("изменено 5 файлов ")
    assert format_turn_summary(11, 0, 0).startswith("изменено 11 файлов ")
    assert format_turn_summary(21, 0, 0).startswith("изменено 21 файл ")


def test_format_turn_summary_none_without_files():
    assert format_turn_summary(0, 0, 0) is None


def test_compose_status_line_pins_segment_to_right_edge():
    head = Text("✓ Выполнил за 12с")
    out, _span = compose_status_line(head, "изменено 1 файл · +1 −0", 60)
    assert out.cell_len == 60
    assert out.plain.startswith("✓ Выполнил за 12с ")
    assert out.plain.endswith("изменено 1 файл · +1 −0")


def test_compose_status_line_hides_segment_when_narrow():
    head = Text("✓ Выполнил за 12с")
    out, _span = compose_status_line(head, "изменено 1 файл · +1 −0", 20)
    assert out.plain == "✓ Выполнил за 12с"
    assert out.cell_len <= 20


def test_compose_status_line_without_segment_returns_head():
    head = Text("✓ Выполнил за 12с")
    out, _span = compose_status_line(head, None, 200)
    assert out.plain == "✓ Выполнил за 12с"


# --- x-диапазон сегмента: без него «клик именно по сводке» нереализуем --------


def test_compose_status_line_reports_summary_span():
    """Диапазон обязан указывать РОВНО на сегмент, а не «где-то справа»."""
    summary = "изменено 1 файл · +1 −0"
    head = Text("✓ Выполнил за 12с")
    out, span = compose_status_line(head, summary, 60)
    assert span == (60 - len(summary), 60)
    assert out.plain[span[0] : span[1]] == summary


def test_span_is_none_when_summary_hidden():
    """Сегмент не влез — кликать не по чему, диапазона нет."""
    head = Text("✓ Выполнил за 12с")
    _out, span = compose_status_line(head, "изменено 1 файл · +1 −0", 20)
    assert span is None


def test_span_is_none_without_summary():
    head = Text("✓ Выполнил за 12с")
    _out, span = compose_status_line(head, None, 200)
    assert span is None


# --- строка итога без записи: исход раннера или статуса сессии -------------


def test_turn_line_without_record_has_head_without_time():
    line = build_turn_line(word="interrupted")
    assert line.text.plain == "■ Прервано"


def test_turn_line_needs_record_or_word():
    with pytest.raises(ValueError):
        build_turn_line()


def test_turn_line_takes_record_or_word_not_both():
    outcome = TurnOutcome(
        status="done",
        reason=None,
        opened_by="u1",
        started_at="2026-10-22T11:32:00Z",
        ended_at="2026-10-22T11:32:12Z",
        duration_sec=12.0,
        error=None,
    )
    with pytest.raises(ValueError):
        build_turn_line(outcome, word="completed")


def test_hover_paints_the_segment_with_the_accent_of_block_buttons():
    """Ревизия 7: наведение — АКЦЕНТ, тот же, что у кнопок блоков.

    Живой смоук показал сегмент рядом с настоящими hover'ами базы: светление
    ($bright) читалось как другой жест. В проекте всего два hover-правила —
    `.diff-more:hover` и `.block-copy:hover`, оба `$accent`; сегмент кликабелен
    ровно так же и обязан говорить тем же цветом. Страж от возврата к $bright.
    """
    from gc_tui.palette import ACCENT, BRIGHT, DEEP_DIM

    summary = "изменено 1 файл · +1 −0"
    head = Text("✓ Выполнил за 12с")
    quiet, _span = compose_status_line(head, summary, 60)
    hot, _span = compose_status_line(head, summary, 60, hover=True)

    assert quiet.plain == hot.plain
    styles = {str(span.style) for span in hot.spans if span.style}
    assert ACCENT in styles
    assert BRIGHT not in styles and DEEP_DIM not in styles


def test_hover_speaks_the_same_color_as_every_hover_rule_of_the_base():
    """Сегмент и hover-правила `styles.tcss` — ОДИН цвет, а не «похожий».

    Токен сверяется с самим CSS, а не с константой рядом: разъехаться эта пара
    может только здесь — в базе одно hover-правило, `.trace-action:hover`, про
    наведение на действие ленты.
    """

    from gc_tui.palette import css_variables

    tcss_path = module_dir("tui") / "styles.tcss"
    tcss = tcss_path.read_text(encoding="utf-8")
    tokens = set(
        re.findall(
            r"\.trace-action:hover\s*\{\s*color:\s*\$([\w-]+)",
            tcss,
        )
    )
    assert tokens, "hover-правила базы не найдены — сверять не с чем"

    hot, _span = compose_status_line(
        Text("✓"), "изменено 1 файл · +1 −0", 60, hover=True
    )
    styles = {str(span.style) for span in hot.spans if span.style}
    assert styles == {css_variables()[token] for token in tokens}


@pytest.mark.parametrize(
    "state, head",
    [
        ("failed", "✕ Ошибка"),
        ("cancelled", "■ Отменено"),
        ("stopped_steps", "⏹ Остановлено: превышен лимит шагов"),
        ("stopped_errors", "⏹ Остановлено: превышен лимит ошибок"),
        ("stopped_tokens", "⏹ Остановлено: превышен лимит токенов"),
        (
            "stopped_tool_calls",
            "⏹ Остановлено: превышен лимит вызовов инструментов",
        ),
    ],
)
def test_every_outcome_line_shows_the_edits_summary(state, head):
    """Ход, упавший или остановленный, файлы уже менял — и об этом строка говорит.

    Сводку сохраняли для любого исхода, а рисовали только у «Выполнил»: после
    ошибки пользователь видел одно «Ошибка», хотя именно тогда ему важнее всего
    знать, что файлы тронуты (ревью PR, находка 7)."""
    line = build_turn_line(word=state)
    line.freshen((1, 1, 1), None)
    plain = line.render().plain
    assert plain.startswith(head)
    assert plain.endswith("изменено 1 файл · +1 −1")
    assert "изменено" in line.render().plain


# --- проводка спадающего фронта --------------------------------------------


def _fresh(scr):
    """Свежая строка итога последнего хода; индикатор ушёл."""
    from gc_tui.widgets.turn_line import TurnLine

    assert scr._thinking is None
    (line,) = scr.query(TurnLine).filter(".-latest")
    return line


def _outcome_text(scr) -> str:
    return _fresh(scr).render().plain


async def _falling_edge(pilot, scr, tid):
    """Ход завершился: индикатор жив, runner уже idle, был running на прошлом тике."""
    scr.attach_active_task(tid)
    scr._show_thinking_indicator()
    scr._was_running = True
    pilot.app.active_runner = _Runner(running=False)
    pilot.app.last_stop_requested = False
    pilot.app.last_runner_error = None
    scr._poll_active()
    await pilot.pause()


@pytest.mark.asyncio
async def test_turn_with_edits_shows_summary_from_journal(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000001a"
    _write_task(tmp_path, tid)
    _edit_on_disk(
        tmp_path,
        tid,
        path="a.py",
        turn="T1",
        before="a\nb\nc\n",
        after="a\nB\nc\n",
        call_id="c1",
    )  # +1 −1
    _edit_on_disk(
        tmp_path,
        tid,
        path="b.py",
        turn="T1",
        before=None,
        after="one\ntwo\n",
        call_id="c2",
    )  # +2 (создан)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "launch_root": str(tmp_path / "proj")}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await _falling_edge(pilot, scr, tid)
        assert _outcome_text(scr).endswith("изменено 2 файла · +3 −1")


@pytest.mark.asyncio
async def test_turn_summary_shows_net_delta_not_sum_of_records(tmp_path):
    """Главный регресс: три правки одного файла за ход не складываются.

    Живой случай — правка fs-тулом плюс две перезаписи файла целиком через
    шелл: счётчики записей дают «+293 −22» на файле в 133 строки, сводка
    обязана показать итог хода.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000008a"
    _write_task(tmp_path, tid)
    before = "".join(f"old{i}\n" for i in range(22))
    after = "".join(f"new{i}\n" for i in range(133))
    _edit_on_disk(
        tmp_path,
        tid,
        path="calc.py",
        turn="T1",
        before=before,
        after=after,
        call_id="c1",
    )
    _append_edit(
        tmp_path, tid, path="calc.py", turn="T1", add=133, del_=0, call_id="c2"
    )
    _append_edit(
        tmp_path, tid, path="calc.py", turn="T1", add=132, del_=0, call_id="c3"
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "launch_root": str(tmp_path / "proj")}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await _falling_edge(pilot, scr, tid)
        assert _outcome_text(scr).endswith("изменено 1 файл · +133 −22")


@pytest.mark.asyncio
async def test_turn_without_edits_has_no_segment(tmp_path):
    """Журнал не дописан за ход — цифры прошлого хода в строку не попадают."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000002a"
    _write_task(tmp_path, tid)
    _append_edit(tmp_path, tid, path="a.py", turn="T0", add=9, del_=9)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._stamp_tui_running()  # старт нового хода → отметка журнала
        await _falling_edge(pilot, scr, tid)
        assert "изменено" not in _outcome_text(scr)


@pytest.mark.asyncio
async def test_second_stamp_does_not_move_the_turn_boundary(tmp_path):
    """Повторная отметка старта не съедает сводку уже начавшегося хода.

    Отметку ставят дважды: из подготовки прогона и из страховочной проверки на
    восходящем фронте, а она приходит с тиком интерфейса. Успела первая правка
    записаться раньше тика — и граница хода переезжала НА НЕЁ: сводка
    «изменено N файлов» пропадала, в meta треда не попадал last_indicator
    (пятый раунд ревью, находка 6).

    Проявлялось на быстрой модели и при занятом интерфейсе — то есть случайно,
    и тем неприятнее.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000009a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._stamp_tui_running()  # подготовка прогона
        _append_edit(tmp_path, tid, path="a.py", turn="T1", add=3, del_=1)
        scr._stamp_tui_running()  # страховочная сетка, уже после правки
        await _falling_edge(pilot, scr, tid)

        assert "изменено" in _outcome_text(scr)


@pytest.mark.asyncio
async def test_empty_journal_has_no_segment(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000003a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await _falling_edge(pilot, scr, tid)
        assert "изменено" not in _outcome_text(scr)


@pytest.mark.asyncio
async def test_config_switch_off_hides_segment(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000004a"
    _write_task(tmp_path, tid)
    _append_edit(tmp_path, tid, path="a.py", turn="T1", add=3, del_=1)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "agent_edits": {"turn_summary_in_status": False},
            }
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await _falling_edge(pilot, scr, tid)
        assert "изменено" not in _outcome_text(scr)


@pytest.mark.asyncio
async def test_no_cta_block_in_trace_after_turn_with_edits(tmp_path):
    """Главный регресс задачи: блока-призыва «зайдите в /diff» в ленте больше нет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    tid = "turn0000000000000000000000000005a"
    _write_task(tmp_path, tid)
    _append_edit(tmp_path, tid, path="a.py", turn="T1", add=3, del_=1)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await _falling_edge(pilot, scr, tid)
        assert not any(
            "/diff" in str(row._content) for row in scr.query(TraceRow)
        )
        assert not hasattr(scr, "_maybe_mount_diff_pending_cta")
        assert not hasattr(scr, "_flush_diff_pending_cta")
        assert not hasattr(scr, "_diff_pending_cta_pending")


# --- resume -----------------------------------------------------------------


# --- клик по сегменту -------------------------------------------------------


async def _turn_with_edits(pilot, tmp_path, tid):
    """Завершённый ход с правками: свежая строка показывает сегмент
    сводки."""
    scr = pilot.app.screen
    await _falling_edge(pilot, scr, tid)
    # Потолок ленты считается от хрома прошлой раскладки: досчитать до
    # жестов, иначе тик опроса сдвинет строку под указателем.
    scr._sync_trace_max_height()
    await pilot.pause()
    ind = _fresh(scr)
    assert ind._summary_span is not None

    def _on_top() -> bool:
        region = ind.region
        return pilot.app.screen.get_widget_at(region.x, region.y)[0] is ind

    # Строка в ленте: жесты — когда раскладка улеглась и строку не
    # накрывает бар.
    await wait_until(pilot, _on_top, "строка итога под указателем")
    return scr, ind


def _post_gesture(pilot, widget, down_dx, up_dx=None, *, button=1):
    """Положить нажатие и отпускание в очередь приложения — БЕЗ ожидания.

    `pilot.click` фабрикует `Click` сам и обходит `App.on_event` — а именно там
    нажатие связывается с отпусканием и считается `chain` двойного клика.
    Жесты, которые виджет обязан различать, проверяются только этим путём;
    синтетический `on_click` их различий не показывает вовсе.

    Ждать внутри нельзя: два жеста подряд обязаны лечь в очередь ДО того, как
    из первого вырастет новый экран, — иначе второй уедет уже ему, и гонка,
    ради которой тест написан, не воспроизведётся.
    """
    from textual.events import MouseDown, MouseUp

    if up_dx is None:
        up_dx = down_dx
    y = widget.region.y

    for cls, dx in ((MouseDown, down_dx), (MouseUp, up_dx)):
        x = widget.region.x + dx
        pilot.app.post_message(
            cls(
                widget,
                x=x,
                y=y,
                delta_x=0,
                delta_y=0,
                button=button,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=x,
                screen_y=y,
            )
        )


async def _gesture(pilot, widget, down_dx, up_dx=None, *, button=1):
    """Один жест мыши, доведённый до последствий."""
    _post_gesture(pilot, widget, down_dx, up_dx, button=button)
    await pilot.pause()
    await pilot.pause()


def _diff_screens(pilot):
    from gc_tui.screens.diff import DiffScreen

    return [s for s in pilot.app.screen_stack if isinstance(s, DiffScreen)]


async def _wait_diff_screen(pilot):
    """Запрос всплывает из строки ленты через контейнеры до экрана —
    несколько тактов."""
    await wait_until(
        pilot, lambda: len(_diff_screens(pilot)) == 1, "экран изменений"
    )


async def _app_with_turn_edits(tmp_path, tid):
    from gc_tui.app import TuiApp

    _write_task(tmp_path, tid)
    _edit_on_disk(
        tmp_path, tid, path="a.py", turn="T1", before="a\nb\n", after="a\nB\n"
    )
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "launch_root": str(tmp_path / "proj")}
        ),
    )


@pytest.mark.asyncio
async def test_click_inside_summary_span_opens_screen_in_turn_source(tmp_path):
    """Бэклог §8 №11: клик по сводке открывает экран сразу в источнике «Ход»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.services.edits_view import SOURCE_TURN

    tid = "turn0000000000000000000000000009a"
    _write_task(tmp_path, tid)
    _edit_on_disk(
        tmp_path, tid, path="a.py", turn="T1", before="a\nb\n", after="a\nB\n"
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "launch_root": str(tmp_path / "proj")}
        ),
    )
    async with app.run_test() as pilot:
        _scr, ind = await _turn_with_edits(pilot, tmp_path, tid)
        start, _end = ind._summary_span

        # Не pilot.click: он обходит App.on_event, и on_mouse_down не всегда
        # успевает выставить _pressed_in_summary — под нагрузкой клик молчит.
        # Тот же путь, что у test_single_click_still_opens_the_screen_through_the_queue.
        await _gesture(pilot, ind, start + 1)
        await _wait_diff_screen(pilot)

        assert isinstance(pilot.app.screen, DiffScreen)
        assert pilot.app.screen.source == SOURCE_TURN


@pytest.mark.asyncio
async def test_click_outside_span_does_nothing(tmp_path):
    """Строка-итог целиком кликом ничего не значит — только сам сегмент."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen

    tid = "turn0000000000000000000000000010a"
    _write_task(tmp_path, tid)
    _edit_on_disk(
        tmp_path, tid, path="a.py", turn="T1", before="a\nb\n", after="a\nB\n"
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "launch_root": str(tmp_path / "proj")}
        ),
    )
    async with app.run_test() as pilot:
        scr, ind = await _turn_with_edits(pilot, tmp_path, tid)
        start, _end = ind._summary_span

        await pilot.click(ind, offset=(0, 0))  # по строке-итогу
        await pilot.pause()
        assert pilot.app.screen is scr

        await pilot.click(ind, offset=(start - 1, 0))  # вплотную к сегменту
        await pilot.pause()
        assert pilot.app.screen is scr
        assert not isinstance(pilot.app.screen, DiffScreen)


@pytest.mark.asyncio
async def test_hover_over_segment_lightens_it_and_leaving_dims_back(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000011a"
    _write_task(tmp_path, tid)
    _edit_on_disk(
        tmp_path, tid, path="a.py", turn="T1", before="a\nb\n", after="a\nB\n"
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "launch_root": str(tmp_path / "proj")}
        ),
    )
    async with app.run_test() as pilot:
        _scr, ind = await _turn_with_edits(pilot, tmp_path, tid)
        start, _end = ind._summary_span

        await pilot.hover(ind, offset=(start + 1, 0))
        await pilot.pause()
        assert ind._summary_hover is True

        await pilot.hover(ind, offset=(0, 0))
        await pilot.pause()
        assert ind._summary_hover is False


# --- жесты мыши, которые открывать экран НЕ должны --------------------------


@pytest.mark.asyncio
async def test_double_click_on_summary_opens_exactly_one_screen(tmp_path):
    """Клик-модель базы вторым кликом ничего не открывает (`click_model`).

    Живой промах: запрос всплывает несколько тактов, и второй `Click` успевал
    прийти в тот же индикатор — экран открывался дважды, и первый Esc его «не
    закрывал».
    """
    pytest.importorskip("textual")
    from gc_tui.messages import OpenEditsScreenRequest

    tid = "turn0000000000000000000000000013a"
    app = await _app_with_turn_edits(tmp_path, tid)
    async with app.run_test() as pilot:
        _scr, ind = await _turn_with_edits(pilot, tmp_path, tid)
        start, _end = ind._summary_span
        depth = len(pilot.app.screen_stack)
        # Считаем сами ЗАПРОСЫ, а не только экраны: страховка `main.py` (экран
        # не на вершине — запрос протух) прикрыла бы лишний клик и без виджета,
        # и защита второго клика осталась бы непроверенной.
        requests: list = []
        emit = ind.post_message

        def _spy(message):
            if isinstance(message, OpenEditsScreenRequest):
                requests.append(message)
            return emit(message)

        ind.post_message = _spy

        _post_gesture(pilot, ind, start + 1)
        _post_gesture(pilot, ind, start + 1)  # тот же пиксель → chain 2
        await pilot.pause()
        await pilot.pause()
        await _wait_diff_screen(pilot)

        assert len(requests) == 1  # второй клик молчит
        assert len(_diff_screens(pilot)) == 1
        assert len(pilot.app.screen_stack) == depth + 1


@pytest.mark.asyncio
async def test_selection_drag_released_on_summary_does_not_open(tmp_path):
    """Протяжка выделения по строке-итогу — жест про текст, а не про экран."""
    pytest.importorskip("textual")
    tid = "turn0000000000000000000000000014a"
    app = await _app_with_turn_edits(tmp_path, tid)
    async with app.run_test() as pilot:
        scr, ind = await _turn_with_edits(pilot, tmp_path, tid)
        start, _end = ind._summary_span

        await _gesture(
            pilot, ind, 0, start + 1
        )  # нажали слева, отпустили на сводке

        assert _diff_screens(pilot) == []
        assert pilot.app.screen is scr


@pytest.mark.asyncio
async def test_right_click_on_summary_does_nothing(tmp_path):
    """У правой кнопки в интерфейсе своей семантики нет."""
    pytest.importorskip("textual")
    tid = "turn0000000000000000000000000015a"
    app = await _app_with_turn_edits(tmp_path, tid)
    async with app.run_test() as pilot:
        scr, ind = await _turn_with_edits(pilot, tmp_path, tid)
        start, _end = ind._summary_span

        await _gesture(pilot, ind, start + 1, button=3)

        assert _diff_screens(pilot) == []
        assert pilot.app.screen is scr


@pytest.mark.asyncio
async def test_single_click_still_opens_the_screen_through_the_queue(tmp_path):
    """Страж от «починили запретами всё, включая сам жест»."""
    pytest.importorskip("textual")
    from gc_tui.services.edits_view import SOURCE_TURN

    tid = "turn0000000000000000000000000016a"
    app = await _app_with_turn_edits(tmp_path, tid)
    async with app.run_test() as pilot:
        _scr, ind = await _turn_with_edits(pilot, tmp_path, tid)
        start, _end = ind._summary_span

        await _gesture(pilot, ind, start + 1)
        await _wait_diff_screen(pilot)

        assert len(_diff_screens(pilot)) == 1
        assert pilot.app.screen.source == SOURCE_TURN


@pytest.mark.asyncio
async def test_stale_request_does_not_stack_over_the_open_screen(tmp_path):
    """Системная страховка: запрос исполняется, пока трейс на вершине стека."""
    pytest.importorskip("textual")
    from gc_tui.messages import OpenEditsScreenRequest
    from gc_tui.services.edits_view import SOURCE_TURN

    tid = "turn0000000000000000000000000017a"
    app = await _app_with_turn_edits(tmp_path, tid)
    async with app.run_test() as pilot:
        scr, ind = await _turn_with_edits(pilot, tmp_path, tid)
        start, _end = ind._summary_span
        await _gesture(pilot, ind, start + 1)
        await _wait_diff_screen(pilot)
        depth = len(pilot.app.screen_stack)

        scr.post_message(OpenEditsScreenRequest(SOURCE_TURN))  # запоздавший
        await pilot.pause()

        assert len(_diff_screens(pilot)) == 1
        assert len(pilot.app.screen_stack) == depth


@pytest.mark.asyncio
async def test_click_on_indicator_without_summary_is_inert(tmp_path):
    """Ход без правок: сегмента нет, и клик по строке-итогу ничего не открывает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen

    tid = "turn0000000000000000000000000012a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "launch_root": str(tmp_path / "proj")}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await _falling_edge(pilot, scr, tid)
        ind = _fresh(scr)
        assert ind._summary_span is None

        await pilot.click(ind, offset=(2, 0))
        await pilot.pause()
        assert pilot.app.screen is scr
        assert not isinstance(pilot.app.screen, DiffScreen)


@pytest.mark.asyncio
async def test_resume_shows_summary_saved_with_indicator(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000006a"
    _write_task(tmp_path, tid)
    _done_turn(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._store.update_tui_meta(
            tid,
            {"status": "completed", "last_indicator": {"edits": [2, 5, 6]}},
        )
        pilot.app.active_runner = None
        scr._destroy_thinking_indicator()
        scr._restore_latest_turn_line()
        await pilot.pause()
        plain = _outcome_text(scr)
        assert plain.startswith("✓ Выполнил за 12с")
        assert plain.endswith("изменено 2 файла · +5 −6")


@pytest.mark.asyncio
async def test_resume_without_saved_summary_shows_no_stale_numbers(tmp_path):
    """Старая запись без `edits`: правки прошлых ходов в журнале есть, но цифры
    рядом с итогом НЕ показываем — они принадлежат не тому ходу."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "turn0000000000000000000000000007a"
    _write_task(tmp_path, tid)
    _done_turn(tmp_path, tid)
    _append_edit(tmp_path, tid, path="a.py", turn="T0", add=9, del_=9)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._store.update_tui_meta(
            tid,
            {"status": "completed", "last_indicator": {"edits": None}},
        )
        pilot.app.active_runner = None
        scr._destroy_thinking_indicator()
        scr._restore_latest_turn_line()
        await pilot.pause()
        assert _outcome_text(scr) == "✓ Выполнил за 12с"


def test_finished_turn_summary_survives_a_busy_snapshot_index():
    """Сводка хода на занятом индексе снимков — не показывается, а не роняет TUI.

    Сводку зовёт таймер (`_poll_active`) по завершении хода, а база хода для
    счётчиков читается из индекса снимков под замком: занятый другим писателем
    замок отвечал таймаутом прямо из колбэка таймера и завершал приложение.
    Сводка — украшение строки-итога: не прочиталась — её просто нет."""
    from types import SimpleNamespace

    from gc_tui.screens.main import TraceScreen

    class _BusyLookup:
        def last_turn_id(self):
            return "t2"

        def turn_summary(self, turn_id):
            raise TimeoutError("индекс снимков занят")

    screen = SimpleNamespace(
        _turn_summary_in_status=lambda: True,
        _edits_lookup_for_thread=lambda _tid=None: _BusyLookup(),
        _edits_baseline_turn_id="t1",
    )

    assert TraceScreen._finished_turn_summary(screen) is None


# --- клик по сводке свежей строки итога в ленте (S10) -----------------------


async def _line_app_requests(line, gestures):
    """Свежая строка со сводкой в тестовом приложении: жесты по ней и
    запросы экрана изменений, которые до приложения дошли."""
    from gc_tui.messages import OpenEditsScreenRequest

    from test_turn_line_widget import LineApp

    class _App(LineApp):
        def __init__(self, line) -> None:
            super().__init__(line)
            self.requests: list = []

        def on_open_edits_screen_request(self, message) -> None:
            assert isinstance(message, OpenEditsScreenRequest)
            self.requests.append(message.source)

    app = _App(line)
    async with app.run_test(size=(80, 10)) as pilot:
        line.freshen((2, 14, 3), None)
        await pilot.pause()
        line.render()
        await gestures(pilot, line)
        await pilot.pause()
        return app.requests


def _fresh_line():
    return build_turn_line(word="completed")


def _summary_x(line) -> int:
    """Первая клетка сегмента сводки в отрисованной строке."""
    return line.render().plain.index("изменено")


@pytest.mark.asyncio
async def test_click_on_fresh_line_summary_sends_one_turn_request():
    from gc_tui.services.edits_view import SOURCE_TURN

    async def click(pilot, line):
        await _gesture(pilot, line, _summary_x(line) + 1)

    assert await _line_app_requests(_fresh_line(), click) == [SOURCE_TURN]


@pytest.mark.asyncio
async def test_double_click_on_fresh_line_summary_sends_one_request():
    async def double(pilot, line):
        x = _summary_x(line) + 1
        _post_gesture(pilot, line, x)
        _post_gesture(pilot, line, x)  # тот же пиксель → chain 2
        await pilot.pause()

    assert len(await _line_app_requests(_fresh_line(), double)) == 1


@pytest.mark.asyncio
async def test_right_click_on_fresh_line_summary_sends_nothing():
    async def right(pilot, line):
        await _gesture(pilot, line, _summary_x(line) + 1, button=3)

    assert await _line_app_requests(_fresh_line(), right) == []


@pytest.mark.asyncio
async def test_selection_drag_from_head_to_summary_sends_nothing():
    async def drag(pilot, line):
        await _gesture(pilot, line, 0, _summary_x(line) + 1)

    assert await _line_app_requests(_fresh_line(), drag) == []


@pytest.mark.asyncio
async def test_leaving_line_between_press_and_release_sends_nothing():
    from textual.events import Leave

    async def leave(pilot, line):
        from textual.events import MouseDown, MouseUp

        x = line.region.x + _summary_x(line) + 1
        y = line.region.y
        kw = dict(
            x=x,
            y=y,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=False,
            screen_x=x,
            screen_y=y,
        )
        pilot.app.post_message(MouseDown(line, **kw))
        await pilot.pause()
        line.post_message(Leave(line))
        await pilot.pause()
        pilot.app.post_message(MouseUp(line, **kw))
        await pilot.pause()

    assert await _line_app_requests(_fresh_line(), leave) == []


@pytest.mark.asyncio
async def test_settled_line_click_sends_nothing():
    async def settle_then_click(pilot, line):
        x = _summary_x(line) + 1
        line.settle()
        await pilot.pause()
        line.render()
        assert "изменено" not in line.render().plain
        await _gesture(pilot, line, x)

    assert await _line_app_requests(_fresh_line(), settle_then_click) == []
