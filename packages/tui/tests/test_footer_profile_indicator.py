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
async def test_footer_dot_color_matches_active_level_from_toggles(
    tmp_path, monkeypatch
):
    """Цвет точки — ИЗ TOGGLES["level"], не хардкод (light=INK, max=ACCENT)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui import palette as p
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "m", "level": "light"}),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        text = scr._compose_footer_right()
        styles = [
            str(s.style) for s in text.spans if "bold" in str(s.style).lower()
        ]
        assert any(p.INK.lower() in st.lower() for st in styles), styles

        app.set_session_override("level", "max")
        text2 = scr._compose_footer_right()
        styles2 = [
            str(s.style) for s in text2.spans if "bold" in str(s.style).lower()
        ]
        assert any(p.ACCENT.lower() in st.lower() for st in styles2), styles2


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
            scr._footer_head(with_profile=True),
            request_seg,
            session_seg,
            available_width=10_000,
        )
        single_without_profile = compose_adaptive_footer_right(
            scr._footer_head(with_profile=False),
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


# --- Task 15: переход «сейчас → станет» посреди хода -------------------------
#
# Уровень подготовленного вызова живёт в `state._call_level`, а Router,
# резолвящий модель, зафиксирован при сборке executor'а («снимок» models/slots/
# assign — не живой `app.display_router()`, который отражает /profiles
# мгновенно): смена профиля посреди хода не должна подменить «факт» на модель
# уже переключившегося профиля раньше, чем ход её реально увидит. Фейк-раннер
# ниже воспроизводит ровно это расхождение — старый Router у пина, новый (или
# просто с другим активным уровнем) у живого `display_router()`.


def _call_runner(level: str, router):
    class _FakeRunner:
        def is_running(self):
            return True

        def call_level(self):
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

        def executor_router(self):
            return router

        def usage_snapshot(self):
            return usage_snapshot

    return _FakeRunner()


@pytest.mark.asyncio
async def test_footer_shows_transition_with_dot_when_level_changes_mid_turn(
    tmp_path, monkeypatch
):
    """Смена уровня посреди хода: слева — факт (пин), справа — точка НОВОГО
    уровня + новая модель (спека Task 15, пример 1)."""
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
            make_settings(snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), level_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        monkeypatch.setattr(app, "display_router", lambda: live_router)

        plain = scr._compose_footer_right().plain
        assert plain == "● minimax-m3 → ● opus-4.8"


@pytest.mark.asyncio
async def test_footer_shows_new_profile_name_and_dotted_transition_when_both_change(
    tmp_path, monkeypatch
):
    """Смена И профиля, И уровня посреди хода (спека Task 15, пример 3): слева —
    имя НОВОГО профиля (выбор, меняется мгновенно) + факт-модель старого пина,
    справа — точка нового уровня + новая модель. Комфортная ширина — ничего не
    ужато, композиция «профиль + переход с точкой» видна целиком в одной строке."""
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
            make_settings(old_snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(new_snapshot), level_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        monkeypatch.setattr(app, "display_router", lambda: live_router)
        ref = ProfileRef(name="other", path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        plain = scr._compose_footer_right().plain
        assert plain == "other ● minimax-m3 → ● opus-4.8"
        assert plain.startswith("other ")  # новое имя профиля слева
        assert "minimax-m3" in plain and "opus-4.8" in plain
        assert "→ ●" in plain  # точка перехода справа


@pytest.mark.asyncio
async def test_footer_shows_transition_without_dot_when_only_profile_changes(
    tmp_path, monkeypatch
):
    """Смена профиля посреди хода, уровень тот же: справа — модель БЕЗ точки
    (спека Task 15, пример 2) — Router у пина остаётся снимком СТАРОГО
    профиля, живой Router уже видит новый."""
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
            make_settings(old_snapshot), level_provider=lambda: "default"
        )
        live_router = ModelRouter(
            make_settings(new_snapshot), level_provider=lambda: "default"
        )
        app.active_runner = _call_runner("default", executor_router)
        monkeypatch.setattr(app, "display_router", lambda: live_router)

        plain = scr._compose_footer_right().plain
        assert plain == "● minimax-m3 → gpt-5.5"


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
            make_settings(snapshot), level_provider=lambda: "default"
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
            make_settings(snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), level_provider=lambda: "max"
        )

        class _FinishedRunner:
            def is_running(self):
                return False

            def call_level(self):
                return "light"

            def executor_router(self):
                return executor_router

        app.active_runner = _FinishedRunner()
        monkeypatch.setattr(app, "display_router", lambda: live_router)

        plain = scr._compose_footer_right().plain
        assert plain == "● opus-4.8"
        assert "→" not in plain


@pytest.mark.asyncio
async def test_footer_drops_profile_name_before_dropping_transition_block(
    tmp_path, monkeypatch
):
    """Порядок жертв по ширине (спека Task 15): сначала снимается имя профиля
    целиком, переход остаётся."""
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
            make_settings(snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), level_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        monkeypatch.setattr(app, "display_router", lambda: live_router)
        ref = ProfileRef(
            name="very-long-profile-name", path=Path("x"), root_kind="project"
        )
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        no_profile_with_transition = "● minimax-m3 → ● opus-4.8"
        avail = cell_len(no_profile_with_transition)
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )

        plain = scr._compose_footer_right().plain
        assert plain == no_profile_with_transition


@pytest.mark.asyncio
async def test_footer_drops_transition_block_when_still_too_narrow_without_profile(
    tmp_path, monkeypatch
):
    """Порядок жертв по ширине (спека Task 15): профиль уже снят и всё равно не
    влезает — снимается ВЕСЬ блок за стрелкой, факт (точка + модель) остаётся."""
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
            make_settings(snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), level_provider=lambda: "max"
        )
        app.active_runner = _call_runner("light", executor_router)
        monkeypatch.setattr(app, "display_router", lambda: live_router)
        ref = ProfileRef(
            name="very-long-profile-name", path=Path("x"), root_kind="project"
        )
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        fact_only = "● minimax-m3"
        avail = cell_len(fact_only)
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )

        plain = scr._compose_footer_right().plain
        assert plain == fact_only
        assert "→" not in plain


# --- Task 16: лесенка деградации — двухстрочная раскладка не теряет всё ------
#
# Дефект найден на живом смоке: правило базы (`compose_adaptive_footer_right`)
# само уводит usage-блок «запрос · сессия» на 2-ю строку целиком, когда он не
# влезает в одну. В этот момент прежняя лесенка проваливала ВСЕ ступени (в
# каждом результате есть `\n`) и код безусловно отдавал `build(None,
# with_transition=False)` — самый бедный вариант, без профиля и без перехода,
# хотя на 1-й строке места хватало для обоих. Юзер также решил: стрелка
# перехода снимается ПОСЛЕДНЕЙ — позже, чем футер уходит на 2-ю строку.


@pytest.mark.asyncio
async def test_footer_two_line_layout_keeps_profile_and_transition(
    tmp_path, monkeypatch
):
    """Главный сценарий брифа (живой смок юзера): usage уезжает на 2-ю строку,
    профиль и переход остаются на 1-й — раньше здесь терялось всё."""
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
            make_settings(snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), level_provider=lambda: "max"
        )
        app.active_runner = _call_runner_with_usage(
            "light", executor_router, (_meta(2_000_000, 0), _meta(1_000_000, 0))
        )
        monkeypatch.setattr(app, "display_router", lambda: live_router)
        ref = ProfileRef(name="other", path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        request_seg, session_seg = scr._usage_segments()
        assert request_seg is not None and session_seg is not None

        head_snapshot = scr._footer_head_snapshot()
        head = scr._footer_head(with_profile=True, snapshot=head_snapshot)
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
        assert "→" in first_line
        assert "minimax-m3" in first_line and "opus-4.8" in first_line
        assert cell_len(first_line) <= avail
        assert cell_len(second_line) <= avail


@pytest.mark.asyncio
async def test_footer_two_line_layout_drops_profile_before_transition(
    tmp_path, monkeypatch
):
    """На двух строках профиль по-прежнему ужимается/снимается раньше
    перехода (порядок жертв Task 15 не меняется) — стрелка остаётся."""
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
            make_settings(snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), level_provider=lambda: "max"
        )
        app.active_runner = _call_runner_with_usage(
            "light", executor_router, (_meta(2_000_000, 0), _meta(1_000_000, 0))
        )
        monkeypatch.setattr(app, "display_router", lambda: live_router)
        ref = ProfileRef(
            name="very-long-profile-name", path=Path("x"), root_kind="project"
        )
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)

        request_seg, session_seg = scr._usage_segments()
        head_snapshot = scr._footer_head_snapshot()

        head_no_profile = scr._footer_head(
            with_profile=False, snapshot=head_snapshot
        )
        two_line_no_profile = compose_adaptive_footer_right(
            head_no_profile, request_seg, session_seg, available_width=0
        )
        avail = max(
            cell_len(line) for line in two_line_no_profile.plain.split("\n")
        )

        head_with_profile = scr._footer_head(
            with_profile=True, snapshot=head_snapshot
        )
        two_line_with_profile = compose_adaptive_footer_right(
            head_with_profile, request_seg, session_seg, available_width=0
        )
        assert (
            max(
                cell_len(line)
                for line in two_line_with_profile.plain.split("\n")
            )
            > avail
        )

        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )
        plain = scr._compose_footer_right().plain
        assert "\n" in plain
        first_line = plain.split("\n")[0]
        assert "very-long-profile-name" not in first_line
        assert "→" in first_line


@pytest.mark.asyncio
async def test_footer_drops_transition_last_when_two_line_still_too_narrow(
    tmp_path, monkeypatch
):
    """Юзер-контракт: стрелка снимается ПОСЛЕДНЕЙ. Профиль уже снят и даже
    двухстрочный вариант с переходом всё ещё не влезает — отдаётся вариант
    без перехода (точка + факт-модель остаются)."""
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
            make_settings(snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), level_provider=lambda: "max"
        )
        app.active_runner = _call_runner_with_usage(
            "light", executor_router, (_meta(2_000_000, 0), _meta(1_000_000, 0))
        )
        monkeypatch.setattr(app, "display_router", lambda: live_router)

        head_snapshot = scr._footer_head_snapshot()
        head_no_profile = scr._footer_head(
            with_profile=False, snapshot=head_snapshot
        )
        # На 1 колонку меньше, чем сама голова с переходом (без профиля) —
        # даже без профиля переход не влезает НИ на одну строку 2-строчного
        # варианта, независимо от того, куда денется usage-блок.
        avail = cell_len(head_no_profile.plain) - 1
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )

        plain = scr._compose_footer_right().plain
        assert "→" not in plain
        assert "●" in plain
        assert "minimax-m3" in plain


@pytest.mark.asyncio
async def test_footer_one_line_priority_kept_with_profile_transition_and_usage(
    tmp_path, monkeypatch
):
    """Регресс-страж Task 8/9: когда всё влезает в одну строку (профиль,
    переход и usage-сегменты разом), результат остаётся однострочным — новая
    лесенка не уносит его на 2-ю строку зря."""
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
            make_settings(snapshot), level_provider=lambda: "light"
        )
        live_router = ModelRouter(
            make_settings(snapshot), level_provider=lambda: "max"
        )
        app.active_runner = _call_runner_with_usage(
            "light", executor_router, (_meta(2_000_000, 0), _meta(1_000_000, 0))
        )
        monkeypatch.setattr(app, "display_router", lambda: live_router)
        ref = ProfileRef(name="other", path=Path("x"), root_kind="project")
        monkeypatch.setattr(app, "active_profile_ref", lambda: ref)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 1000)

        plain = scr._compose_footer_right().plain
        assert "\n" not in plain
        assert plain.startswith("other ")
        assert "→" in plain


# --- Task 17: единая шестиступенчатая лесенка --------------------------------
#
# Правая колонка (#footer-right, width: auto) ЗАЯВЛЯЕТ потребность, левая
# (#footer-left, 1fr) получает остаток — поэтому ступени лесенки различаются
# ровно тем, сколько рядов при этом остатке занимает левая. Ширину здесь не
# подсовывают: её задаёт настоящий размер терминала (`pilot.resize_terminal`),
# как в жизни, — иначе взаимодействие колонок не проверить.
#
# Порядок жертв (решение юзера, дословно): 1) всё одной строкой; 2) цены вниз;
# 3) имя профиля 14 → 5; 4) левая переносит подсказки, имя пробуется снова от
# 14 к 5; 5) имя снимается — только когда слева двигать нечего; 6) снимается
# блок за стрелкой.

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


def _rows(text: str, width: int) -> int:
    """Сколько РЯДОВ займёт `text` при ширине `width` — той же меркой, какой его
    рисует Rich (перенос по словам), с учётом явных переносов."""
    from rich.console import Console
    from rich.text import Text

    console = Console(width=width)
    return sum(
        len(Text(line).wrap(console, width)) for line in text.split("\n")
    )


def _left_rows_beside(scr, total: int, right_plain: str) -> int:
    """Сколько рядов займёт левая колонка, когда правая забрала своё."""
    from gc_tui.services.notices_ui import (
        footer_left_with_badge,
        format_notices_badge,
    )
    from rich.cells import cell_len

    need = max(cell_len(line) for line in right_plain.split("\n"))
    width = total - need - _FOOTER_CHROME
    if width <= 0:
        return 99
    return _rows(
        footer_left_with_badge(
            scr._footer_left_commands(),
            format_notices_badge(scr._collect_footer_notices()),
            width=width,
        ),
        width,
    )


def _visible_profile_cols(right_plain: str) -> int:
    """Сколько колонок занимает ВИДИМОЕ имя профиля (0 — имя снято). Имя стоит
    в начале 1-й строки, до точки-индикатора уровня."""
    from rich.cells import cell_len

    head_line = right_plain.split("\n")[0]
    if head_line.startswith("●"):
        return 0
    return cell_len(head_line.split(" ●")[0])


def _left_rows_requested(scr, total: int, right_plain: str) -> int:
    """Сколько рядов левая ЗАПРОСИЛА бы при таком варианте правой — то самое
    число, по которому решает лесенка (считается ДО сторожа двух рядов, потому
    что сторож любой запрос кламповал бы до двух)."""
    from gc_tui.services.notices_ui import (
        footer_left_rows,
        format_notices_badge,
    )
    from rich.cells import cell_len

    need = max(cell_len(line) for line in right_plain.split("\n"))
    width = total - need - _FOOTER_CHROME
    if width <= 0:
        return 99
    return footer_left_rows(
        scr._footer_left_commands(),
        format_notices_badge(scr._collect_footer_notices()),
        width=width,
    )


def _narrowest_named_variant(scr, cols: int) -> str:
    """Самый узкий вариант правой колонки, где имя профиля ещё есть: голова с
    именем в `cols` колонок и цены внизу — последняя ступень перед снятием
    имени. Собирается тем же кодом, что и лесенка."""
    from gc_tui.services.usage_meter import compose_adaptive_footer_right

    head = scr._footer_head(
        with_profile=True,
        profile_name_cols=cols,
        snapshot=scr._footer_head_snapshot(),
    )
    request_seg, session_seg = scr._usage_segments()
    if request_seg is None and session_seg is None:
        return head.plain
    return compose_adaptive_footer_right(
        head, request_seg, session_seg, 0
    ).plain


def _arm_transition(app, monkeypatch, *, profile=None, usage=None):
    """Пин хода «light → max» (переход виден) + опционально usage-сегменты и
    активный профиль — общая обвязка ступенчатых тестов."""
    from gc_core.config_modules.profiles import ProfileRef
    from gc_core.model_router import ModelRouter

    snapshot = {
        "models": {"minimax-m3": {}, "opus-4.8": {}},
        "slots": {"light": "minimax-m3", "max": "opus-4.8"},
    }
    pinned = ModelRouter(
        make_settings(snapshot), level_provider=lambda: "light"
    )
    live = ModelRouter(make_settings(snapshot), level_provider=lambda: "max")
    app.active_runner = (
        _call_runner_with_usage("light", pinned, usage)
        if usage
        else _call_runner("light", pinned)
    )
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
async def test_footer_moves_prices_down_where_profile_used_to_be_dropped(
    tmp_path, monkeypatch
):
    """Ступень 2 на живой ширине (дефект смока): раньше правой колонке считали
    бюджет как «всё, что осталось от левой», левая не отдавала ни колонки — и на
    этой ширине футер терял и имя профиля, и стрелку разом. Теперь вниз уезжают
    цены, а голова (профиль + переход) остаётся на 1-й строке целиком."""
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
        _arm_transition(
            app,
            monkeypatch,
            profile=_LONG_NAME,
            usage=(_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
        )

        request_seg, session_seg = scr._usage_segments()
        head = scr._footer_head(
            with_profile=True, snapshot=scr._footer_head_snapshot()
        )
        stacked = compose_adaptive_footer_right(
            head, request_seg, session_seg, 0
        )
        need = max(cell_len(line) for line in stacked.plain.split("\n"))
        commands = scr._footer_left_commands()
        # Ширина: правой ровно на «цены вниз» с полным именем, левой — ровно
        # столько, сколько ей нужно на ДВА ряда (одного ряда уже не хватает).
        left_two = min(
            w
            for w in range(20, cell_len(commands) + 1)
            if _rows(commands, w) <= 2
        )
        total = need + _FOOTER_CHROME + left_two

        await pilot.resize_terminal(total, 30)
        plain = scr._compose_footer_right().plain

        assert plain == stacked.plain
        first, second = plain.split("\n")
        assert first.startswith(_CEILING)  # имя профиля цело (в потолке)
        assert "→" in first  # и переход тоже
        assert "запрос" in second and "сессия" in second
        assert _left_rows_beside(scr, total, plain) == 2


@pytest.mark.asyncio
async def test_footer_moves_left_to_two_rows_before_shrinking_profile(
    tmp_path, monkeypatch
):
    """Ступень 3 ПЕРЕД ступенью 4 (живой смок юзера, фикс-раунд 3): на ширине,
    где потолку имени не хватает ровно одной колонки, левая уходит в два ряда, а
    имя остаётся ПОЛНЫМ.

    Раньше проход с однорядной левой перебирал имя целиком — и имя ужималось
    (`openrout…`) при том, что слева все шесть подсказок спокойно стояли в один
    ряд. Перенос подсказок вниз ничего не теряет (второй ряд футера оплачен и
    так), а срез имени теряет: бесплатный ход обязан идти раньше платного."""
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
        _arm_transition(app, monkeypatch, profile=_LONG_NAME)

        ceiling_need = cell_len(
            scr._footer_head(
                with_profile=True,
                profile_name_cols=_PROFILE_NAME_MAX_COLS,
                snapshot=scr._footer_head_snapshot(),
            ).plain
        )
        commands = scr._footer_left_commands()
        # Ровно одной колонки не хватает, чтобы потолок имени ужился с левой в
        # одном ряду: прежний порядок резал здесь имя, новый — переносит левую.
        total = ceiling_need + _FOOTER_CHROME + cell_len(commands) - 1

        await pilot.resize_terminal(total, 30)
        plain = scr._compose_footer_right().plain

        assert plain.startswith(_CEILING)  # имя целое, не ужато
        assert "→" in plain
        assert _left_rows_beside(scr, total, plain) == 2


@pytest.mark.asyncio
async def test_footer_keeps_full_profile_when_left_goes_two_rows(
    tmp_path, monkeypatch
):
    """То же правило на ширине, где не хватает даже самому узкому имени при
    однорядной левой: имя всё равно остаётся в потолке — перебор имени живёт
    целиком во втором проходе, после того как левая уже разложилась в два ряда."""
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
        _arm_transition(app, monkeypatch, profile=_LONG_NAME)

        snapshot = scr._footer_head_snapshot()
        floor_need = cell_len(
            scr._footer_head(
                with_profile=True,
                profile_name_cols=_PROFILE_NAME_MIN_COLS,
                snapshot=snapshot,
            ).plain
        )
        commands = scr._footer_left_commands()
        # Левой на один ряд не хватает одной колонки даже при самом узком
        # варианте правой с именем — значит, придётся идти в два ряда.
        total = floor_need + _FOOTER_CHROME + cell_len(commands) - 1

        await pilot.resize_terminal(total, 30)
        plain = scr._compose_footer_right().plain

        assert plain.startswith(_CEILING)  # имя в потолке, не ужато
        assert "→" in plain
        assert _left_rows_beside(scr, total, plain) == 2


@pytest.mark.asyncio
async def test_footer_shrinks_profile_on_the_two_row_pass(
    tmp_path, monkeypatch
):
    """Ступень 4: имя ужимается 14 → 5 — но ТОЛЬКО во втором проходе, когда
    левая уже в двух рядах. Ширина здесь мала даже для потолка при двухрядной
    левой — имя ужимается, но не снимается."""
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
        _arm_transition(app, monkeypatch, profile=_LONG_NAME)

        snapshot = scr._footer_head_snapshot()
        ceiling_need = cell_len(
            scr._footer_head(
                with_profile=True,
                profile_name_cols=_PROFILE_NAME_MAX_COLS,
                snapshot=snapshot,
            ).plain
        )
        commands = scr._footer_left_commands()
        left_two = min(
            w
            for w in range(20, cell_len(commands) + 1)
            if _rows(commands, w) <= 2
        )
        # На одну колонку уже, чем нужно потолку при двухрядной левой.
        total = ceiling_need + _FOOTER_CHROME + left_two - 1

        await pilot.resize_terminal(total, 30)
        plain = scr._compose_footer_right().plain

        assert plain.startswith(_LONG_NAME[:12] + "…")  # 13 колонок вместо 14
        assert "→" in plain
        assert _left_rows_beside(scr, total, plain) == 2


@pytest.mark.asyncio
async def test_footer_ladder_order_holds_across_terminal_widths(
    tmp_path, monkeypatch
):
    """Ступени 5 и 6 как порядок, а не как одна ширина: перебор реальных ширин
    терминала сверху вниз.

    - имя профиля снимается, только когда с ним не прошёл ни один вариант:
      даже с минимальным именем левая запросила бы третий ряд;
    - имя профиля при сужении только укорачивается, никогда не растёт
      (фикс-раунд 3: перебор имени целиком во втором проходе, и он тоже
      начинается с потолка);
    - блок за стрелкой снимается последним: нет ширины, где переход снят, а имя
      профиля ещё на месте;
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
        _arm_transition(app, monkeypatch, profile=_LONG_NAME)

        seen_profile_dropped = False
        seen_transition_dropped = False
        prev_profile_cols = None
        for total in range(115, 59, -1):
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
                assert _left_rows_requested(scr, total, floor) > 2, (
                    f"ширина {total}: имя снято, хотя с минимальным именем левой хватило бы двух рядов — {plain!r}"
                )
            if "→" not in plain:
                seen_transition_dropped = True
                assert not has_profile, (
                    f"ширина {total}: переход снят раньше имени профиля — {plain!r}"
                )
        assert seen_profile_dropped and seen_transition_dropped


@pytest.mark.asyncio
async def test_footer_ladder_is_monotone_prices_never_come_back_up(
    tmp_path, monkeypatch
):
    """Лесенка монотонна по ширине: при сужении окна состояние только
    ухудшается.

    Цены уезжают вниз ПЕРВЫМИ и остаются внизу. Обратный прыжок возможен ровно
    на границе ступени 3: левой разрешили 2 ряда, места правой стало больше — и
    если вариант «всё одной строкой» пробуется во втором проходе, цены
    возвращаются наверх. Это то самое дёрганье, из-за которого задача заведена.

    Имя профиля монотонно вместе с ними (фикс-раунд 3): в первом проходе оно
    всегда в потолке, во втором перебор начинается тоже с потолка — «подрасти»
    при сужении ему больше негде.

    Раскладка подобрана так, чтобы прыжок был ДОСТИЖИМ: короткая модель и
    отсутствие перехода делают однострочный вариант достаточно узким, чтобы он
    влез при двухрядной левой (с длинной головой он не влезает никогда, и
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
        _arm_transition(
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
            assert "●" in first  # точка уровня и модель хода — там же
            assert "m" in first


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
        _arm_transition(
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
    executor = ModelRouter(make_settings(snapshot), level_provider=lambda: level["value"])
    app = TuiApp(threads_dir=str(tmp_path / "t"), settings=tui_settings({"agent.model": "m"}))
    app._config_path = str(tmp_path / "config.yml")
    app.active_runner = _running_runner(executor)
    monkeypatch.setattr(app, "_level_provider", lambda: level["value"])
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
        make_settings({"models": {"minimax-m3": {}}, "slots": {"light": "minimax-m3"}}),
        level_provider=lambda: "light",
    )
    new = ModelRouter(
        make_settings({"models": {"other-model": {}}, "slots": {"light": "other-model"}}),
        level_provider=lambda: "light",
    )
    app = TuiApp(threads_dir=str(tmp_path / "t"), settings=tui_settings({"agent.model": "m"}))
    app._config_path = str(tmp_path / "config.yml")
    app.active_runner = _running_runner(old)
    monkeypatch.setattr(app, "_level_provider", lambda: "light")
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
        make_settings({"models": {"other-model": {}}, "slots": {"light": "other-model"}}),
        level_provider=lambda: "light",
    )
    app = TuiApp(threads_dir=str(tmp_path / "t"), settings=tui_settings({"agent.model": "m"}))
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
        make_settings({"models": {"stale-model": {}}, "slots": {"light": "stale-model"}}),
        level_provider=lambda: "light",
    )
    app2 = TuiApp(threads_dir=str(tmp_path / "t2"), settings=tui_settings({"agent.model": "m"}))
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

    app = TuiApp(threads_dir=str(tmp_path / "t"), settings=tui_settings({"agent.model": "m"}))
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
    executor = ModelRouter(make_settings(snapshot), level_provider=lambda: "light")
    live = ModelRouter(make_settings(snapshot), level_provider=lambda: "max")
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
        app, monkeypatch, profile="other",
        usage=(_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
    )
    app.tui_state.active_task_id = "t1"
    monkeypatch.setattr(app, "auto_mode_active", lambda: True)
    async with app.run_test(size=(200, 30)):
        scr = app.screen
        head = scr._footer_head(with_profile=True)
        request, session = scr._usage_segments()
        expected = compose_adaptive_footer_right(
            head, request, session, available_width=0 if prices_down else 10_000
        )
        width = max(cell_len(line) for line in expected.plain.split("\n"))
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: width)
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
        app, monkeypatch, profile="very-long-profile-name",
        usage=(_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
    )
    from gc_core.model_router import ModelRouter

    model = "very-long-running-model-name-that-exceeds-usage-width"
    app.active_runner = _running_runner(
        ModelRouter(
            make_settings({"models": {model: {}}, "slots": {"default": model}}),
            level_provider=lambda: "default",
        ),
        (_usage_meta(2_000_000, 0), _usage_meta(1_000_000, 0)),
    )
    app.tui_state.active_task_id = "t1"
    monkeypatch.setattr(app, "auto_mode_active", lambda: True)
    async with app.run_test(size=(200, 30)):
        scr = app.screen
        request, session = scr._usage_segments()
        no_profile = compose_adaptive_footer_right(
            scr._footer_head(with_profile=False), request, session, available_width=0
        )
        width = max(cell_len(line) for line in no_profile.plain.split("\n"))
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: width)
        plain = scr._compose_footer_right().plain
        assert "\n" in plain
        assert plain.startswith(f"● → ● {model}")
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 4)
        plain = scr._compose_footer_right().plain
        assert plain.split("\n")[0] == "●"
