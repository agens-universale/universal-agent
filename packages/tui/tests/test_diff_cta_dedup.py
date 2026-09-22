"""Отменённый ход — тоже ход: спадающий фронт is_running монтирует CTA про диффы
заново. Если состав pending не менялся и CTA уже последний в трейсе — повтор
не нужен (смок 2026-08-02: четыре одинаковых блока подряд)."""
import json

import pytest


class _FakeEntry:
    def __init__(self, key):
        self.key = key


class _FakeDiffService:
    def __init__(self, keys):
        self._keys = keys

    def list_entries(self, pending_only=False):
        return [_FakeEntry(k) for k in self._keys]


def _cta_rows(scr):
    """Строки трейса, которые являются CTA про диффы."""
    from gc_tui.widgets.trace_row import TraceRow
    return [w for w in scr.query(TraceRow) if "F3/diff" in getattr(w, "_content", "")]


@pytest.mark.asyncio
async def test_second_identical_cta_is_not_mounted():
    """Второй ход подряд с тем же составом диффов не добавляет вторую строку."""
    from gc_tui.app import TuiApp

    app = TuiApp(config={"model": "m"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        scr._diff_service_for_preview = lambda: _FakeDiffService([".gitignore", "README.md"])
        scr._maybe_mount_diff_pending_cta()
        await pilot.pause()
        assert len(_cta_rows(scr)) == 1
        scr._maybe_mount_diff_pending_cta()          # второй отменённый ход
        await pilot.pause()
        assert len(_cta_rows(scr)) == 1, "CTA продублировался подряд"


@pytest.mark.asyncio
async def test_cta_returns_after_other_output():
    """Между ходами что-то вывелось — напоминание внизу снова уместно."""
    from gc_tui.app import TuiApp

    app = TuiApp(config={"model": "m"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        scr._diff_service_for_preview = lambda: _FakeDiffService([".gitignore"])
        scr._maybe_mount_diff_pending_cta()
        await pilot.pause()
        scr._mount_trace_row(prefix="", content="ответ агента",
                             kind_class="line-meta", focusable=False)
        await pilot.pause()
        scr._maybe_mount_diff_pending_cta()
        await pilot.pause()
        assert len(_cta_rows(scr)) == 2


@pytest.mark.asyncio
async def test_changed_diff_set_mounts_again():
    """Состав диффов изменился — это новая информация, показываем."""
    from gc_tui.app import TuiApp

    app = TuiApp(config={"model": "m"})
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        scr._diff_service_for_preview = lambda: _FakeDiffService([".gitignore", "README.md"])
        scr._maybe_mount_diff_pending_cta()
        await pilot.pause()
        scr._diff_service_for_preview = lambda: _FakeDiffService([".gitignore"])
        scr._maybe_mount_diff_pending_cta()
        await pilot.pause()
        assert len(_cta_rows(scr)) == 2


def test_flag_not_set_when_agent_produced_nothing():
    """Ход отменён, не начавшись: агент не выдал ни одного события — диффы измениться
    не могли, напоминать не о чем (правило B)."""
    from gc_tui.screens.main import TraceScreen

    scr = TraceScreen.__new__(TraceScreen)      # без монтажа: проверяем чистую логику
    scr._diff_pending_cta_pending = False
    scr._events_since_last_user_send = 0
    assert TraceScreen._should_arm_diff_cta(scr) is False


def test_flag_set_when_agent_produced_events():
    """Ход поработал и был прерван — мог что-то изменить, показываем."""
    from gc_tui.screens.main import TraceScreen

    scr = TraceScreen.__new__(TraceScreen)
    scr._diff_pending_cta_pending = False
    scr._events_since_last_user_send = 3
    assert TraceScreen._should_arm_diff_cta(scr) is True


class _IdleRunner:
    """Раннер, только что завершивший ход (is_running() уже False)."""
    def is_running(self):
        return False


def _write_empty_task(tmp_path, tid):
    """meta + пустой data-файл — минимальный тред для прямого вызова _poll_active."""
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8")
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_cta_shows_after_fast_turn_error_same_tick(tmp_path):
    """Ревью раунда фиксов 1: правило B взводится в начале _poll_active, а
    _events_since_last_user_send растёт в _render_records — внутри того же
    _poll_active она выполняется ПОЗЖЕ (хвост событий тика). Ход, целиком
    уложившийся в один интервал поллинга (провайдер ответил 401 почти
    мгновенно), на момент взвода видит счётчик = 0, хотя единственное
    событие хода вот-вот отрендерится в этом же тике. Идёт через настоящий
    _poll_active, не через предикат напрямую (иначе ошибка проходит мимо)."""
    pytest.importorskip("textual")
    from gc_core.message_policy.user_message_policy import (
        TECHNICAL_TYPE_ERROR_MESSAGE,
        wrap_technical_user_content,
    )
    from gc_tui.app import TuiApp

    tid = "cta0000000000000000000000000001a"
    _write_empty_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._diff_service_for_preview = lambda: _FakeDiffService([".gitignore"])
        assert scr._events_since_last_user_send == 0   # до тика — ничего не рендерилось
        # Хвост этого тика: единственное событие хода — быстрая ошибка провайдера.
        # Пишем ПОСЛЕ attach, чтобы попасть в tail, а не в начальный снимок.
        with open(tmp_path / f"data_cli_{tid}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "message", "thread_id": tid, "system": "cli", "seq": 1,
                "ts": "2026-08-03T10:00:00Z",
                "payload": {
                    "id": "e1", "role": "user",
                    "content": wrap_technical_user_content(
                        "401 Unauthorized", technical_type=TECHNICAL_TYPE_ERROR_MESSAGE,
                    ),
                },
            }, ensure_ascii=False) + "\n")
        # Один и тот же тик: спадающий фронт is_running (ход только закончился)
        # и рендер хвоста с ошибкой — оба внутри одного вызова _poll_active.
        scr._was_running = True
        pilot.app.active_runner = _IdleRunner()
        scr._poll_active()
        await pilot.pause()
        assert len(_cta_rows(scr)) == 1


@pytest.mark.asyncio
async def test_wipe_restore_shows_cta_at_zero_counter(tmp_path):
    """Ловушка фикса выше: наивный перенос правила B целиком в
    _flush_diff_pending_cta придушил бы и восстановление CTA после wipe
    (stream_reset) — тот взвод не про итог хода, а про физическую потерю
    строки при перерисовке трейса. При нулевом счётчике (хода не было
    вовсе) восстановление всё равно обязано смонтировать CTA."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cta0000000000000000000000000002a"
    _write_empty_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._diff_service_for_preview = lambda: _FakeDiffService([".gitignore"])
        assert scr._events_since_last_user_send == 0   # хода не было вовсе
        assert scr._was_running is False                 # не спадающий фронт хода
        # storage_epoch меняется, как после компакции, — tail_events увидит
        # stream_reset, трейс перерисуется с нуля.
        meta_path = tmp_path / f"meta_cli_{tid}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["last_summarized_at"] = "2026-08-03T10:00:00Z"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        scr._poll_active()
        await pilot.pause()
        assert len(_cta_rows(scr)) == 1
