"""Футер-разводка usage-метра: снапшот исполнителя → «запрос …/… · сессия …/…».

Проверяем: сегмент появляется рядом с моделью; TUI-кэш «запроса» держит итог
последнего хода, пока во время активного хода last_run_meta=None; смена сессии
сбрасывает кэш; без runner'а сегмента нет.
"""
import pytest

from usage.account import CATEGORY_MAIN, UsageAccount

_PRICING = {"enabled": True, "input": 1.0, "output": 2.0, "currency": "₽",
            "per_tokens": 1_000_000}


class _FakeRunner:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def usage_snapshot(self):
        return self.snapshot


def _meta(inp, out):
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, inp, out)
    return acc.to_meta()


@pytest.mark.asyncio
async def test_footer_shows_request_and_session(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._config = {"model": "m", "pricing": _PRICING}
        scr._active_task_id = "t1"
        app.active_runner = _FakeRunner((_meta(2_000_000, 0), _meta(1_000_000, 0)))

        right = scr._compose_footer_right().plain
        assert "запрос 1.0M/1.00₽" in right
        assert "сессия 2.0M/2.00₽" in right


@pytest.mark.asyncio
async def test_request_reflects_live_snapshot(tmp_path):
    """Футер показывает «запрос» как есть из снапшота (исполнитель сам считает
    live-дельту хода) — без TUI-заморозки прошлого хода."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._config = {"model": "m", "pricing": _PRICING}
        scr._active_task_id = "t1"

        # Ход в процессе: «запрос» = live-дельта, растёт вместе с сессией.
        app.active_runner = _FakeRunner((_meta(3_000_000, 0), _meta(1_000_000, 0)))
        right = scr._compose_footer_right().plain
        assert "запрос 1.0M/1.00₽" in right
        assert "сессия 3.0M/3.00₽" in right

        # Тик позже дельта выросла — футер обновляется без кэша.
        app.active_runner = _FakeRunner((_meta(3_500_000, 0), _meta(1_500_000, 0)))
        assert "запрос 1.5M/1.50₽" in scr._compose_footer_right().plain


@pytest.mark.asyncio
async def test_request_hidden_until_first_tokens(tmp_path):
    """Новый вопрос только начался (дельта ещё пустая) — «запрос» скрыт,
    видна только «сессия» (кумулятив прошлых ходов)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._config = {"model": "m", "pricing": _PRICING}
        scr._active_task_id = "t1"
        app.active_runner = _FakeRunner((_meta(2_000_000, 0), UsageAccount().to_meta()))
        right = scr._compose_footer_right().plain
        assert "запрос" not in right
        assert "сессия 2.0M/2.00₽" in right


@pytest.mark.asyncio
async def test_no_usage_segment_when_runner_lacks_method(tmp_path):
    """Раннер без usage_snapshot (старый/тестовый) не должен ронять футер."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    class _Bare:
        pass

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._config = {"model": "m", "pricing": _PRICING}
        scr._active_task_id = "t1"
        app.active_runner = _Bare()
        right = scr._compose_footer_right().plain      # не должно кинуть
        assert right == "m"


@pytest.mark.asyncio
async def test_no_usage_segment_without_runner(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._config = {"model": "m", "pricing": _PRICING}
        scr._active_task_id = "t1"
        app.active_runner = None
        right = scr._compose_footer_right().plain
        assert "запрос" not in right and "сессия" not in right


@pytest.mark.asyncio
async def test_footer_stacks_session_on_narrow_width(tmp_path):
    """Узкий экран: «запрос» и «сессия» уезжают на 2-ю строку вместе (команды
    слева не вытесняются, в 1-й строке нет дыры). Широкий: одна строка."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test(size=(40, 24)) as pilot:
        scr = pilot.app.screen
        app._config = {"model": "m", "pricing": _PRICING}
        scr._active_task_id = "t1"
        app.active_runner = _FakeRunner((_meta(2_000_000, 0), _meta(1_000_000, 0)))
        plain = scr._compose_footer_right().plain
        lines = plain.split("\n")
        assert len(lines) == 2                       # застекано
        assert "запрос" not in lines[0] and "сессия" not in lines[0]
        assert "запрос" in lines[1] and "сессия" in lines[1]


@pytest.mark.asyncio
async def test_footer_never_flush_against_left(tmp_path):
    """Граничная ширина: right-блок влезает ровно впритык к footer-left
    (хром футера съедает весь резерв) → должен уйти в стек, а не клеиться.
    W=67: left=25 (_FOOTER_LEFT), right=39, хром=3 → зазор был бы 0."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test(size=(67, 24)) as pilot:
        scr = pilot.app.screen
        app._config = {"model": "m", "pricing": _PRICING}
        scr._active_task_id = "t1"
        app.active_runner = _FakeRunner((_meta(2_000_000, 0), _meta(1_000_000, 0)))
        plain = scr._compose_footer_right().plain
        assert "\n" in plain                        # впритык → стек, не склейка


@pytest.mark.asyncio
async def test_footer_single_line_on_wide_width(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test(size=(200, 24)) as pilot:
        scr = pilot.app.screen
        app._config = {"model": "m", "pricing": _PRICING}
        scr._active_task_id = "t1"
        app.active_runner = _FakeRunner((_meta(2_000_000, 0), _meta(1_000_000, 0)))
        plain = scr._compose_footer_right().plain
        assert "\n" not in plain
        assert "запрос" in plain and "сессия" in plain


@pytest.mark.asyncio
async def test_footer_fact_currency_follows_endpoint(tmp_path):
    """Единственная проверка, что адрес эндпоинта реально доезжает из конфига
    TUI до расчёта: факт от .ru-провайдера показывается в рублях."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    def _billed_meta(inp, out, cost):
        acc = UsageAccount()
        acc.add(CATEGORY_MAIN, inp, out, 0, cost=cost, billed=True)
        return acc.to_meta()

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._config = {
            "model": "m",
            "pricing": _PRICING,
            "completions_url": "https://routerai.ru/api/v1/chat/completions",
        }
        scr._active_task_id = "t1"
        app.active_runner = _FakeRunner(
            (_billed_meta(2_000_000, 0, 20.0), _billed_meta(1_000_000, 0, 10.0))
        )

        right = scr._compose_footer_right().plain
        assert "₽" in right
        assert "$" not in right
