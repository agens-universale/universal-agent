"""Резюм треда: скилл — именем в реплике (спека P3 §2.3, §10 п. 5).

Имя — запускаемый вид, как в палитре: короткое, если ведёт ровно к этому
скиллу и не занято командой, иначе полный id; холодный кэш — короткое.
Строки `[skill]` нет ни на резюме, ни вживую.
"""

from gc_testkit.tui_support import tui_settings
import pytest


def _user_rec_with_skill(skill_id: str, text: str = "просто запрос") -> dict:
    """Запись-пользователь с skill_loaded — как сохраняется в .jsonl."""
    return {
        "payload": {
            "role": "user",
            "content": "<skill>\n" + text,
            "display_content": text,
            "skill_loaded": skill_id,
        },
        "seq": 1,
        "ts": "2026-06-26T00:00:00+00:00",
    }


def _replies(scr) -> list[str]:
    out = []
    for w in scr.query("#trace UserReplyContainer"):
        line = w._reply_lines(80)[0]
        out.append((line.prefix + line.body).plain)
    return out


def _trace_has(scr, needle: str) -> bool:
    return any(needle in str(w.render()) for w in scr.query("#trace Static"))


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_skill_reply_uses_short_name(tmp_path, live):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "supercoder:brainstorming"}]
        rec = _user_rec_with_skill("supercoder:brainstorming", "придумай")
        scr._render_records([rec], live=live)
        await pilot.pause()
        assert _replies(scr) == ["› /brainstorming придумай"]
        assert not _trace_has(scr, "загружен")


@pytest.mark.asyncio
async def test_ambiguous_short_name_shows_full_id(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "a:dup"}, {"id": "b:dup"}]
        scr._render_records([_user_rec_with_skill("a:dup", "x")])
        await pilot.pause()
        assert _replies(scr) == ["› /a:dup x"]


@pytest.mark.asyncio
async def test_cold_cache_shows_short_name(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = None
        scr._render_records([_user_rec_with_skill("a:dup", "x")])
        await pilot.pause()
        assert _replies(scr) == ["› /dup x"]


@pytest.mark.asyncio
async def test_midrun_delivery_resume_is_skill_only_reply(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "qa-x"}]
        scr._render_records([_user_rec_with_skill("qa-x", "")])
        await pilot.pause()
        assert _replies(scr) == ["› /qa-x"]
        assert not _trace_has(scr, "загружен")
