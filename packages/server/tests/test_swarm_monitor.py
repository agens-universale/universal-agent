"""Тесты для src/swarm/monitor.py.

Покрывает:
- register/unregister/get_active_count/get_active_ids_copy;
- _check_one_swarm — делегирует в lifecycle.check_and_finalize (мокаем);
- _run_loop — фоновый цикл с прерыванием через stop_event;
- thread-safety поверхностно (несколько register/unregister без race);
- start_in_background + stop.
"""

import threading
from unittest.mock import MagicMock

from gc_server.swarm.monitor import SwarmMonitor

# ---------------------------------------------------------------------------
# Active set API
# ---------------------------------------------------------------------------


def test_register_increments_count():
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    assert m.get_active_count() == 0
    m.register_active("g_t1")
    assert m.get_active_count() == 1
    m.register_active("g_t2")
    assert m.get_active_count() == 2


def test_register_same_id_idempotent():
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.register_active("dup")
    m.register_active("dup")
    assert m.get_active_count() == 1


def test_unregister_decrements():
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.register_active("a")
    m.register_active("b")
    m.unregister_active("a")
    assert m.get_active_count() == 1


def test_unregister_unknown_id_does_not_raise():
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.unregister_active("no_such_id")  # set.discard — silent


def test_get_active_ids_copy_returns_snapshot():
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.register_active("a")
    m.register_active("b")
    snap = m.get_active_ids_copy()
    assert snap == {"a", "b"}
    # Мутация снапшота не должна влиять на monitor.
    snap.add("c")
    assert m.get_active_count() == 2


def test_concurrent_register_unregister_thread_safe():
    """Десятки потоков пишут и стирают одновременно — нет race."""
    m = SwarmMonitor("/tmp/th", "/tmp/sb")

    def worker(idx):
        sid = f"id_{idx}"
        m.register_active(sid)
        m.unregister_active(sid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Все unregister выполнились → count = 0.
    assert m.get_active_count() == 0


# ---------------------------------------------------------------------------
# _check_one_swarm — делегирует в lifecycle (мокаем)
# ---------------------------------------------------------------------------


def test_check_one_swarm_unregisters_when_meta_missing(monkeypatch):
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.register_active("ghost")
    monkeypatch.setattr(
        "gc_server.swarm.monitor.lifecycle.check_and_finalize",
        lambda *a, **kw: {},  # пустой meta
    )
    m._check_one_swarm("ghost")
    assert m.get_active_count() == 0


def test_check_one_swarm_unregisters_when_status_final(monkeypatch):
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.register_active("done")
    monkeypatch.setattr(
        "gc_server.swarm.monitor.lifecycle.check_and_finalize",
        lambda *a, **kw: {"swarm_meta": {"status": "completed"}},
    )
    m._check_one_swarm("done")
    assert m.get_active_count() == 0


def test_check_one_swarm_keeps_active_when_running(monkeypatch):
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.register_active("live")
    monkeypatch.setattr(
        "gc_server.swarm.monitor.lifecycle.check_and_finalize",
        lambda *a, **kw: {"swarm_meta": {"status": "running"}},
    )
    m._check_one_swarm("live")
    assert m.get_active_count() == 1


def test_check_one_swarm_swallows_exceptions(monkeypatch, caplog):
    """Падение в lifecycle не должно убивать loop."""
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.register_active("err")

    def boom(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(
        "gc_server.swarm.monitor.lifecycle.check_and_finalize", boom
    )
    # Не должно бросить наверх.
    m._check_one_swarm("err")
    # active_count не изменился (ошибка → ранний return).
    assert m.get_active_count() == 1


def test_check_one_swarm_passes_correct_args(monkeypatch):
    """Проверить, что check_and_finalize вызвался с правильными args.

    Monitor loop использует heavy_checks=True для enforcement ресурсов
    (шаги 2.2-2.4).
    """
    m = SwarmMonitor("/threads", "/sandbox")
    m.register_active("xxx")
    mock_finalize = MagicMock(
        return_value={"swarm_meta": {"status": "running"}}
    )
    monkeypatch.setattr(
        "gc_server.swarm.monitor.lifecycle.check_and_finalize", mock_finalize
    )
    m._check_one_swarm("xxx")
    mock_finalize.assert_called_once_with(
        "/threads",
        "/sandbox",
        "xxx",
        heavy_checks=True,
        swarm_logs_dir="",
    )


# ---------------------------------------------------------------------------
# _run_loop — фоновой
# ---------------------------------------------------------------------------


def test_run_loop_exits_on_stop_event(monkeypatch):
    """stop() должен прерывать loop за < 1с (через stop_event.wait)."""
    m = SwarmMonitor("/tmp/th", "/tmp/sb", check_interval_seconds=60)
    # Заменяем check_one_swarm на no-op, чтобы не дёргать lifecycle.
    monkeypatch.setattr(m, "_check_one_swarm", lambda sid: None)

    m.start_in_background()
    # Дать loop'у возможность стартовать
    import time

    time.sleep(0.05)
    t0 = time.time()
    m.stop()
    elapsed = time.time() - t0
    assert elapsed < 1.5, f"stop() заняло {elapsed:.2f}s — должно быть < 1.5s"


def test_run_loop_invokes_check_for_each_active(monkeypatch):
    """В одной итерации loop'а — _check_one_swarm для каждого active id."""
    m = SwarmMonitor("/tmp/th", "/tmp/sb", check_interval_seconds=0)
    m.register_active("a")
    m.register_active("b")
    m.register_active("c")

    called = []
    monkeypatch.setattr(m, "_check_one_swarm", lambda sid: called.append(sid))

    # Запустить _run_loop в потоке, дать одну итерацию, потом stop.
    t = threading.Thread(target=m._run_loop, daemon=True)
    t.start()

    import time

    # check_interval_seconds=0 → wait(0) сразу возвращает False, loop крутится.
    # Дадим 100ms на несколько итераций.
    time.sleep(0.1)
    m.stop()
    t.join(timeout=1.0)

    # Все три должны побывать в called (как минимум по разу).
    assert set(called) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# start_in_background / stop
# ---------------------------------------------------------------------------


def test_start_in_background_idempotent():
    """Повторный start не должен создавать второй поток."""
    m = SwarmMonitor("/tmp/th", "/tmp/sb", check_interval_seconds=60)
    m.start_in_background()
    first_thread = m._thread
    m.start_in_background()
    assert m._thread is first_thread
    m.stop()


def test_stop_when_not_started_does_not_raise():
    m = SwarmMonitor("/tmp/th", "/tmp/sb")
    m.stop()  # должно быть no-op


def test_constructor_default_sandbox_root_dir_is_empty():
    """Совместимость со старым кодом, который не передавал sandbox_root_dir."""
    m = SwarmMonitor("/tmp/th")
    assert m._sandbox_root_dir == ""
