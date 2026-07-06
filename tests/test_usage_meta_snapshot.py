"""AgentExecutor.usage_meta_snapshot: (session_meta, last_run_meta|None) | None.

Читает под замком ``state.usage`` / ``state.last_run_usage`` и отдаёт их to_meta()-
снимки для TUI-футера. Без state → None.
"""
from types import SimpleNamespace

from agent_executor import AgentExecutor
from usage.account import CATEGORY_MAIN, UsageAccount


def _executor_with_state(usage: UsageAccount, last_run):
    ex = AgentExecutor(SimpleNamespace())
    ex._state = SimpleNamespace(usage=usage, last_run_usage=last_run)
    return ex


def test_none_when_no_state():
    ex = AgentExecutor(SimpleNamespace())
    assert ex.usage_meta_snapshot() is None


def test_session_and_no_last_run():
    session = UsageAccount()
    session.add(CATEGORY_MAIN, 100, 20)
    ex = _executor_with_state(session, None)

    snap = ex.usage_meta_snapshot()
    assert snap is not None
    session_meta, last_run_meta = snap
    assert last_run_meta is None
    assert UsageAccount.from_meta(session_meta).totals()["input"] == 100


def test_session_and_last_run():
    session = UsageAccount()
    session.add(CATEGORY_MAIN, 100, 20)
    last_run = UsageAccount()
    last_run.add(CATEGORY_MAIN, 40, 5)
    ex = _executor_with_state(session, last_run)

    session_meta, last_run_meta = ex.usage_meta_snapshot()
    assert UsageAccount.from_meta(session_meta).totals()["output"] == 20
    assert UsageAccount.from_meta(last_run_meta).totals()["input"] == 40


def test_live_delta_while_run_active():
    """Ход идёт (last_run_usage ещё None): «запрос» = усия − baseline старта хода."""
    session = UsageAccount()
    session.add(CATEGORY_MAIN, 100, 20)          # кумулятив на текущий момент
    baseline = UsageAccount()
    baseline.add(CATEGORY_MAIN, 60, 10)          # снимок на старте хода (прошлые ходы)
    ex = AgentExecutor(SimpleNamespace())
    ex._state = SimpleNamespace(
        usage=session, last_run_usage=None,
        run_start_usage_meta=baseline.to_meta(),
    )

    session_meta, request_meta = ex.usage_meta_snapshot()
    req = UsageAccount.from_meta(request_meta)
    assert req.by_category[CATEGORY_MAIN]["input"] == 40   # 100 − 60, вживую
    assert req.by_category[CATEGORY_MAIN]["output"] == 10  # 20 − 10
    assert UsageAccount.from_meta(session_meta).totals()["input"] == 100


def test_last_run_takes_precedence_when_set():
    """Ход завершён (last_run_usage выставлен) — показываем его, не live-дельту."""
    session = UsageAccount(); session.add(CATEGORY_MAIN, 100, 20)
    last_run = UsageAccount(); last_run.add(CATEGORY_MAIN, 40, 5)
    baseline = UsageAccount(); baseline.add(CATEGORY_MAIN, 60, 10)
    ex = AgentExecutor(SimpleNamespace())
    ex._state = SimpleNamespace(
        usage=session, last_run_usage=last_run,
        run_start_usage_meta=baseline.to_meta(),
    )
    _, request_meta = ex.usage_meta_snapshot()
    assert UsageAccount.from_meta(request_meta).by_category[CATEGORY_MAIN]["input"] == 40
    assert UsageAccount.from_meta(request_meta).by_category[CATEGORY_MAIN]["output"] == 5
