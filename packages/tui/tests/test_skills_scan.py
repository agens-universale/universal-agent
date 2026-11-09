"""Список скиллов вне цикла интерфейса (services/skills_scan)."""

import asyncio
import threading
import time

import pytest


class _Reg:
    def __init__(self, entries=None, exc=None, block_s=0.0):
        self.entries = entries or []
        self.exc = exc
        self.block_s = block_s
        self.thread = None

    async def list_skills(self, *, pack=None, include_all=False):
        self.thread = threading.current_thread()
        time.sleep(self.block_s)
        if self.exc is not None:
            raise self.exc
        return list(self.entries)


def _patch(monkeypatch, reg):
    from gc_core.tools.skills import registry as reg_mod

    monkeypatch.setattr(reg_mod, "get_skills_registry", lambda cfg=None: reg)


@pytest.mark.asyncio
async def test_scan_runs_off_the_calling_thread(monkeypatch):
    from gc_tui.services.skills_scan import list_skills_off_loop

    reg = _Reg([{"id": "a"}])
    _patch(monkeypatch, reg)
    assert await list_skills_off_loop(None) == [{"id": "a"}]
    assert reg.thread is not threading.current_thread()


@pytest.mark.asyncio
async def test_scan_error_reaches_caller(monkeypatch):
    from gc_tui.services.skills_scan import list_skills_off_loop

    _patch(monkeypatch, _Reg(exc=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await list_skills_off_loop(None)


@pytest.mark.asyncio
async def test_cancelled_waiter_drops_late_result(monkeypatch):
    from gc_tui.services.skills_scan import list_skills_off_loop

    _patch(monkeypatch, _Reg([{"id": "late"}], block_s=0.2))
    task = asyncio.ensure_future(list_skills_off_loop(None))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)  # поздний результат не роняет цикл
