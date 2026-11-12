"""Футер на узком и на низком окне: полоса не теряет ни хвоста, ни себя.

Высота полосы (`styles.tcss`) — `auto` с полом: пока текст влезает в строку,
под ним остаётся привычный воздух, а когда не влезает — полоса растёт, и
последний сегмент («Esc») доезжает до пользователя. Обрубок врал бы о
клавишах ровно так же, как обрубок счётчиков в шапке экрана изменений.

Второе правило — про высоту (Р17): футер прибит к низу и виден при ЛЮБОЙ
высоте окна, а область содержимого ограничена доступной высотой и скроллится.
Экраны, чья карточка росла по содержимому (`/settings`, онбординг,
пост-апдейт), выдавливали полосу за кадр — на 100x12 `#set-footer` оказывался
на y=14, то есть за экраном высотой 12, и выхода из экрана было не видно.

Третье правило — про рамку (Р19): она всегда выглядит завершённой, а футер
приклеен к её нижней грани. Ограничить одну высоту оказалось мало: рамка жила
на карточке ВНУТРИ прокрутки, росла вместе с содержимым и уходила за нижний
край кадра — нижней грани не было нарисовано вовсе, скроллбар шёл снаружи, у
края экрана, а футер стоял сам по себе внизу. Рамка переехала на саму область
прокрутки (`height: auto` + потолок из `ScrollFrameMixin._sync_frame_cap`), и
её высота стала min(содержимое, остаток кадра).

Все три правила общие для read-only экранов, поэтому и проверяются здесь, а не
в тестах каждого из них по отдельности.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.ladder import STEP_HEAD
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.thread_store import edits_journal_path
from gc_testkit.tui_support import as_settings, tui_settings
from gc_tui.screens.diff import (
    ROW_ACCEPTED,
    ROW_GROUP,
    ROW_GROUP_FILE,
    ROW_PLAIN,
    DiffScreen,
    footer_text,
)
from textual.widgets import Static


def _clipped(widget: Static) -> bool:
    """Содержимому нужно больше строк, чем отдано полосе, — хвост не виден."""
    needed = widget.get_content_height(
        widget.container_size, widget.container_size, widget.size.width
    )
    return needed > widget.size.height


def _diff_screen(tmp_path: Path) -> DiffScreen:
    """Экран изменений с одним файлом в git-режиме — самый длинный футер."""
    threads = tmp_path / "threads"
    threads.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    EditsJournal(edits_journal_path(str(threads), "cli", "th1")).append(
        EditRecord(
            path="a.py",
            turn_id="t1",
            ts=1.0,
            call_id="c1",
            op="edit",
            hunks="",
            add=1,
            del_=1,
            before_sha256=None,
            after_sha256=None,
            flags=[],
        )
    )
    SnapshotStore(
        snapshots_root(str(root)), "cli_th1", threads
    ).capture_session("a.py", b"before\n")
    (root / "a.py").write_text("after\n", encoding="utf-8", newline="\n")
    return DiffScreen(
        launch_root=str(root),
        settings=as_settings(
            {"launch_root": str(root), "agent.model": "M", "agent_edits": {}}
        ),
        threads_dir=str(threads),
        system_id="cli",
        thread_id="th1",
    )


@pytest.mark.asyncio
async def test_diff_footer_is_whole_on_a_narrow_terminal(tmp_path):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(40, 20)) as pilot:
        screen = _diff_screen(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        footer = screen.query_one("#footer", Static)
        assert footer.size.height > 2  # текст в одну полосу не влез
        assert not _clipped(footer)


@pytest.mark.asyncio
async def test_wide_terminal_keeps_the_air_line_under_the_footer(tmp_path):
    """Пол высоты сохраняет привычный воздух: короткий футер полосы не сжимает."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(160, 20)) as pilot:
        screen = _diff_screen(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        footer = screen.query_one("#footer", Static)
        assert footer.size.height == 2
        assert not _clipped(footer)


# Строка действий `/diff` собирается из сегментов по строке под курсором
# (`footer_text`), и живой фикстурой все сочетания не поднять: ступень HEAD
# требует настоящих коммитов, а `revertable=False` — файла без снимка. Полосу
# при этом обязан выдерживать КАЖДЫЙ вариант, поэтому текст подставляется
# прямо в виджет — проверяется здесь именно вёрстка полосы, не логика выбора
# варианта (её держит `tests/tui/test_diff_screen.py`).
#
# `Enter` переехал из навигации в действия (Ж3 батча 4) — своим сегментом в
# каждом варианте, кроме заголовка группы, где он и стал всей строкой.
_LONGEST_FOOTER = dict(
    row=ROW_PLAIN,
    can_reset=True,
    step=STEP_HEAD,
    mass_accept=True,
    mass_revert=True,
    mass_reset=True,
)
_FOOTER_VARIANTS = [
    ("обычный", dict(row=ROW_PLAIN)),
    ("обычный+u", dict(row=ROW_PLAIN, can_reset=True)),
    ("коммит-ступень", dict(row=ROW_PLAIN, can_reset=True, step=STEP_HEAD)),
    ("без отката", dict(row=ROW_PLAIN, can_revert=False)),
    ("принято", dict(row=ROW_ACCEPTED)),
    ("группа свёрнута", dict(row=ROW_GROUP, mass_reset=True)),
    (
        "группа развёрнута",
        dict(row=ROW_GROUP, group_expanded=True, mass_reset=True),
    ),
    ("файл группы", dict(row=ROW_GROUP_FILE, can_reset=True, mass_reset=True)),
    # Хвост из трёх сегментов (Е4) — массовые клавиши свойство ПРЕДСТАВЛЕНИЯ и
    # живут целиком на любой строке; на `ROW_ACCEPTED` он держится с волны 4.
    (
        "хвост из трёх сегментов",
        dict(
            row=ROW_ACCEPTED,
            mass_accept=True,
            mass_revert=True,
            mass_reset=True,
        ),
    ),
    # Самое длинное сочетание вообще (Ж3 батча 4): обычная строка с `Enter
    # дифф`, всеми тремя своими клавишами и всеми тремя массовыми — её
    # сегменты длиннее, чем у `ROW_ACCEPTED`. Ступень — `STEP_HEAD`:
    # «закоммиченному» на знак длиннее «началу сессии».
    ("обычный+u+ступень HEAD+хвост из трёх сегментов", _LONGEST_FOOTER),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, kwargs",
    _FOOTER_VARIANTS,
    ids=[name for name, _ in _FOOTER_VARIANTS],
)
async def test_every_diff_footer_variant_is_whole_on_a_narrow_terminal(
    tmp_path, name, kwargs
):
    """Ни один вариант строки действий не теряет хвоста на узком окне."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(40, 24)) as pilot:
        screen = _diff_screen(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        footer = screen.query_one("#footer", Static)
        footer.update(footer_text(has_group=True, **kwargs))
        await pilot.pause()
        assert not _clipped(footer), name


@pytest.mark.asyncio
async def test_sessions_footer_is_whole_on_a_narrow_terminal(tmp_path):
    """Тот же приём у полосы `/sessions` — её подсказки длиннее всех в базе."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.sessions import SessionsScreen

    threads = tmp_path / "threads"
    threads.mkdir(parents=True, exist_ok=True)
    app = TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(60, 20)) as pilot:
        screen = SessionsScreen(threads_dir=str(threads), system="cli")
        await pilot.app.push_screen(screen)
        await pilot.pause()
        # Здесь режет не сам текст, а полоса вокруг него: подсказки лежат в
        # `Horizontal`, и высоту всей полосе задаёт он.
        label = screen.query_one("#sessions-footer-left", Static)
        needed = label.get_content_height(
            label.container_size, label.container_size, label.size.width
        )
        assert needed > 1  # в строку не влез
        assert screen.query_one("#sessions-footer").size.height >= needed


@pytest.mark.asyncio
async def test_main_screen_footer_height_stays_fixed(tmp_path):
    """Исключение записано явно: футер главного экрана верстается кодом.

    Он стоит над полем ввода, и рост полосы двигал бы ввод под рукой; усечение
    там делают строки левой части (многоточие) и адаптивный usage, а не
    перенос строки.
    """
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(30, 20)) as pilot:
        screen = pilot.app.screen
        label = screen.query_one("#footer-slot", Static)
        # Текст задаём сами: естественное содержимое футера у develop адаптивное
        # (значок уведомлений уходит на свою строку), и его длина — не предмет
        # этого теста. Предмет — что высота полосы приколочена версткой и не
        # едет за содержимым.
        label.update("команда\n" * 4)
        await pilot.pause()
        needed = label.get_content_height(
            label.container_size, label.container_size, label.size.width
        )
        assert needed > 2  # без исключения полоса выросла бы
        assert screen.query_one("#footer").size.height == 2


# --- Р17: футер не уезжает за нижний край -------------------------------------


def _screen_lines(app) -> list[str]:
    """Текстовый слепок экрана строками композитора."""
    return [
        "".join(seg.text for seg in strip).rstrip()
        for strip in app.screen._compositor.render_strips()
    ]


def _footer_on_screen(app, footer: Static, height: int) -> bool:
    """Полоса и стоит в кадре по геометрии, и правда нарисована."""
    region = footer.region
    if region.y < 0 or region.y + region.height > height:
        return False
    head = str(footer.render()).splitlines()[0][:20]
    return any(head in line for line in _screen_lines(app))


def _settings_screen(tmp_path: Path):
    from gc_tui.screens.settings import SettingsScreen

    return SettingsScreen(
        settings=as_settings({"agent.model": "M"}),
        config_path=None,
        on_apply=lambda: None,
    )


def _onboarding_screen(tmp_path: Path):
    from gc_tui.screens.onboarding import OnboardingScreen

    return OnboardingScreen(
        settings=as_settings({"agent.model": "M"}),
        config_path=str(tmp_path / "c.yml"),
        review_all=True,
        on_commit=lambda: None,
    )


def _post_update_screen(tmp_path: Path):
    from gc_tui.screens.post_update import PostUpdateScreen

    class _Upgrade:
        old_version = "0.2.2"
        new_version = "0.2.3"

    return PostUpdateScreen(
        upgrade=_Upgrade(),
        settings=as_settings({}),
        config_path=str(tmp_path / "c.yml"),
    )


_LOW_SCREENS = [
    ("settings", _settings_screen, "#set-footer"),
    ("onboarding", _onboarding_screen, "#onb-footer"),
    ("post-update", _post_update_screen, "#footer"),
    ("diff", _diff_screen, "#footer"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 12), (80, 10)])
@pytest.mark.parametrize(
    "name,build,footer_id", _LOW_SCREENS, ids=[row[0] for row in _LOW_SCREENS]
)
async def test_footer_stays_in_frame_on_a_low_terminal(
    tmp_path, size, name, build, footer_id
):
    """Р17: на низком окне полоса остаётся в кадре, а не уезжает под него."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=size) as pilot:
        screen = build(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        footer = screen.query_one(footer_id, Static)
        assert _footer_on_screen(pilot.app, footer, size[1]), (
            name,
            footer.region,
            _screen_lines(pilot.app),
        )
        if name == "diff":
            # Хвост массовых клавиш (Е4) и `Enter`, переехавший в действия
            # (Ж3 батча 4), удлинили строку настолько, что даже на этом файле
            # (без группы) она переносится и здесь.
            assert footer.size.height > 2, (size, footer.region)
            # Самое длинное сочетание сегментов растит полосу сильнее всего —
            # именно на низком окне это и грозит выдавить её за кадр.
            footer.update(footer_text(has_group=True, **_LONGEST_FOOTER))
            await pilot.pause()
            assert not _clipped(footer), (size, footer.region)
            assert _footer_on_screen(pilot.app, footer, size[1]), (
                size,
                footer.region,
                _screen_lines(pilot.app),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,build,scroll_id,up",
    [
        ("onboarding", _onboarding_screen, "#onb-scroll", "ctrl+up"),
        ("post-update", _post_update_screen, "#pu-scroll", "pageup"),
    ],
)
async def test_keyboard_scrolls_the_content_area_both_ways(
    tmp_path, name, build, scroll_id, up
):
    """Р17: клавиатурная прокрутка вернулась — и вниз, и ОБРАТНО вверх.

    `can_focus = False` у области прокрутки отнимал у неё все клавиши, и
    содержимое ниже сгиба доставалось только колесу мыши. Дорога назад
    проверяется отдельно: в онбординге `PgUp` занят возвратом на шаг, а
    `Home`/`End` съедает поле ввода — уехав вниз, вернуться было нечем.
    """
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 10)) as pilot:
        screen = build(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        scroll = screen.query_one(scroll_id)
        assert scroll.max_scroll_y > 0, name  # прокручивать есть что
        await pilot.press("pagedown")
        await pilot.pause()
        down = scroll.scroll_offset.y
        assert down > 0, name

        await pilot.press(up)
        await pilot.pause()
        await pilot.pause()
        assert scroll.scroll_offset.y < down, (name, up)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,build,footer_id,scroll_id",
    [
        ("onboarding", _onboarding_screen, "#onb-footer", "#onb-scroll"),
        ("settings", _settings_screen, "#set-footer", "#set-scroll"),
    ],
)
async def test_scroll_key_is_named_only_while_it_works(
    tmp_path, name, build, footer_id, scroll_id
):
    """Подсказка о прокрутке живёт, пока содержимое не влезает в кадр.

    То же правило «мёртвые клавиши не рекламируются», что на экране изменений:
    на высоком окне прокручивать нечего, и звать `Ctrl+↑/↓` не за чем.
    """
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 10)) as pilot:
        screen = build(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        assert screen.query_one(scroll_id).max_scroll_y > 0, name
        assert "Ctrl+↑/↓" in str(
            screen.query_one(footer_id, Static).render()
        ), name

        await pilot.resize_terminal(80, 60)  # влезло целиком
        await pilot.pause()
        await pilot.pause()
        assert screen.query_one(scroll_id).max_scroll_y == 0, name
        assert "Ctrl+↑/↓" not in str(
            screen.query_one(footer_id, Static).render()
        ), name


@pytest.mark.asyncio
async def test_content_area_owns_keyboard_scrolling(tmp_path):
    """Р17: у области содержимого есть фокус — иначе прокручивать её нечем.

    `can_focus = False` не просто снимал курсор: когда внутри экрана не
    фокусируется НИЧЕГО (меню `/settings` — одни Static), контейнер выпадал и
    из цепочки биндингов, и клавиши прокрутки не доставались никому. `End` в
    меню экраном не занят и обязан довозить кадр до низа.
    """
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 10)) as pilot:
        screen = _settings_screen(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        scroll = screen.query_one("#set-scroll")
        assert scroll.can_focus and scroll.has_focus
        await pilot.press("end")
        await pilot.pause()
        assert scroll.scroll_offset.y == scroll.max_scroll_y > 0


def _visible_in(scroll, widget) -> bool:
    window = scroll.scrollable_content_region
    region = widget.region
    return (
        region.height > 0
        and region.y >= window.y
        and region.y + region.height <= window.y + window.height
    )


def _bottom_of_card(screen):
    """Самый нижний элемент карточки — последний в DOM внутри `#set-frame`."""
    kids = [w for w in screen.query("#set-frame *") if w.outer_size.height]
    return kids[-1] if kids else None


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 12)])
# Секции — ПО ИМЕНАМ, а не номерам: вставка новой секции сдвигает номера
# соседей, и тест, прибитый к числу, тихо проверяет не ту (канон
# `gc_testkit.tui_support.settings_section_index`).
@pytest.mark.parametrize(
    "section_name",
    [
        "Подключение",
        "Модели",
        "Профили",
        "MCP-серверы",
        "Голосовой ввод",
        "Режимы работы",
        "Внешний вид",
        "Обновление",
    ],
)
async def test_every_settings_section_reaches_its_bottom_by_keyboard(
    tmp_path, size, section_name
):
    """Р17: нижний элемент КАЖДОЙ секции достижим одной клавиатурой.

    Замер до фикса: «Подключение» на 80x24 (нормальный размер) — `max_y=4`, и
    ни одна клавиша кадр не двигала; под сгибом оставался вердикт проверки,
    то есть ответ на `Tab` — ровно то, ради чего в секцию заходят.

    Набор клавиш: `↓` (кадр едет за курсором) и `Ctrl+↓` (явная прокрутка для
    того, к чему курсор не ходит). `PgUp`/`PgDn` тут заняты сменой секции, а
    `Home`/`End` внутри поля принадлежат тексту — их не трогаем.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.settings import SettingsScreen

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://host/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer secret-token"\n'
        "agent:\n"
        '  model: "test-model"\n',
        encoding="utf-8",
    )
    config = {
        "backend.completions_url": "http://host/v1/chat/completions",
        "backend.chat_completion_headers": {
            "Authorization": "Bearer secret-token"
        },
        "agent.model": "test-model",
    }

    def _probe_fn(endpoint, auth, verify, model, **kw):
        from gc_core.model_picker import ModelInfo
        from gc_tui.services.settings_probe import ProbeResult

        return ProbeResult(
            "ok", 200, [ModelInfo(id="test-model")], 118, "доступен"
        )

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(config),
    )
    async with app.run_test(size=size) as pilot:
        screen = SettingsScreen(
            settings=as_settings(config),
            config_path=str(cfg_file),
            on_apply=lambda: None,
            probe_fn=_probe_fn,
        )
        await pilot.app.push_screen(screen)
        await pilot.pause()
        from gc_testkit.tui_support import settings_section_index

        section = settings_section_index(section_name)
        screen._open_section(section)
        await pilot.pause()
        await pilot.pause()
        if section in (0, 1):
            await pilot.press("tab")  # вердикт проверки — в самом низу
            await pilot.pause()
            await pilot.pause()
        scroll = screen.query_one("#set-scroll")
        if not scroll.max_scroll_y:
            return  # секция влезла целиком
        for key, presses in (("down", 8), ("ctrl+down", 8), ("tab", 12)):
            for _ in range(presses):
                bottom = _bottom_of_card(screen)
                if bottom is not None and _visible_in(scroll, bottom):
                    return
                await pilot.press(key)
                await pilot.pause()
                await pilot.pause()
                assert screen._view == section, (
                    key
                )  # клавиша не сменила секцию
        bottom = _bottom_of_card(screen)
        assert bottom is not None and _visible_in(scroll, bottom), (
            section,
            size,
            scroll.scroll_offset.y,
            scroll.max_scroll_y,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("section_name", ["Подключение", "Голосовой ввод"])
async def test_ctrl_down_with_nothing_to_open_scrolls_the_section(
    tmp_path, section_name
):
    """`Ctrl+↓`, которому нечего раскрывать, прокручивает тело секции.

    У `develop` `Ctrl+↓` — тихий синоним `Alt+↓` «раскрыть список поля», и ветки
    раскрытия съедали клавишу БЕЗУСЛОВНО — даже когда список уже раскрыт (адрес) или
    его нет вовсе (голос). Футер при этом обещал «Ctrl+↑/↓ — прокрутка», а вниз
    кадр не ехал ничем: у голоса низ секции оставался недостижим с клавиатуры.
    Сперва `Ctrl+↑` — он раскрытием не занят и освобождает место внизу. Третья
    ветка того же класса — реестр моделей — сторожится у своей секции
    (`test_settings_models_section`): ей нужен конфиг с реестром."""
    from form_harness import ok_probe
    from gc_testkit.tui_support import open_settings_section
    from gc_tui.app import TuiApp
    from gc_tui.screens.settings import SettingsScreen

    config = {
        "backend.completions_url": "http://host/v1/chat/completions",
        "backend.chat_completion_headers": {
            "Authorization": "Bearer secret-token"
        },
        "agent.model": "test-model",
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(config),
    )
    async with app.run_test(size=(100, 12)) as pilot:
        screen = SettingsScreen(
            settings=as_settings(config),
            config_path=str(tmp_path / "config.yml"),
            on_apply=lambda: None,
            probe_fn=ok_probe(),
        )
        await pilot.app.push_screen(screen)
        await pilot.pause()
        open_settings_section(screen, section_name)
        await pilot.pause()
        await pilot.pause()
        scroll = screen.query_one("#set-scroll")
        for _ in range(4):
            await pilot.press("ctrl+up")
            await pilot.pause()
        top = scroll.scroll_offset.y
        assert top < scroll.max_scroll_y, "предусловие: внизу есть куда ехать"
        await pilot.press("ctrl+down")
        await pilot.pause()
        await pilot.pause()
        assert scroll.scroll_offset.y > top, section_name


@pytest.mark.asyncio
async def test_settings_section_cursor_carries_the_frame(tmp_path):
    """Р17: в секции кадр тоже едет за курсором, а не только в меню.

    Ход курсора в секции ведёт редактор, а не экран, — доводка обязана быть
    общей, иначе выбранная строка списка серверов уезжает под футер молча.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.settings import SettingsScreen

    config = {"agent.model": "test-model"}
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(config),
    )
    async with app.run_test(size=(100, 12)) as pilot:
        screen = SettingsScreen(
            settings=as_settings(config),
            config_path=str(tmp_path / "c.yml"),
            on_apply=lambda: None,
        )
        await pilot.app.push_screen(screen)
        await pilot.pause()
        screen._open_section(2)  # MCP-серверы: список длиннее кадра
        await pilot.pause()
        await pilot.pause()
        scroll = screen.query_one("#set-scroll")
        assert scroll.max_scroll_y > 0
        for _ in range(4):
            await pilot.press("down")
            await pilot.pause()
        await pilot.pause()
        assert scroll.scroll_offset.y > 0
        rows = [w for w in screen.query(".selected") if w.outer_size.height]
        assert rows, "курсор секции потерялся"
        assert _visible_in(scroll, rows[-1]), rows[-1].region


@pytest.mark.asyncio
async def test_settings_menu_cursor_carries_the_frame(tmp_path):
    """Р17 в `/settings`: PgUp/PgDn там заняты секциями, кадр ведёт курсор.

    Выбор двигают ↑/↓, и на низком окне выбранная строка уезжала под футер
    молча: прокрутить к ней было нечем, а сама она за кадром не показывалась.
    """
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 10)) as pilot:
        screen = _settings_screen(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        scroll = screen.query_one("#set-scroll")
        assert scroll.max_scroll_y > 0  # прокручивать есть что
        for _ in range(4):
            await pilot.press("down")
        await pilot.pause()
        await pilot.pause()
        assert scroll.scroll_offset.y > 0
        row = [
            w for w in screen.query(".set-menurow") if w.has_class("selected")
        ]
        assert row and row[0].region.y >= 0, row  # курсор в кадре
        assert row[0].region.y + row[0].region.height <= 10


# --- Р19: рамка завершена, футер приклеен к её нижней грани --------------------


def _viewer_screen(tmp_path: Path):
    """Вьюер правки — тело семьи `/diff`, единственное с длинным содержимым."""
    from gc_tui.screens.diff import DiffFileViewer
    from gc_tui.services.edits_view import SOURCE_SESSION, EditsView

    threads = tmp_path / "threads"
    threads.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    lines = [f"line{n:02d}" for n in range(1, 41)]
    SnapshotStore(
        snapshots_root(str(root)), "cli_th1", threads
    ).capture_session("a.py", ("\n".join(lines) + "\n").encode("utf-8"))
    changed = list(lines)
    changed[2] = "line03 правлено"
    (root / "a.py").write_text(
        "\n".join(changed) + "\n", encoding="utf-8", newline="\n"
    )
    EditsJournal(edits_journal_path(str(threads), "cli", "th1")).append(
        EditRecord(
            path="a.py",
            turn_id="t1",
            ts=1.0,
            call_id="c1",
            op="edit",
            hunks="",
            add=1,
            del_=1,
            before_sha256=None,
            after_sha256=None,
            flags=[],
        )
    )
    config = {"launch_root": str(root), "agent.model": "M", "agent_edits": {}}
    view = EditsView(
        str(threads), "cli", "th1", str(root), as_settings(config)
    )
    files = [row.entry for row in view.session_view(SOURCE_SESSION).active]
    return DiffFileViewer(
        view=view, source=SOURCE_SESSION, files=files, index=0
    )


_FRAMED_SCREENS = [
    ("settings", _settings_screen, "#set-scroll", "#set-footer"),
    ("onboarding", _onboarding_screen, "#onb-scroll", "#onb-footer"),
    ("post-update", _post_update_screen, "#pu-scroll", "#footer"),
    ("diff", _diff_screen, "#trace", "#footer"),
    ("viewer", _viewer_screen, "#trace", "#footer"),
]
_FRAMED_IDS = [row[0] for row in _FRAMED_SCREENS]
# Экраны правок держат между рамкой и полосой подсказок зарезервированную
# строку под плашку результата (Д8 второго смоука): она есть ВСЕГДА — и с
# плашкой, и без неё, — поэтому футер там отбит от нижней грани на строку, а
# не прижат вплотную, как у остальных.
_FLASH_GAP_SCREENS = {"diff", "viewer"}


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(40, 10), (80, 12), (80, 24), (120, 40)])
@pytest.mark.parametrize(
    "name,build,scroll_id,footer_id", _FRAMED_SCREENS, ids=_FRAMED_IDS
)
async def test_frame_is_closed_and_the_footer_hugs_its_bottom_edge(
    tmp_path, size, name, build, scroll_id, footer_id
):
    """Р19: нижняя грань нарисована при любой высоте, футер — сразу под ней.

    Замер до фикса (`/settings`, меню): на 80x12 нижней грани не было вовсе —
    рамка уходила за край, а на 80x24 она закрывалась на y=18, тогда как футер
    стоял на y=22, оторванный от неё четырьмя пустыми строками. Обе беды —
    одна причина: рамка жила на карточке внутри прокрутки. У экранов правок
    «сразу под ней» — через зарезервированную строку под плашку
    (`_FLASH_GAP_SCREENS`), и это тоже фиксированное расстояние: рамка не
    отрывается от полосы на произвольную пустоту.
    """
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=size) as pilot:
        screen = build(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        frame = screen.query_one(scroll_id)
        footer = screen.query_one(footer_id, Static)
        lines = _screen_lines(pilot.app)
        edge = frame.region.y + frame.region.height - 1  # строка нижней грани
        assert 0 <= edge < size[1], (name, frame.region)  # рамка в кадре
        assert "╰" in lines[edge] and "╯" in lines[edge], (name, edge, lines)
        gap = 2 if name in _FLASH_GAP_SCREENS else 1
        assert footer.region.y == edge + gap, (name, edge, footer.region)


# Список `/diff` здесь не участвует: в местной фикстуре у него один файл, и
# прокручивать нечего ни на какой высоте. Рамку своей семьи он делит с вьюером
# (одно правило CSS на все три экрана правок), а вьюер в наборе есть.
_SCROLLING_SCREENS = [row for row in _FRAMED_SCREENS if row[0] != "diff"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,build,scroll_id,footer_id",
    _SCROLLING_SCREENS,
    ids=[row[0] for row in _SCROLLING_SCREENS],
)
async def test_scrollbar_runs_inside_the_frame(
    tmp_path, name, build, scroll_id, footer_id
):
    """Р19: скроллбар — внутри рамки, а не снаружи у края экрана.

    Прокрутка живёт на самом обрамлённом контейнере, поэтому полоса ползунка
    обязана идти между гранями. Пока рамка была на карточке внутри скролла,
    ползунок шёл по краю ЭКРАНА — рамка и прокрутка выглядели как два разных
    предмета.
    """
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 10)) as pilot:
        screen = build(tmp_path)
        await pilot.app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        frame = screen.query_one(scroll_id)
        assert frame.max_scroll_y > 0, name  # прокручивать есть что
        bar = frame.vertical_scrollbar
        assert bar.display, name
        assert frame.region.x < bar.region.x, (name, bar.region, frame.region)
        assert bar.region.right < frame.region.right, (
            name,
            bar.region,
            frame.region,
        )
        assert frame.region.y < bar.region.y, (name, bar.region, frame.region)
        assert bar.region.bottom < frame.region.bottom, (
            name,
            bar.region,
            frame.region,
        )
