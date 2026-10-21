"""SkillPicker integration в TraceScreen: триггер, фильтр, mutex с командной палитрой."""

from gc_testkit.tui_support import tui_settings
import pytest


class _FakeReg:
    async def list_skills(self, *, pack=None, include_all=False):
        return [
            {"id": "supercoder:planner"},
            {"id": "supercoder:coder"},
            {"id": "codebase:using-codebase"},
            {"id": "skill-creator"},
        ]


def _patch_registry(monkeypatch):
    from gc_core.tools.skills import registry as reg_mod

    monkeypatch.setattr(
        reg_mod, "get_skills_registry", lambda cfg=None: _FakeReg()
    )


@pytest.mark.asyncio
async def test_picker_opens_on_skill_space(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 1


@pytest.mark.asyncio
async def test_picker_filters_by_id_substring(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill super"
        await pilot.pause(0.1)
        picker = pilot.app.query_one(SkillPicker)
        rows_text = "\n".join(
            str(s.render()) for s in picker.query(".palette-row")
        )
        heads = [str(s.render()) for s in picker.query(".palette-pack-head")]
        # строки — короткие имена под шапкой пакета, полный id не нужен
        assert [h.strip() for h in heads] == ["supercoder"]
        assert "planner" in rows_text
        assert "coder" in rows_text
        assert "supercoder:" not in rows_text
        assert "using-codebase" not in rows_text


@pytest.mark.asyncio
async def test_tab_after_arrows_inserts_full_id_of_flat_skill(
    tmp_path, monkeypatch
):
    """Стрелки идут по скиллам мимо шапок пакетов; Tab вставляет полный id."""
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        # skill-creator (без пакета) первым, затем planner, coder, …
        await pilot.press("down", "down", "down")
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert inp.text == "/skill codebase:using-codebase"


@pytest.mark.asyncio
async def test_picker_closes_when_space_removed(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_palette import CommandPalette
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 1

        inp.text = (
            "/skill"  # пробел убран → должна сработать командная палитра
        )
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 0
        assert len(pilot.app.query(CommandPalette)) == 1


@pytest.mark.asyncio
async def test_palette_and_picker_mutually_exclusive(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_palette import CommandPalette
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        # Состояния: /skill -> palette; /skill " " -> picker; /skill -> palette снова
        inp.text = "/sk"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        assert len(pilot.app.query(SkillPicker)) == 0

        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 0
        assert len(pilot.app.query(SkillPicker)) == 1

        inp.text = "/skill"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        assert len(pilot.app.query(SkillPicker)) == 0


@pytest.mark.asyncio
async def test_tab_completes_id_into_input_and_closes_picker(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill super"
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)

        assert inp.text == "/skill supercoder:planner"
        assert len(pilot.app.query(SkillPicker)) == 0


@pytest.mark.asyncio
async def test_esc_closes_picker_keeps_input(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill super"
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)

        assert len(pilot.app.query(SkillPicker)) == 0
        assert inp.text == "/skill super"


@pytest.mark.asyncio
async def test_empty_filter_result_shows_no_match_line(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill zzz_no_such"
        await pilot.pause(0.1)
        picker = pilot.app.query_one(SkillPicker)
        hint_text = "\n".join(
            str(s.render()) for s in picker.query(".palette-hint")
        )
        assert "нет совпадений" in hint_text
        # Нет .palette-row
        assert len(picker.query(".palette-row")) == 0


@pytest.mark.asyncio
async def test_skills_loaded_only_once_per_session(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    calls = {"n": 0}

    from gc_core.tools.skills import registry as reg_mod

    def factory(cfg=None):
        calls["n"] += 1
        return _FakeReg()

    monkeypatch.setattr(reg_mod, "get_skills_registry", factory)

    from gc_tui.app import TuiApp
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 1
        inp.text = ""
        await pilot.pause(0.1)
        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 1
        # Реестр загружен один раз на всю сессию.
        assert calls["n"] == 1


@pytest.mark.asyncio
async def test_hint_has_keys_margin_and_honest_desired_height(
    tmp_path, monkeypatch
):
    """Hint SkillPicker — докнутый footer (.palette-footer) с воздухом (.palette-keys),
    desired_height включает строку margin (шов main.py::_sync_trace_max_height)."""
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        picker = pilot.app.query_one(SkillPicker)
        hint = picker.query(".palette-keys")
        assert len(hint) == 1
        assert picker.query_one(".palette-footer").query(
            ".palette-keys"
        )  # hint внутри докнутого подвала
        assert picker.desired_height == len(picker.children) + 3
        assert hint[0].region.bottom <= picker.region.bottom


@pytest.mark.asyncio
async def test_hint_visible_on_open_with_long_list_no_scroll(
    tmp_path, monkeypatch
):
    """Регрессия c0622c9a: длинный список скиллов (> высоты клэмпа) — hint-строка
    видна СРАЗУ при открытии, без скролла (докнутый .palette-footer)."""
    pytest.importorskip("textual")

    class _BigReg:
        async def list_skills(self, *, pack=None, include_all=False):
            return [{"id": f"pack:skill{i:03d}"} for i in range(45)]

    from gc_core.tools.skills import registry as reg_mod

    monkeypatch.setattr(
        reg_mod, "get_skills_registry", lambda cfg=None: _BigReg()
    )

    from gc_tui.app import TuiApp
    from gc_tui.widgets.skill_picker import SkillPicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        picker = pilot.app.query_one(SkillPicker)
        assert len(picker._visible_rows()) > 20  # список длиннее клэмпа
        assert (
            picker.region.height < picker.desired_height
        )  # клэмп реально сработал — скролл включён
        hint = picker.query(".palette-keys")
        assert len(hint) == 1
        assert hint[0].region.bottom <= 24  # на экране
        assert (
            hint[0].region.bottom <= picker.region.bottom
        )  # внутри рамки пикера, видна без скролла


def _patch_big_registry(monkeypatch, skills):
    class _BigReg:
        async def list_skills(self, *, pack=None, include_all=False):
            return skills

    from gc_core.tools.skills import registry as reg_mod

    monkeypatch.setattr(
        reg_mod, "get_skills_registry", lambda cfg=None: _BigReg()
    )


def _on_screen(picker, widget) -> bool:
    """Строка целиком внутри видимой области списка пикера."""
    top = picker.scroll_offset.y
    y = widget.virtual_region.y
    return top <= y and y + 1 <= top + picker.scrollable_content_region.height


async def _open_picker(pilot):
    from gc_tui.widgets.skill_picker import SkillPicker

    inp = pilot.app.query_one("#cmdinput")
    inp.text = "/skill "
    await pilot.pause(0.1)
    return pilot.app.query_one(SkillPicker)


_LONG = (
    [{"id": "flat-skill"}]
    + [{"id": f"alpha:a{i:02d}"} for i in range(20)]
    + [{"id": f"beta:b{i:02d}"} for i in range(20)]
)


@pytest.mark.asyncio
@pytest.mark.parametrize("skills", [_LONG, _LONG[1:]], ids=["flat", "head"])
async def test_scroll_back_to_top_shows_first_row(
    tmp_path, monkeypatch, skills
):
    """Вниз до конца и обратно наверх — список снова с самого верха: первая
    строка (скилл без пакета или шапка первого пакета) видна."""
    pytest.importorskip("textual")
    _patch_big_registry(monkeypatch, skills)
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        picker = await _open_picker(pilot)
        n = len(picker._visible_rows())
        for _ in range(n - 1):
            await pilot.press("down")
        await pilot.pause(0.1)
        assert picker.scroll_offset.y > 0
        for _ in range(n - 1):
            await pilot.press("up")
        await pilot.pause(0.1)
        assert picker.selected_skill() == skills[0]
        assert picker.scroll_offset.y == 0


@pytest.mark.asyncio
async def test_first_skill_of_group_shows_its_header(tmp_path, monkeypatch):
    """Подъём на первый скилл группы показывает и шапку над ним."""
    pytest.importorskip("textual")
    _patch_big_registry(monkeypatch, _LONG)
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        picker = await _open_picker(pilot)
        n = len(picker._visible_rows())
        for _ in range(n - 1):
            await pilot.press("down")
        for _ in range(19):  # с последнего b19 на b00 — первый в beta
            await pilot.press("up")
        await pilot.pause(0.1)
        assert picker.selected_skill()["id"] == "beta:b00"
        heads = list(picker.query(".palette-pack-head"))
        beta = next(h for h in heads if str(h.content).strip() == "beta")
        assert _on_screen(picker, beta)


@pytest.mark.asyncio
async def test_pack_header_colour_is_note_ink(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from textual.color import Color

    from gc_tui.app import TuiApp
    from gc_tui.palette import NOTE_INK

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        picker = await _open_picker(pilot)
        head = picker.query(".palette-pack-head").first()
        assert head.styles.color == Color.parse(NOTE_INK)
