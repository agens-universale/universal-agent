"""Футер: активный профиль + цветовой индикатор уровня перед моделью (Task 8,
docs 2026-08-09-profiles-a-migration-plan).

Вид: `<профиль> ● <модель> · <контекст> · …` — жирная точка одновременно
разделитель и индикатор уровня (цвет = уровень); профиля нет → строка
начинается с точки. Цвет — ЕДИНЫЙ источник `tui.toggles.TOGGLES["level"]`.
При нехватке ширины имя профиля сначала усекается (потолок 14 колонок,
дальше — до минимума 5, Task 11 + Task 17), и только ниже минимума снимается
целиком — точка остаётся всегда."""

from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings
from pathlib import Path

import pytest

_LONG_NAME = "very-long-profile-name"  # 22 символа
_CEILING = _LONG_NAME[:13] + "…"  # потолок: 13 символов + «…» = 14 колонок
_FLOOR = _LONG_NAME[:4] + "…"  # минимум: 4 символа + «…» = 5 колонок


@pytest.mark.asyncio
async def test_footer_profile_name_truncated_to_ceiling_even_on_wide_screen(
    tmp_path, monkeypatch
):
    """Task 9: потолок (14 колонок) применяется ВСЕГДА, даже когда места с
    избытком — длинное имя профиля не должно отжирать футер целиком."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        ref = ProfileRef(name=_LONG_NAME, path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 1000)
        plain = scr._compose_footer_right().plain
        assert plain == f"{_CEILING} ● m"


@pytest.mark.asyncio
async def test_footer_profile_name_shrinks_toward_floor_under_width_deficit(
    tmp_path, monkeypatch
):
    """Task 11: потолок не влезает — имя ужимается по факту доступной ширины,
    здесь ровно до минимума (5 колонок, Task 17), а не снимается сразу целиком."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        ref = ProfileRef(name=_LONG_NAME, path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        # "● m" = 3 колонки; бюджет 9 = ровно минимум (5) + пробел (1) + "● m" (3):
        # потолок (14) сюда не влезает, минимум — впритык.
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 9)
        plain = scr._compose_footer_right().plain
        assert plain == f"{_FLOOR} ● m"


@pytest.mark.asyncio
async def test_footer_profile_name_dropped_entirely_below_floor_dot_stays(
    tmp_path, monkeypatch
):
    """Task 11: даже минимум (5 колонок, Task 17) не влезает — имя снимается
    целиком (прежнее поведение), точка-индикатор уровня остаётся на месте."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        ref = ProfileRef(name=_LONG_NAME, path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        # 8 = на 1 колонку меньше, чем нужно даже минимуму (9) — впритык под.
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 8)
        plain = scr._compose_footer_right().plain
        assert plain == "● m"
        assert "●" in plain


@pytest.mark.asyncio
async def test_footer_dot_present_across_truncation_ladder(
    tmp_path, monkeypatch
):
    """Task 9: точка-индикатор уровня присутствует на КАЖДОЙ ступени лесенки
    деградации имени профиля — потолок / сжатие / снятие целиком."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        ref = ProfileRef(name=_LONG_NAME, path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        for width in (1000, 18, 8, 7, 3):
            monkeypatch.setattr(
                scr, "_footer_right_available_width", lambda w=width: w
            )
            plain = scr._compose_footer_right().plain
            assert "●" in plain, (width, plain)


@pytest.mark.asyncio
async def test_footer_dot_present_without_active_profile(
    tmp_path, monkeypatch
):
    """Профиль не активен → строка начинается с точки-индикатора, не с имени."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        plain = scr._compose_footer_right().plain
        assert plain.startswith("● m")


@pytest.mark.asyncio
async def test_footer_shows_profile_name_before_dot_when_active(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        ref = ProfileRef(name="work", path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        plain = scr._compose_footer_right().plain
        assert plain.startswith("work ● m")


@pytest.mark.asyncio
async def test_footer_dot_color_is_the_level_of_the_model(
    tmp_path, monkeypatch
):
    """Цвет точки — уровень модели по `model_sign.LEVEL_COLORS`, не хардкод
    (light=INK, max=RUN_COLOR); модель без уровня — серая точка."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui import palette as p
    from gc_tui.app import TuiApp

    registry = {
        "models": {"fast": {}, "big": {}},
        "slots": {"light": "fast", "max": "big"},
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings(
            {"agent.model": "m", "model": "light", **registry}
        ),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        text = scr._compose_footer_right()
        assert text.plain.startswith("● fast")
        assert str(text.spans[0].style) == p.INK

        app.set_session_override("model", "max")
        text2 = scr._compose_footer_right()
        assert text2.plain.startswith("● big")
        assert str(text2.spans[0].style) == p.RUN_COLOR


@pytest.mark.asyncio
async def test_footer_drops_profile_name_first_on_narrow_width_keeps_dot(
    tmp_path, monkeypatch
):
    """Ширина не влезает даже без usage-сегментов → снимается ИМЯ ПРОФИЛЯ, точка
    остаётся (§ брифа: «первым снимается имя профиля»)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        ref = ProfileRef(
            name="very-long-profile-name", path=Path("x"), root_kind="project"
        )
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        # 8 = меньше минимума (9 = 5 колонок имени + пробел + «● m») — имя снимается.
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 8)
        plain = scr._compose_footer_right().plain
        assert plain == "● m"


@pytest.mark.asyncio
async def test_footer_keeps_profile_name_when_width_allows(
    tmp_path, monkeypatch
):
    """Контрольный случай к предыдущему тесту: широкий экран — имя профиля на
    месте, деградация не срабатывает зря."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        ref = ProfileRef(name="work", path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 200)
        plain = scr._compose_footer_right().plain
        assert plain == "work ● m"


@pytest.mark.asyncio
async def test_footer_stacks_usage_before_dropping_profile(
    tmp_path, monkeypatch
):
    """Task 17 (решение юзера ОТМЕНИЛО правило Task 8 «однострочность
    приоритетнее имени профиля»): при АКТИВНЫХ usage-сегментах («запрос ·
    сессия») на ширину, где одной строкой всё не влезает, ПЕРВЫМИ уезжают вниз
    ЦЕНЫ — вторая строка футера (height: 2) и так оплачена, перекладывание в
    неё ничего не теряет. Имя профиля остаётся на 1-й строке целиком: его
    снятие — потеря информации, ступень ниже. Ширина калибруется динамически:
    ровно достаточна для однострочного варианта БЕЗ профиля, но недостаточна —
    С профилем."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
    from gc_tui.app import TuiApp
    from gc_tui.services.usage_meter import compose_adaptive_footer_right
    from rich.cells import cell_len

    class _FakeRunner:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def usage_snapshot(self):
            return self.snapshot

    def _meta(inp, out):
        acc = UsageAccount()
        acc.add(CATEGORY_MAIN, inp, out)
        return acc.to_meta()

    pricing = {
        "enabled": True,
        "input": 1.0,
        "output": 2.0,
        "currency": "₽",
        "per_tokens": 1_000_000,
    }

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": pricing}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        ref = ProfileRef(
            name="very-long-profile-name", path=Path("x"), root_kind="project"
        )
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        app.active_runner = _FakeRunner(
            (_meta(2_000_000, 0), _meta(1_000_000, 0))
        )

        request_seg, session_seg = scr._usage_segments()
        assert (
            request_seg is not None and session_seg is not None
        )  # оба сегмента активны

        # available_width=10_000 → compose_adaptive_footer_right всегда отдаёт
        # однострочную склейку (порог заведомо не пройден) — считаем длины обоих
        # вариантов, чтобы не хардкодить конкретные числа символов.
        single_with_profile = compose_adaptive_footer_right(
            scr._footer_head(with_profile=True)[0],
            request_seg,
            session_seg,
            available_width=10_000,
        )
        single_without_profile = compose_adaptive_footer_right(
            scr._footer_head(with_profile=False)[0],
            request_seg,
            session_seg,
            available_width=10_000,
        )
        avail = cell_len(single_without_profile.plain)
        assert (
            cell_len(single_with_profile.plain) > avail
        )  # с профилем в этот бюджет не влезло бы

        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )
        plain = scr._compose_footer_right().plain
        first, second = plain.split("\n")  # цены уехали вниз
        assert first == f"{_CEILING} ● m"  # имя профиля цело (в потолке)
        assert "запрос" in second and "сессия" in second


# --- Выбор, ждущий ближайшего шага (спека model-knob §6.5) ------------------
#
# Было (Task 15): переход «сейчас → станет». Стало (раунд 3 смока): знак
# выбора с тусклым именем во второй строке под моделью хода, пока выбор окна
# не тот, что применил ран.
#
# Уровень подготовленного вызова живёт в `state._call_level`, а Router,
# резолвящий модель, зафиксирован при сборке executor'а («снимок» models/slots/
# assign — не живой `app.display_router()`, который отражает /profile
# мгновенно): смена профиля посреди хода не должна подменить «факт» на модель
# уже переключившегося профиля раньше, чем ход её реально увидит. Фейк-раннер
# ниже воспроизводит ровно это расхождение — старый Router у пина, новый (или
# просто с другим активным уровнем) у живого `display_router()`.


def _live(app, monkeypatch, router):
    """Выбор окна и витрина — от живого Router'а: его выбор и есть выбор,
    ждущий ближайшего шага, если ран применил другой. Router рана, как
    настоящий, читает выбор окна живым каналом (`live_choice`)."""
    monkeypatch.setattr(app, "display_router", lambda: router)
    monkeypatch.setattr(app, "_choice_provider", router.live_choice)
    runner = app.active_runner
    pinned_fn = getattr(runner, "executor_router", None)
    pinned = pinned_fn() if callable(pinned_fn) else None
    if pinned is not None:
        ran = pinned.with_choice_providers(
            pinned.active_target, router.live_choice
        )
        monkeypatch.setattr(runner, "executor_router", lambda: ran)


def _call_runner(level: str, router):
    class _FakeRunner:
        def is_running(self):
            return True

        def call_level(self):
            return level

        def applied_choice(self):
            return level

        def executor_router(self):
            return router

    return _FakeRunner()


def _call_runner_with_usage(level: str, router, usage_snapshot):
    """Как `_call_runner`, но и с активными usage-сегментами (запрос ·
    сессия) — нужно, чтобы вынудить usage-блок на 2-ю строку (Task 16)."""

    class _FakeRunner:
        def is_running(self):
            return True

        def call_level(self):
            return level

        def applied_choice(self):
            return level

        def executor_router(self):
            return router

        def usage_snapshot(self):
            return usage_snapshot

    return _FakeRunner()


@pytest.mark.asyncio
async def test_footer_shows_pending_choice_when_level_changes_mid_turn(
    tmp_path, monkeypatch
):
    """Смена уровня посреди хода: строка 1 — факт (пин), строка 2 — знак
    модели нового выбора под ним."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"minimax-m3": {}, "opus-4.8": {}},
            "slots": {"light": "minimax-m3", "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        _live(app, monkeypatch, live_router)

        plain = scr._compose_footer_right().plain
        assert plain == "● minimax-m3\n● opus-4.8  "


@pytest.mark.asyncio
async def test_footer_shows_new_profile_name_and_pending_choice_when_both_change(
    tmp_path, monkeypatch
):
    """Смена И профиля, И уровня посреди хода (спека Task 15, пример 3): слева —
    имя НОВОГО профиля (выбор, меняется мгновенно) + факт-модель старого пина,
    под ним — знак нового выбора. Комфортная ширина — ничего не ужато."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        old_snapshot = {
            "models": {"minimax-m3": {}},
            "slots": {"light": "minimax-m3"},
        }
        new_snapshot = {
            "models": {"opus-4.8": {}},
            "slots": {"max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(old_snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(new_snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        _live(app, monkeypatch, live_router)
        ref = ProfileRef(name="other", path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        plain = scr._compose_footer_right().plain
        assert plain == "other ● minimax-m3\n      ● opus-4.8  "


@pytest.mark.asyncio
async def test_footer_no_pending_when_only_profile_changes(
    tmp_path, monkeypatch
):
    """Смена профиля посреди хода, выбор тот же: ждать нечего — второй
    строки нет (сравниваются выборы, не модели), факт — модель Router'а
    у пина, снимка СТАРОГО профиля."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        old_snapshot = {
            "models": {"minimax-m3": {}},
            "slots": {"default": "minimax-m3"},
        }
        new_snapshot = {
            "models": {"gpt-5.5": {}},
            "slots": {"default": "gpt-5.5"},
        }
        executor_router = ModelRouter(
            make_settings(old_snapshot), choice_provider=lambda: "default"
        )
        live_router = ModelRouter(
            make_settings(new_snapshot), choice_provider=lambda: "default"
        )
        app.active_runner = _call_runner("default", executor_router)
        _live(app, monkeypatch, live_router)

        plain = scr._compose_footer_right().plain
        assert plain == "● minimax-m3"


@pytest.mark.asyncio
async def test_footer_no_arrow_when_future_model_matches_fact(
    tmp_path, monkeypatch
):
    """Будущая модель совпадает с фактической (тот же уровень, тот же
    профиль) — стрелки нет (спека Task 15)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"opus-4.8": {}},
            "slots": {"default": "opus-4.8"},
        }
        router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "default"
        )
        app.active_runner = _call_runner("default", router)
        monkeypatch.setattr(app, "display_router", lambda: router)

        plain = scr._compose_footer_right().plain
        assert plain == "● opus-4.8"
        assert "→" not in plain


@pytest.mark.asyncio
async def test_footer_no_arrow_when_turn_finished(tmp_path, monkeypatch):
    """Ход завершился (раннер больше не бежит) — прежний вид без стрелки, даже
    если раннер ещё не выброшен и последний уровень вызова доступен (спека Task 15)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"minimax-m3": {}, "opus-4.8": {}},
            "slots": {"light": "minimax-m3", "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )

        class _FinishedRunner:
            def is_running(self):
                return False

            def call_level(self):
                return "light"

            def executor_router(self):
                return executor_router

        app.active_runner = _FinishedRunner()
        _live(app, monkeypatch, live_router)

        plain = scr._compose_footer_right().plain
        assert plain == "● opus-4.8"
        assert "→" not in plain


@pytest.mark.asyncio
async def test_footer_drops_profile_name_before_model_names(
    tmp_path, monkeypatch
):
    """Порядок жертв по ширине: сначала снимается имя профиля целиком,
    ожидающий выбор с именем остаётся."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp
    from rich.cells import cell_len

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"minimax-m3": {}, "opus-4.8": {}},
            "slots": {"light": "minimax-m3", "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        _live(app, monkeypatch, live_router)
        ref = ProfileRef(
            name="very-long-profile-name", path=Path("x"), root_kind="project"
        )
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        no_profile_with_pending = "● minimax-m3\n● opus-4.8  "
        avail = cell_len("● minimax-m3")
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )

        plain = scr._compose_footer_right().plain
        assert plain == no_profile_with_pending


@pytest.mark.asyncio
async def test_footer_drops_names_keeps_dots_and_star_when_too_narrow(
    tmp_path, monkeypatch
):
    """Порядок жертв по ширине: профиль уже снят и всё равно не влезает —
    снимаются имена (модели хода и выбора), точки остаются."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp
    from rich.cells import cell_len

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"minimax-m3": {}, "opus-4.8": {}},
            "slots": {"light": "minimax-m3", "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        _live(app, monkeypatch, live_router)
        ref = ProfileRef(
            name="very-long-profile-name", path=Path("x"), root_kind="project"
        )
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        dots_only = "●\n●"
        avail = cell_len("● minimax-m3") - 1
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )

        plain = scr._compose_footer_right().plain
        assert plain == dots_only


# --- Task 16: лесенка деградации — двухстрочная раскладка не теряет всё ------
#
# Дефект найден на живом смоке: правило базы (`compose_adaptive_footer_right`)
# само уводит usage-блок «запрос · сессия» на 2-ю строку целиком, когда он не
# влезает в одну. В этот момент прежняя лесенка проваливала ВСЕ ступени (в
# каждом результате есть `\n`) и код безусловно отдавал `build(None,
# with_transition=False)` — самый бедный вариант, без профиля и без перехода,
# хотя на 1-й строке места хватало для обоих. Юзер также решил: блок
# выбора снимается ПОСЛЕДНИМ — позже, чем футер уходит на 2-ю строку; с
# model-knob §6.5 это имена, а точки остаются всегда.


@pytest.mark.asyncio
async def test_footer_two_line_layout_keeps_profile_and_pending(
    tmp_path, monkeypatch
):
    """Главный сценарий брифа (живой смок юзера): usage в строку 1 не
    влезает — профиль и модель хода остаются на 1-й, ожидающий выбор
    временно занимает место цен во 2-й (раунд 3 смока)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
    from gc_tui.app import TuiApp
    from gc_tui.services.usage_meter import compose_adaptive_footer_right
    from rich.cells import cell_len

    def _meta(inp, out):
        acc = UsageAccount()
        acc.add(CATEGORY_MAIN, inp, out)
        return acc.to_meta()

    pricing = {
        "enabled": True,
        "input": 1.0,
        "output": 2.0,
        "currency": "₽",
        "per_tokens": 1_000_000,
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": pricing}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"minimax-m3": {}, "opus-4.8": {}},
            "slots": {"light": "minimax-m3", "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner_with_usage(
            "light",
            executor_router,
            (_meta(2_000_000, 0), _meta(1_000_000, 0)),
        )
        _live(app, monkeypatch, live_router)
        ref = ProfileRef(name="other", path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        request_seg, session_seg = scr._usage_segments()
        assert request_seg is not None and session_seg is not None

        head_snapshot = scr._footer_head_snapshot()
        head = scr._footer_head(with_profile=True, snapshot=head_snapshot)[0]
        # available_width=0 форсирует перенос усложнённых сегментов на 2-ю
        # строку безусловно — так же, как это делает база при нехватке места.
        two_line = compose_adaptive_footer_right(
            head, request_seg, session_seg, available_width=0
        )
        avail = max(cell_len(line) for line in two_line.plain.split("\n"))
        single = compose_adaptive_footer_right(
            head, request_seg, session_seg, available_width=10_000
        )
        assert (
            cell_len(single.plain) > avail
        )  # одна строка при этой ширине не влезает

        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )
        plain = scr._compose_footer_right().plain
        assert "\n" in plain
        first_line, second_line = plain.split("\n")
        assert first_line.startswith("other ")
        assert "minimax-m3" in first_line and "opus-4.8" in second_line
        assert "запрос" not in plain and "сессия" not in plain
        assert cell_len(first_line) <= avail
        assert cell_len(second_line) <= avail


@pytest.mark.asyncio
async def test_footer_two_line_layout_drops_profile_before_names(
    tmp_path, monkeypatch
):
    """На двух строках профиль по-прежнему ужимается/снимается раньше
    имён модели и выбора — ожидающий выбор во второй строке остаётся с
    именем."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _PROFILE_NAME_MIN_COLS
    from rich.cells import cell_len

    def _meta(inp, out):
        acc = UsageAccount()
        acc.add(CATEGORY_MAIN, inp, out)
        return acc.to_meta()

    pricing = {
        "enabled": True,
        "input": 1.0,
        "output": 2.0,
        "currency": "₽",
        "per_tokens": 1_000_000,
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": pricing}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"minimax-m3": {}, "opus-4.8": {}},
            "slots": {"light": "minimax-m3", "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner_with_usage(
            "light",
            executor_router,
            (_meta(2_000_000, 0), _meta(1_000_000, 0)),
        )
        _live(app, monkeypatch, live_router)
        ref = ProfileRef(
            name="very-long-profile-name", path=Path("x"), root_kind="project"
        )
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        head_snapshot = scr._footer_head_snapshot()
        # Ширина — ровно на строку 1 без профиля: с профилем (даже ужатым)
        # она не влезает.
        avail = cell_len(
            scr._footer_head(with_profile=False, snapshot=head_snapshot)[
                0
            ].plain
        )
        with_profile = scr._footer_head(
            with_profile=True,
            profile_name_cols=_PROFILE_NAME_MIN_COLS,
            snapshot=head_snapshot,
        )[0]
        assert cell_len(with_profile.plain) > avail

        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )
        plain = scr._compose_footer_right().plain
        assert "\n" in plain
        first_line = plain.split("\n")[0]
        assert first_line.startswith("● minimax-m3")
        assert "● opus-4.8" in plain.split("\n")[1]


@pytest.mark.asyncio
async def test_footer_drops_names_last_when_two_line_still_too_narrow(
    tmp_path, monkeypatch
):
    """Юзер-контракт: блок выбора снимается ПОСЛЕДНИМ. Профиль уже снят и
    даже двухстрочный вариант с именами не влезает — имена снимаются,
    точки остаются."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.model_router import ModelRouter
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
    from gc_tui.app import TuiApp
    from rich.cells import cell_len

    def _meta(inp, out):
        acc = UsageAccount()
        acc.add(CATEGORY_MAIN, inp, out)
        return acc.to_meta()

    pricing = {
        "enabled": True,
        "input": 1.0,
        "output": 2.0,
        "currency": "₽",
        "per_tokens": 1_000_000,
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": pricing}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"minimax-m3": {}, "opus-4.8": {}},
            "slots": {"light": "minimax-m3", "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner_with_usage(
            "light",
            executor_router,
            (_meta(2_000_000, 0), _meta(1_000_000, 0)),
        )
        _live(app, monkeypatch, live_router)

        # На 1 колонку меньше, чем строка ожидающего выбора: с именами не
        # влезает 2-строчный вариант, куда бы ни делся usage-блок.
        avail = cell_len("● opus-4.8") - 1
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )

        plain = scr._compose_footer_right().plain
        assert plain == "●\n●"
        assert "minimax-m3" not in plain and "opus-4.8" not in plain


@pytest.mark.asyncio
async def test_footer_one_line_priority_kept_with_profile_pending_and_usage(
    tmp_path, monkeypatch
):
    """Регресс-страж Task 8/9: когда профиль, модель и usage-сегменты
    влезают в строку 1, цены остаются в ней, а ожидающий выбор — во
    второй строке (раунд 3 смока)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
    from gc_tui.app import TuiApp

    def _meta(inp, out):
        acc = UsageAccount()
        acc.add(CATEGORY_MAIN, inp, out)
        return acc.to_meta()

    pricing = {
        "enabled": True,
        "input": 1.0,
        "output": 2.0,
        "currency": "₽",
        "per_tokens": 1_000_000,
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": pricing}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {"minimax-m3": {}, "opus-4.8": {}},
            "slots": {"light": "minimax-m3", "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner_with_usage(
            "light",
            executor_router,
            (_meta(2_000_000, 0), _meta(1_000_000, 0)),
        )
        _live(app, monkeypatch, live_router)
        ref = ProfileRef(name="other", path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 1000)

        first, second = scr._compose_footer_right().plain.split("\n")
        assert first.startswith("other ") and "сессия" in first
        assert second.strip() == "● opus-4.8"


# --- Task 17: единая лесенка жертв -----------------------------------------
#
# Правая колонка (#footer-right, width: auto) ЗАЯВЛЯЕТ потребность, левая
# (#footer-left, 1fr) получает остаток — поэтому ступени лесенки различаются
# ровно тем, встаёт ли при этом остатке строка 1 левой (режим + хвост) и бейдж
# уведомлений. Ширину здесь не подсовывают: её задаёт настоящий размер
# терминала (`pilot.resize_terminal`), как в жизни, — иначе взаимодействие
# колонок не проверить.
#
# Порядок жертв: 1) всё одной строкой; 2) цены вниз; 3) пилюля режима — глиф;
# 4) имя профиля 14 → 5; 5) имя снимается; 6) снимаются имена модели и
# выбора (точки остаются).

_FOOTER_CHROME = 5  # padding'и футера + зазор между колонками (main.py)
_PRICING = {
    "enabled": True,
    "input": 1.0,
    "output": 2.0,
    "currency": "₽",
    "per_tokens": 1_000_000,
}
# Имя профиля видно, пока от него остаются хотя бы первые символы (минимум —
# 4 символа + «…»): по ним и опознаём «имя на месте» в переборе ширин.
_PROFILE_MIN_VISIBLE = 4


def _usage_meta(inp, out):
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount

    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, inp, out)
    return acc.to_meta()


def _left_room(total: int, right_plain: str) -> int:
    """Сколько колонок остаётся строкам левой, когда правая забрала своё."""
    from rich.cells import cell_len

    need = max(cell_len(line) for line in right_plain.split("\n"))
    return total - need - _FOOTER_CHROME


def _mode_line_cols(scr, *, glyph: bool = False) -> int:
    """Ширина строки 1 левой: пилюля полная или глифом + хвост команд."""
    from rich.cells import cell_len

    return cell_len(scr._footer_mode_variants()[int(glyph)].plain)


def _left_fits(scr, total: int, right_plain: str, *, glyph: bool) -> bool:
    """Встают ли строка 1 (в заданной форме пилюли) и бейдж уведомлений рядом
    с таким вариантом правой — то самое условие, по которому решает лесенка."""
    from gc_tui.services.notices_ui import format_notices_badge
    from rich.cells import cell_len

    badge = format_notices_badge(scr._collect_footer_notices())
    need = max(_mode_line_cols(scr, glyph=glyph), cell_len(badge), 1)
    return _left_room(total, right_plain) >= need


def _visible_profile_cols(right_plain: str) -> int:
    """Сколько колонок занимает ВИДИМОЕ имя профиля (0 — имя снято). Имя стоит
    в начале 1-й строки, до точки-индикатора уровня."""
    from rich.cells import cell_len

    head_line = right_plain.split("\n")[0]
    if head_line.startswith("●"):
        return 0
    return cell_len(head_line.split(" ●")[0])


def _narrowest_named_variant(scr, cols: int) -> str:
    """Самый узкий вариант правой колонки, где имя профиля ещё есть: голова с
    именем в `cols` колонок и цены внизу — последняя ступень перед снятием
    имени. Собирается тем же кодом, что и лесенка."""
    from gc_tui.services.usage_meter import compose_adaptive_footer_right

    head = scr._footer_head(
        with_profile=True,
        profile_name_cols=cols,
        snapshot=scr._footer_head_snapshot(),
    )[0]
    request_seg, session_seg = scr._usage_segments()
    if request_seg is None and session_seg is None:
        return head.plain
    return compose_adaptive_footer_right(
        head, request_seg, session_seg, 0
    ).plain


def _arm_pending(app, monkeypatch, *, profile=None, usage=None, choice="max"):
    """Пин хода «light», выбор `choice` («max» — ждёт, вторая строка;
    «light» — ожидания нет) + опционально usage-сегменты и активный
    профиль — общая обвязка ступенчатых тестов."""
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter

    snapshot = {
        "models": {"minimax-m3": {}, "opus-4.8": {}},
        "slots": {"light": "minimax-m3", "max": "opus-4.8"},
    }
    pinned = ModelRouter(
        make_settings(snapshot), choice_provider=lambda: "light"
    )
    live = ModelRouter(make_settings(snapshot), choice_provider=lambda: choice)
    app.active_runner = (
        _call_runner_with_usage("light", pinned, usage)
        if usage
        else _call_runner("light", pinned)
    )
    _live(app, monkeypatch, live)
    monkeypatch.setattr(app, "collect_footer_notices", list)
    if profile is not None:
        monkeypatch.setattr(
            app,
            "active_profile_ref",
            lambda: ProfileRef(
                name=profile, path=Path("x"), root_kind="project"
            ),
        )


@pytest.mark.asyncio
async def test_footer_moves_prices_down_where_profile_used_to_be_dropped(
    tmp_path, monkeypatch
):
    """Ступень 2 на живой ширине (дефект смока): раньше правой колонке считали
    бюджет как «всё, что осталось от левой», левая не отдавала ни колонки — и на
    этой ширине футер терял и имя профиля, и стрелку разом. Теперь вниз уезжают
    цены, а голова (профиль + модель) остаётся на 1-й строке целиком. Без
    ожидающего выбора: с ним вторая строка — его (тесты выше)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.services.usage_meter import compose_adaptive_footer_right
    from rich.cells import cell_len

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": _PRICING}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        _arm_pending(
            app,
            monkeypatch,
            profile=_LONG_NAME,
            usage=(_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
            choice="light",
        )

        request_seg, session_seg = scr._usage_segments()
        head = scr._footer_head(
            with_profile=True, snapshot=scr._footer_head_snapshot()
        )[0]
        stacked = compose_adaptive_footer_right(
            head, request_seg, session_seg, 0
        )
        need = max(cell_len(line) for line in stacked.plain.split("\n"))
        # Ширина: правой ровно на «цены вниз» с полным именем, левой — ровно
        # на строку 1 с полной пилюлей.
        total = need + _FOOTER_CHROME + _mode_line_cols(scr)

        await pilot.resize_terminal(total, 30)
        plain = scr._compose_footer_right().plain

        assert plain == stacked.plain
        first, second = plain.split("\n")
        assert first.startswith(_CEILING)  # имя профиля цело (в потолке)
        assert "● minimax-m3" in first  # и модель хода тоже
        assert "запрос" in second and "сессия" in second
        assert _left_fits(scr, total, plain, glyph=False)


@pytest.mark.asyncio
async def test_footer_turns_pill_to_glyph_before_shrinking_profile(
    tmp_path, monkeypatch
):
    """Ступень 3 ПЕРЕД ступенью 4: на ширине, где потолку имени не хватает
    ровно одной колонки рядом с полной пилюлей, пилюля режима сжимается до
    глифа, а имя профиля остаётся в потолке — сначала уступает левая форма,
    потом правое содержимое."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _PROFILE_NAME_MAX_COLS
    from rich.cells import cell_len

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        _arm_pending(app, monkeypatch, profile=_LONG_NAME)

        ceiling_need = cell_len(
            scr._footer_head(
                with_profile=True,
                profile_name_cols=_PROFILE_NAME_MAX_COLS,
                snapshot=scr._footer_head_snapshot(),
            )[0].plain
        )
        # Ровно одной колонки не хватает потолку имени рядом с полной пилюлей.
        total = ceiling_need + _FOOTER_CHROME + _mode_line_cols(scr) - 1

        await pilot.resize_terminal(total, 30)
        plain = scr._compose_footer_right().plain

        assert plain.startswith(_CEILING)  # имя целое, не ужато
        assert "● opus-4.8" in plain.split("\n")[1]
        assert not _left_fits(scr, total, plain, glyph=False)
        assert _left_fits(scr, total, plain, glyph=True)


@pytest.mark.asyncio
async def test_footer_keeps_full_profile_while_glyph_frees_room(
    tmp_path, monkeypatch
):
    """То же правило на ширине, где рядом с полной пилюлей не хватает даже
    самому узкому имени: имя всё равно остаётся в потолке — перебор имени
    живёт целиком во втором проходе, после того как пилюля стала глифом."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _PROFILE_NAME_MIN_COLS
    from rich.cells import cell_len

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        _arm_pending(app, monkeypatch, profile=_LONG_NAME)

        snapshot = scr._footer_head_snapshot()
        floor_need = cell_len(
            scr._footer_head(
                with_profile=True,
                profile_name_cols=_PROFILE_NAME_MIN_COLS,
                snapshot=snapshot,
            )[0].plain
        )
        # Полной пилюле не хватает одной колонки даже рядом с самым узким
        # вариантом правой с именем — значит, пилюля станет глифом.
        total = floor_need + _FOOTER_CHROME + _mode_line_cols(scr) - 1

        await pilot.resize_terminal(total, 30)
        plain = scr._compose_footer_right().plain

        assert plain.startswith(_CEILING)  # имя в потолке, не ужато
        assert "● opus-4.8" in plain.split("\n")[1]
        assert _left_fits(scr, total, plain, glyph=True)


@pytest.mark.asyncio
async def test_footer_shrinks_profile_on_the_glyph_pass(tmp_path, monkeypatch):
    """Имя профиля ужимается 14 → 5 — но ТОЛЬКО в платном проходе, когда
    бесплатные ходы кончились и пилюля режима слева уже сжата до глифа. Ширина
    здесь на одну колонку меньше, чем нужно потолку имени рядом с полом левой, —
    имя ужимается, но не снимается."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _PROFILE_NAME_MAX_COLS
    from rich.cells import cell_len

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        _arm_pending(app, monkeypatch, profile=_LONG_NAME)

        snapshot = scr._footer_head_snapshot()
        ceiling_need = cell_len(
            scr._footer_head(
                with_profile=True,
                profile_name_cols=_PROFILE_NAME_MAX_COLS,
                snapshot=snapshot,
            )[0].plain
        )
        # Пол левой: пилюля режима в один глиф (за ней в покое ничего нет).
        floor = _mode_line_cols(scr, glyph=True)
        # На одну колонку уже, чем нужно потолку имени рядом с полом левой.
        total = ceiling_need + _FOOTER_CHROME + floor - 1

        await pilot.resize_terminal(total, 30)
        plain = scr._compose_footer_right().plain

        assert plain.startswith(_LONG_NAME[:12] + "…")  # 13 колонок вместо 14
        first, second = plain.split("\n")
        assert "● opus-4.8" in second
        # левой досталось ровно столько, сколько занимает глиф режима
        assert total - cell_len(first) - _FOOTER_CHROME == floor


@pytest.mark.asyncio
async def test_footer_ladder_order_holds_across_terminal_widths(
    tmp_path, monkeypatch
):
    """Ступени 5 и 7 как порядок, а не как одна ширина: перебор реальных ширин
    терминала сверху вниз.

    - имя профиля снимается, только когда с ним не прошёл ни один вариант:
      даже с минимальным именем строка 1 с глифом не встаёт;
    - имя профиля при сужении только укорачивается, никогда не растёт
      (фикс-раунд 3: перебор имени целиком во втором проходе, и он тоже
      начинается с потолка);
    - имена модели и выбора снимаются последними: нет ширины, где они сняты,
      а имя профиля ещё на месте; строка ожидающего выбора не снимается
      никогда;
    - обе ступени действительно достигаются, иначе перебор ничего не стерёг.

    Проверяется РЕШЕНИЕ («вариант с именем не проходит предикат»), а не его
    следствие («левая в двух рядах»): следствие верно не всегда — сняв имя,
    правая отдаёт ширину обратно, и левая может вернуться в один ряд (ревью,
    решение контроллера: это приемлемо и запирать не нужно)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import _PROFILE_NAME_MIN_COLS

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        _arm_pending(app, monkeypatch, profile=_LONG_NAME)

        seen_profile_dropped = False
        seen_names_dropped = False
        prev_profile_cols = None
        for total in range(115, 14, -1):
            await pilot.resize_terminal(total, 30)
            plain = scr._compose_footer_right().plain
            has_profile = plain.startswith(_LONG_NAME[:_PROFILE_MIN_VISIBLE])
            profile_cols = _visible_profile_cols(plain)
            if prev_profile_cols is not None:
                assert profile_cols <= prev_profile_cols, (
                    f"ширина {total}: имя профиля выросло с {prev_profile_cols} "
                    f"до {profile_cols} колонок при сужении — {plain!r}"
                )
            prev_profile_cols = profile_cols
            if not has_profile:
                seen_profile_dropped = True
                floor = _narrowest_named_variant(scr, _PROFILE_NAME_MIN_COLS)
                assert not _left_fits(scr, total, floor, glyph=True), (
                    f"ширина {total}: имя снято, хотя с минимальным "
                    f"именем строка 1 вставала — {plain!r}"
                )
            assert plain.count("\n") == 1, (total, plain)
            if "opus-4.8" not in plain:
                seen_names_dropped = True
                assert not has_profile, (
                    f"ширина {total}: имена сняты раньше имени профиля — {plain!r}"
                )
        assert seen_profile_dropped and seen_names_dropped


@pytest.mark.asyncio
async def test_footer_ladder_is_monotone_prices_never_come_back_up(
    tmp_path, monkeypatch
):
    """Лесенка монотонна по ширине: при сужении окна состояние только
    ухудшается.

    Цены уезжают вниз ПЕРВЫМИ и остаются внизу. Обратный прыжок возможен ровно
    на границе ступени 3: пилюля стала глифом, места правой стало больше — и
    если вариант «всё одной строкой» пробуется во втором проходе, цены
    возвращаются наверх. Это то самое дёрганье, из-за которого задача заведена.

    Имя профиля монотонно вместе с ними (фикс-раунд 3): в первом проходе оно
    всегда в потолке, во втором перебор начинается тоже с потолка — «подрасти»
    при сужении ему больше негде.

    Раскладка подобрана так, чтобы прыжок был ДОСТИЖИМ: короткая модель и
    отсутствие перехода делают однострочный вариант достаточно узким, чтобы он
    влез рядом с глифом (с длинной головой он не влезает никогда, и
    сторож молчал бы вхолостую)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.profiles import ProfileRef
    from gc_tui.app import TuiApp

    class _UsageOnlyRunner:
        """Только usage-сегменты: хода нет — значит, нет и блока за стрелкой."""

        def is_running(self):
            return False

        def usage_snapshot(self):
            return (_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0))

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": _PRICING}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(app, "collect_footer_notices", list)
        monkeypatch.setattr(
            app,
            "active_profile_ref",
            lambda: ProfileRef(
                name=_LONG_NAME, path=Path("x"), root_kind="project"
            ),
        )
        app.active_runner = _UsageOnlyRunner()

        stacked_at = None
        prev_profile_cols = None
        for total in range(160, 59, -1):
            await pilot.resize_terminal(total, 30)
            plain = scr._compose_footer_right().plain
            if "\n" in plain:
                stacked_at = total if stacked_at is None else stacked_at
            elif stacked_at is not None:
                raise AssertionError(
                    f"ширина {total}: цены вернулись на 1-ю строку после того, "
                    f"как уехали вниз на {stacked_at} — {plain!r}"
                )
            profile_cols = _visible_profile_cols(plain)
            if prev_profile_cols is not None:
                assert profile_cols <= prev_profile_cols, (
                    f"ширина {total}: имя профиля выросло с {prev_profile_cols} "
                    f"до {profile_cols} колонок при сужении — {plain!r}"
                )
            prev_profile_cols = profile_cols
        assert stacked_at is not None, (
            "цены так и не уехали вниз — перебор ничего не стерёг"
        )


@pytest.mark.asyncio
async def test_footer_context_meter_stays_on_the_first_line_at_every_step(
    tmp_path, monkeypatch
):
    """Инвариант: плашка контекста ВСЕГДА на 1-й строке правой колонки — не
    уезжает вниз вместе с ценами и не снимается ни на одной ступени."""
    pytest.importorskip("textual")
    import json

    from gc_tui.app import TuiApp

    tid = "ctx0000000000000000000000000017a"
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "runtime": {"live_history_chars": 500, "live_msg_count": 1},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "m",
                "pricing": _PRICING,
                "backend.api_mode": "chat_completions",
                "summary": {"chat_completions_summary_trigger_chars": 1000},
            }
        ),
    )
    async with app.run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        _arm_pending(
            app,
            monkeypatch,
            profile=_LONG_NAME,
            usage=(_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
        )
        assert scr._context_meter() is not None

        for total in (200, 140, 120, 110, 100, 90, 80, 70, 60):
            await pilot.resize_terminal(total, 30)
            first = scr._compose_footer_right().plain.split("\n")[0]
            assert "контекст" in first, (total, first)
            assert "●" in first  # точка модели хода — там же


@pytest.mark.asyncio
async def test_footer_resolves_context_meter_once_per_frame(
    tmp_path, monkeypatch
):
    """Перф-контракт кадра (тот же, что у `_footer_head_snapshot`, cfccade9):
    плашка контекста читает meta треда С ДИСКА, а лесенка строит голову до 22
    раз за кадр — значит, метр снимается ОДИН раз и передаётся готовым.

    Ширина здесь заведомо узкая: перебор проходит все ступени, а не выходит на
    первой."""
    pytest.importorskip("textual")
    import json

    from gc_tui.app import TuiApp

    tid = "ctx0000000000000000000000000017b"
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "runtime": {"live_history_chars": 500, "live_msg_count": 1},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "m",
                "pricing": _PRICING,
                "backend.api_mode": "chat_completions",
                "summary": {"chat_completions_summary_trigger_chars": 1000},
            }
        ),
    )
    async with app.run_test(size=(70, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        _arm_pending(
            app,
            monkeypatch,
            profile=_LONG_NAME,
            usage=(_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
        )

        calls = []
        original = scr._context_meter

        def _counted():
            calls.append(1)
            return original()

        monkeypatch.setattr(scr, "_context_meter", _counted)
        plain = scr._compose_footer_right().plain
        assert "контекст" in plain  # метр действительно участвовал
        assert len(calls) == 1, (
            f"метр контекста резолвился {len(calls)} раз(а) за кадр"
        )


def _running_runner(router, usage_snapshot=None):
    """Раннер бегущего хода: Router, зафиксированный при сборке executor'а
    (не живой `display_router()`)."""

    class _FakeRunner:
        def is_running(self):
            return True

        def executor_router(self):
            return router

        def usage_snapshot(self):
            return usage_snapshot

    return _FakeRunner()


@pytest.mark.asyncio
async def test_footer_running_turn_follows_live_level_through_executor_router(
    tmp_path, monkeypatch
):
    """Без ручного пина футер следует уровню Router текущего executor."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp

    snapshot = {
        "models": {"minimax-m3": {}, "opus-4.8": {}},
        "slots": {"light": "minimax-m3", "max": "opus-4.8"},
    }
    level = {"value": "light"}
    executor = ModelRouter(
        make_settings(snapshot), choice_provider=lambda: level["value"]
    )
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    app.active_runner = _running_runner(executor)
    monkeypatch.setattr(app, "_choice_provider", lambda: level["value"])
    monkeypatch.setattr(app, "display_router", lambda: executor)
    monkeypatch.setattr(app, "collect_footer_notices", lambda: [])
    async with app.run_test(size=(120, 24)) as pilot:
        scr = app.screen
        assert "minimax-m3" in scr._compose_footer_right().plain
        assert "→" not in scr._compose_footer_right().plain
        level["value"] = "max"
        assert "opus-4.8" in scr._compose_footer_right().plain
        assert "→" not in scr._compose_footer_right().plain


@pytest.mark.asyncio
async def test_footer_running_turn_keeps_executor_model_when_profile_switches(
    tmp_path, monkeypatch
):
    """Смена профиля посреди хода: имя модели — из Router'а executor'а (снимок
    старого профиля), живая витрина нового профиля его не подменяет."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_core.model_router import ModelRouter

    old = ModelRouter(
        make_settings(
            {"models": {"minimax-m3": {}}, "slots": {"light": "minimax-m3"}}
        ),
        choice_provider=lambda: "light",
    )
    new = ModelRouter(
        make_settings(
            {"models": {"other-model": {}}, "slots": {"light": "other-model"}}
        ),
        choice_provider=lambda: "light",
    )
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    app.active_runner = _running_runner(old)
    monkeypatch.setattr(app, "_choice_provider", lambda: "light")
    monkeypatch.setattr(app, "display_router", lambda: new)
    monkeypatch.setattr(app, "collect_footer_notices", lambda: [])
    async with app.run_test(size=(120, 24)) as pilot:
        plain = app.screen._compose_footer_right().plain
        assert "minimax-m3" in plain and "other-model" not in plain


@pytest.mark.asyncio
async def test_footer_idle_uses_display_router(tmp_path, monkeypatch):
    """Хода нет — футер показывает живой резолв витрины."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_core.model_router import ModelRouter

    live = ModelRouter(
        make_settings(
            {"models": {"other-model": {}}, "slots": {"light": "other-model"}}
        ),
        choice_provider=lambda: "light",
    )
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    app.active_runner = None
    monkeypatch.setattr(app, "display_router", lambda: live)
    monkeypatch.setattr(app, "collect_footer_notices", lambda: [])
    async with app.run_test(size=(120, 24)) as pilot:
        assert "other-model" in app.screen._compose_footer_right().plain

    # Раннер есть (не обнулён после стопа хода), но is_running() -> False:
    # футер обязан взять живую витрину (display_router), а не Router раннера —
    # иначе после стопа футер продолжал бы показывать модель прошлого хода.
    stale = ModelRouter(
        make_settings(
            {"models": {"stale-model": {}}, "slots": {"light": "stale-model"}}
        ),
        choice_provider=lambda: "light",
    )
    app2 = TuiApp(
        threads_dir=str(tmp_path / "t2"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app2._config_path = str(tmp_path / "config.yml")
    app2.active_runner = _running_runner(stale)
    app2.active_runner.is_running = lambda: False
    monkeypatch.setattr(app2, "display_router", lambda: live)
    monkeypatch.setattr(app2, "collect_footer_notices", lambda: [])
    async with app2.run_test(size=(120, 24)) as pilot2:
        plain = app2.screen._compose_footer_right().plain
        assert "other-model" in plain
        assert "stale-model" not in plain


@pytest.mark.asyncio
async def test_footer_drops_model_name_after_profile_but_keeps_dot(
    tmp_path, monkeypatch
):
    """Лесенка (спека §7.1): после имени профиля снимается имя модели, точка остаётся."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    _arm_unpinned_running(app, monkeypatch, profile="work")
    async with app.run_test(size=(120, 24)) as pilot:
        scr = app.screen
        wide = scr._compose_footer_right().plain
        assert "work" in wide and "minimax-m3" in wide
        # сузить до ширины, где имя профиля уже снято, а имя модели ещё нет
        # (подобрать по существующему образцу `_narrowest_named_variant`), затем ещё уже:
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 4)
        narrow = scr._compose_footer_right().plain
        assert "●" in narrow
        assert "minimax-m3" not in narrow and "work" not in narrow


def _arm_unpinned_running(app, monkeypatch, *, profile=None, usage=None):
    """Бегущий ход на executor-Router (уровень «light» → minimax-m3), живая
    витрина — на другом уровне («max») + опционально usage-сегменты и активный
    профиль — общая обвязка ступенчатых тестов."""
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter

    snapshot = {
        "models": {"minimax-m3": {}, "opus-4.8": {}},
        "slots": {"light": "minimax-m3", "max": "opus-4.8"},
    }
    executor = ModelRouter(
        make_settings(snapshot), choice_provider=lambda: "light"
    )
    live = ModelRouter(make_settings(snapshot), choice_provider=lambda: "max")
    app.active_runner = _running_runner(executor, usage)
    monkeypatch.setattr(app, "display_router", lambda: live)
    monkeypatch.setattr(app, "collect_footer_notices", list)
    if profile is not None:
        monkeypatch.setattr(
            app,
            "active_profile_ref",
            lambda: ProfileRef(
                name=profile, path=Path("x"), root_kind="project"
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("prices_down", [False, True])
async def test_footer_auto_fact_keeps_profile_and_usage_on_both_layouts(
    tmp_path, monkeypatch, prices_down
):
    """Перенос цен не теряет профиль и факт auto; широкий футер остаётся однострочным."""
    from gc_tui.app import TuiApp
    from gc_tui.services.usage_meter import compose_adaptive_footer_right
    from rich.cells import cell_len

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": _PRICING}),
    )
    app._config_path = str(tmp_path / "config.yml")
    _arm_unpinned_running(
        app,
        monkeypatch,
        profile="other",
        usage=(_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
    )
    app.tui_state.active_task_id = "t1"
    monkeypatch.setattr(app, "auto_mode_active", lambda: True)
    async with app.run_test(size=(200, 30)):
        scr = app.screen
        head = scr._footer_head(with_profile=True)[0]
        request, session = scr._usage_segments()
        expected = compose_adaptive_footer_right(
            head,
            request,
            session,
            available_width=0 if prices_down else 10_000,
        )
        width = max(cell_len(line) for line in expected.plain.split("\n"))
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: width
        )
        plain = scr._compose_footer_right().plain
        assert plain.startswith("other ● → ● minimax-m3")
        assert "запрос" in plain and "сессия" in plain
        assert ("\n" in plain) is prices_down


@pytest.mark.asyncio
async def test_footer_two_line_auto_drops_profile_before_fact_and_fact_last(
    tmp_path, monkeypatch
):
    """При дефиците ширины профиль и имя модели уходят раньше стрелки факта auto."""
    from gc_tui.app import TuiApp
    from gc_tui.services.usage_meter import compose_adaptive_footer_right
    from rich.cells import cell_len

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "pricing": _PRICING}),
    )
    app._config_path = str(tmp_path / "config.yml")
    _arm_unpinned_running(
        app,
        monkeypatch,
        profile="very-long-profile-name",
        usage=(_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
    )
    from gc_core.model_router import ModelRouter

    model = "very-long-running-model-name-that-exceeds-usage-width"
    app.active_runner = _running_runner(
        ModelRouter(
            make_settings(
                {"models": {model: {}}, "slots": {"default": model}}
            ),
            choice_provider=lambda: "default",
        ),
        (_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
    )
    app.tui_state.active_task_id = "t1"
    monkeypatch.setattr(app, "auto_mode_active", lambda: True)
    async with app.run_test(size=(200, 30)):
        scr = app.screen
        request, session = scr._usage_segments()
        no_profile = compose_adaptive_footer_right(
            scr._footer_head(with_profile=False)[0],
            request,
            session,
            available_width=0,
        )
        width = max(cell_len(line) for line in no_profile.plain.split("\n"))
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: width
        )
        plain = scr._compose_footer_right().plain
        assert "\n" in plain
        assert plain.startswith(f"● → ● {model}")
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 4)
        plain = scr._compose_footer_right().plain
        assert plain.split("\n")[0] == "●"


@pytest.mark.asyncio
async def test_narrow_pending_drops_both_names_keeps_dots_and_star(
    tmp_path, monkeypatch
):
    """Узкое окно при ожидающем выборе (спека model-knob §6.5): сначала
    имена — и длинной модели хода, и выбора; точки остаются."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_core.model_router import ModelRouter
    from gc_tui.app import TuiApp

    long_id = "gatewayhub/acme-lab/acme-big-coder-4.5"
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        snapshot = {
            "models": {long_id: {}, "opus-4.8": {}},
            "slots": {"light": long_id, "max": "opus-4.8"},
        }
        executor_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), choice_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        _live(app, monkeypatch, live_router)
        monkeypatch.setattr(scr, "_context_meter", lambda: None)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 8)

        assert scr._compose_footer_right().plain == "●\n●"
