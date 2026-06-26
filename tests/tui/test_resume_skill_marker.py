"""Проверяет, что [skill]-маркер монтируется ровно один раз:
- на resume/rebuild пути (live=False) — _render_record монтирует маркер;
- на live-пути (live=True)  — _render_record НЕ монтирует маркер
  (он уже смонтирован delivery-флоу через _mount_user_reply → _flush_skill_marker).
"""
import pytest


def _user_rec_with_skill(skill_id: str = "qa-x") -> dict:
    """Запись-пользователь с skill_loaded — как сохраняется в .jsonl."""
    return {
        "payload": {
            "role": "user",
            "content": "просто запрос",
            "display_content": "просто запрос",
            "skill_loaded": skill_id,
        },
        "seq": 1,
        "ts": "2026-06-26T00:00:00+00:00",
    }


def _count_skill_markers(scr) -> int:
    """Считает [skill]-маркеры в #trace (line-meta-строки с 'загружен')."""
    from textual.widgets import Static
    texts = [str(w.render()) for w in scr.query("#trace Static")]
    return sum(1 for t in texts if "загружен" in t)


@pytest.mark.asyncio
async def test_resume_path_mounts_exactly_one_marker(tmp_path):
    """На rebuild (live=False) _render_record монтирует [skill]-маркер."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records([_user_rec_with_skill("qa-x")], live=False)
        await pilot.pause()
        assert _count_skill_markers(scr) == 1, (
            "На resume-пути должен быть ровно один [skill]-маркер"
        )


@pytest.mark.asyncio
async def test_live_path_mounts_no_marker(tmp_path):
    """На live-пути (live=True) _render_record НЕ монтирует [skill]-маркер
    (delivery-флоу уже смонтировал его сам)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records([_user_rec_with_skill("qa-x")], live=True)
        await pilot.pause()
        assert _count_skill_markers(scr) == 0, (
            "На live-пути _render_record не должен монтировать [skill]-маркер"
        )
