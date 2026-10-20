"""Правая часть футера не должна отбирать место у левой. Левая — две строки:
режим с хвостом команд (строка 1) и слот с бейджем уведомлений (строка 2);
правая колонка (#footer-right, width:auto) ЗАЯВЛЯЕТ потребность, левая (1fr)
получает остаток. Лесенка `_compose_footer_right` заявляет ровно столько, чтобы
строка 1 встала целиком, а бейдж — без обрезки. Поэтому сторожим не
промежуточный «бюджет», а ИТОГ: фактическую композицию правой рядом с
фактической левой."""

from gc_testkit.tui_support import tui_settings
import pytest
from rich.cells import cell_len

_CHROME = 5  # хром футера: padding'и + зазор между колонками (main.py)


def _need(scr) -> int:
    """Ширина, которую правая колонка заявляет по факту композиции."""
    return max(
        cell_len(line)
        for line in scr._compose_footer_right().plain.split("\n")
    )


def _left_room(scr) -> int:
    """Сколько колонок остаётся строкам левой рядом с правой."""
    return scr._footer_total_width() - _need(scr) - _CHROME


def _badge_cols(scr) -> int:
    from gc_tui.services.notices_ui import format_notices_badge

    return cell_len(format_notices_badge(scr._collect_footer_notices()))


@pytest.mark.asyncio
async def test_right_column_leaves_room_for_notices_badge():
    """Строка 1 левой целиком и бейдж уведомлений в строке 2 без обрезки —
    рядом с правой колонкой, которой при этом ничего не урезали зря."""
    from gc_core.startup_notices import Notice
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    app = TuiApp(
        settings=tui_settings({"agent.model": "anthropic/claude-opus-5"})
    )
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        assert isinstance(scr, TraceScreen)
        app.collect_footer_notices = lambda: [
            Notice("warning", f"c{i}", f"msg{i}") for i in range(3)
        ]
        await pilot.pause()
        room = _left_room(scr)
        assert cell_len(scr._footer_mode_line().plain) <= room
        assert _badge_cols(scr) <= room
        badge = scr._footer_frame().badge
        assert scr._footer_slot_line(badge).plain.startswith("⚠ 3")
        # Односторонние assert'ы выше не ловят излишне зажатую правую часть —
        # имя модели на этой ширине обязано быть целым.
        assert "anthropic/claude-opus-5" in scr._compose_footer_right().plain


@pytest.mark.asyncio
async def test_right_column_without_notices_is_unchanged():
    """Без уведомлений — та же проверка на голой левой части."""
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"agent.model": "m"}))
    async with app.run_test(size=(120, 30)) as pilot:
        scr = app.screen_stack[0]
        app.collect_footer_notices = list
        await pilot.pause()
        assert cell_len(scr._footer_mode_line().plain) <= _left_room(scr)


# --- Лесенка жертв: жертва обязана что-то давать, а стоп-клавиша неприкосновенна ---

_PRICING = {
    "enabled": True,
    "input": 1.0,
    "output": 2.0,
    "currency": "₽",
    "per_tokens": 1_000_000,
}


class _RunningRunner:
    """Раннер с ценами; `is_running` — чтобы левая колонка стала «Ctrl+C/stop»."""

    def __init__(self, snapshot, running=False):
        self.snapshot = snapshot
        self._running = running

    def usage_snapshot(self):
        return self.snapshot

    def is_running(self):
        return self._running


def _billed(inp, out, cost):
    """Смешанный счёт: часть провайдер выставил в долларах, часть считается по
    тарифу в рублях — сегмент несёт обе валюты и знак «~». Это и есть строка цен
    боевой ширины (51 колонка против 37 у одновалютной): именно она после правок
    develop стала диктовать ширину правой колонки."""
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount

    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, inp, out, 0, cost=cost, billed=True, currency="$")
    acc.add("summary", inp // 2, out, 0)
    return acc.to_meta()


def _with_prices(app, scr, *, running=False):
    app._settings = tui_settings(
        {
            "agent.model": "some-long-model-name",
            "pricing": _PRICING,
            "backend.completions_url": "https://routerai.ru/api/v1/chat/completions",
        }
    )
    app.tui_state.active_task_id = "t1"
    scr._active_task_id = "t1"
    app.active_runner = _RunningRunner(
        (_billed(2_000_000, 0, 20.0), _billed(1_000_000, 0, 10.0)),
        running=running,
    )


@pytest.mark.asyncio
async def test_profile_name_survives_when_dropping_it_frees_nothing(
    tmp_path, monkeypatch
):
    """Цены стоят на второй строке и шире шапки — значит ширину правой колонки
    диктуют ОНИ, и снятие имени профиля не освобождает ни колонки. Раньше лесенка
    всё равно доходила до последней ступени и отдавала имя вместе со стрелкой
    даром. Теперь ужатие шапки перебирается заново на каждом уровне цен, поэтому
    имя возвращается, как только жертва ценами освободила место."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from pathlib import Path as _P

    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(60, 30)) as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(
            app,
            "active_profile_ref",
            lambda: ProfileRef(name="work", path=_P("x"), root_kind="project"),
        )
        _with_prices(app, scr)
        await pilot.pause()

        right = scr._compose_footer_right().plain
        assert "work" in right, f"имя профиля снято даром: {right!r}"
        assert "some-long-model-name" in right  # модель не трогаем никогда


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [55, 60, 65, 70, 80])
async def test_stop_key_keeps_its_room_on_narrow_window(
    tmp_path, monkeypatch, width
):
    """Во время хода слева живёт «Ctrl+C/stop» — усечь её нечем, а другого способа
    остановить агента нет. Правая колонка обязана оставить ей место, пожертвовав
    ценами; раньше последняя ступень возвращалась безусловно и выдавливала левую
    в ноль колонок (замер: на 55-60 колонках слева не оставалось ничего)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _FOOTER_RUNNING_LEFT

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(width, 30)) as pilot:
        scr = pilot.app.screen
        _with_prices(app, scr, running=True)
        await pilot.pause()

        assert scr._footer_tail_commands() == _FOOTER_RUNNING_LEFT
        # пол левой — режим хотя бы глифом и стоп-клавиша за ним
        floor = scr._footer_mode_variants()[1].plain
        assert floor.endswith(_FOOTER_RUNNING_LEFT)
        left_width = _left_room(scr)
        assert left_width >= cell_len(floor), (
            f"ширина {width}: левой осталось {left_width} колонок, "
            f"а «{floor}» требует {cell_len(floor)}"
        )


def _work_profile(app, monkeypatch):
    from pathlib import Path as _P

    from gc_core.config_modules.profiles import ProfileRef

    monkeypatch.setattr(
        app,
        "active_profile_ref",
        lambda: ProfileRef(name="work", path=_P("x"), root_kind="project"),
    )


def _warnings(n):
    from gc_core.startup_notices import Notice

    return lambda: [Notice("warning", f"c{i}", f"m{i}") for i in range(n)]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [64, 80, 100, 140])
async def test_right_column_beside_two_row_left(tmp_path, monkeypatch, width):
    """На любой ширине правая колонка — не больше двух строк, строка 1 левой
    целиком (режим хотя бы глифом и стоп-клавиша), бейдж в строке 2 не
    режется. Цены жертвуются позже имени профиля."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _FOOTER_RUNNING_LEFT
    from textual.widgets import Static

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(width, 30)) as pilot:
        scr = pilot.app.screen
        _work_profile(app, monkeypatch)
        _with_prices(app, scr, running=True)
        monkeypatch.setattr(app, "collect_footer_notices", _warnings(3))
        scr._refresh_footer()
        await pilot.pause()

        right = scr._compose_footer_right().plain
        assert len(right.split("\n")) <= 2
        room = _left_room(scr)
        mode = scr.query_one("#footer-mode", Static)
        assert mode.content_size.width == room  # расчёт совпал с раскладкой
        line = scr._footer_mode_line().plain
        assert line.endswith(_FOOTER_RUNNING_LEFT)
        assert cell_len(line) <= room
        assert _badge_cols(scr) <= room
        if "сессия" not in right:
            assert "work" not in right, f"цены сняты раньше имени: {right!r}"


@pytest.mark.asyncio
async def test_notices_badge_does_not_push_prices_down(tmp_path, monkeypatch):
    """Бейдж живёт в строке 2, а не рядом с командами: правой хватает места
    «всё одной строкой», если строка 1 и бейдж встают каждая сама по себе —
    их сумма в один ряд не нужна."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _PROFILE_NAME_MAX_COLS, _RIGHT_ONE_LINE
    from gc_tui.services.usage_meter import compose_adaptive_footer_right

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        _work_profile(app, monkeypatch)
        _with_prices(app, scr)
        monkeypatch.setattr(app, "collect_footer_notices", _warnings(3))
        head = scr._footer_head(
            with_profile=True,
            profile_name_cols=_PROFILE_NAME_MAX_COLS,
            snapshot=scr._footer_head_snapshot(),
        )
        one_line = compose_adaptive_footer_right(
            head, *scr._usage_segments(), _RIGHT_ONE_LINE
        ).plain
        assert "\n" not in one_line
        full = cell_len(scr._footer_mode_variants()[0].plain)
        total = cell_len(one_line) + _CHROME + max(full, _badge_cols(scr))

        await pilot.resize_terminal(total, 30)
        assert scr._compose_footer_right().plain == one_line


def test_model_name_is_cut_in_the_middle():
    """Начало — провайдер, конец — версия: по ним модель и узнают."""
    pytest.importorskip("textual")
    from gc_tui.screens.main import _truncate_middle

    name = "gatewayhub/acme-lab/acme-big-coder-4.5"
    assert _truncate_middle(name, 22) == "gatewayhub/…-coder-4.5"
    assert _truncate_middle(name, len(name)) == name
    assert _truncate_middle(name, 10) == "gatew…-4.5"


#: Нейтральный длинный ID (38 знаков, как у реальных ID шлюзов).
_LONG_MODEL = "gatewayhub/acme-lab/acme-big-coder-4.5"
_METER = "ctx 42%"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("width", "name_kept"), [(65, True), (50, True), (40, False)]
)
async def test_long_model_id_yields_before_the_stop_key(
    tmp_path, monkeypatch, width, name_kept
):
    """Длинный ID модели в узком окне: «Ctrl+C/stop» цел, имя сначала
    ужато по середине, на самом узком снято целиком; плашка контекста
    остаётся всегда (спека §10.4, спека автовыбора §7.1)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _FOOTER_RUNNING_LEFT
    from rich.text import Text

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": _LONG_MODEL}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(width, 30)) as pilot:
        scr = pilot.app.screen
        app.tui_state.active_task_id = "t1"
        scr._active_task_id = "t1"
        app.active_runner = _RunningRunner(None, running=True)
        monkeypatch.setattr(scr, "_context_meter", lambda: Text(_METER))
        await pilot.pause()

        floor = scr._footer_mode_variants()[1].plain
        assert floor.endswith(_FOOTER_RUNNING_LEFT)
        left_width = _left_room(scr)
        assert left_width >= cell_len(floor), (
            f"ширина {width}: левой {left_width}, «{floor}» требует "
            f"{cell_len(floor)}"
        )
        top = scr._compose_footer_right().plain.split("\n")[0]
        assert top.endswith(f" · {_METER}")
        if not name_kept:
            assert top == f"● · {_METER}"
            return
        model = top.split("● ", 1)[1].split(" · ", 1)[0]
        head, cut, tail = model.partition("…")
        assert cut == "…", f"имя не ужато: {top!r}"
        assert head and tail
        assert _LONG_MODEL.startswith(head) and _LONG_MODEL.endswith(tail)
        assert cell_len(model) >= 10
