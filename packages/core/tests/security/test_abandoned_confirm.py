"""Брошенное подтверждение не записывает решение в память разрешений.

На Ctrl+C перехват отменяет ожидание и отдаёт гарду «не подтвердили», а само
подтверждение живёт дальше в своём потоке: прервать блокирующее чтение нечем.
Дождавшись строки — набранной уже для СЛЕДУЮЩЕГО вопроса, — оно доигрывало
`request_confirm` до конца, включая запись решения. Одно нажатие `y`, адресованное
другому вопросу, превращалось в запомненное на сессию разрешение на путь, в
котором человек только что отказал.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from gc_core.security import confirm_port
from gc_core.security.permission_store import PermissionStore

_THREAD = "test-abandoned-confirm"


async def test_an_abandoned_confirm_records_nothing(monkeypatch):
    """Ломается, если брошенный поток снова сможет писать в память решений."""
    # Внутри теста, а не на уровне модуля: модули `tests/security/` импортируются
    # под заглушкой `gc_core.tools` (см. conftest), и взятый тогда
    # `run_abandonable_in` был бы из выброшенного экземпляра модуля — со своей,
    # чужой для `request_confirm` меткой «брошен».
    from gc_core.tools.connect_mcp.abandonable import run_abandonable_in

    release = threading.Event()

    def _late_answer(_ctx, **_kw):
        release.wait(5)  # человек ещё думает…
        return confirm_port.CONFIRM_ALLOW_REMEMBER  # …и жмёт «y» — уже не сюда

    monkeypatch.setattr(confirm_port, "_ask_choice", _late_answer)
    store = PermissionStore()
    ctx = SimpleNamespace(permission_store=store)

    waiting = asyncio.ensure_future(
        run_abandonable_in(
            threading.BoundedSemaphore(1),
            confirm_port.request_confirm,
            title="Запись",
            question="Разрешить?",
            fields=[],
            ctx=ctx,
            decision_key="write:/etc/SECRET",
            _thread_name=_THREAD,
        )
    )
    await asyncio.sleep(0.1)
    waiting.cancel()  # Ctrl+C: вопрос брошен
    with pytest.raises(asyncio.CancelledError):
        await waiting

    release.set()
    for thread in [t for t in threading.enumerate() if t.name == _THREAD]:
        thread.join(5)

    assert store.decision_for("write:/etc/SECRET") is None, "брошенное подтверждение записало решение по чужому нажатию"
