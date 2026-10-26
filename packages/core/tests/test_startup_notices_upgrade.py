"""Notice «агент обновлён» в канале startup_notices."""

from gc_core.startup_notices import collect_startup_notices
from gc_core.upgrade_gate import UpgradeInfo
from gc_testkit.settings import make_settings


def _collect(upgrade_info):
    return collect_startup_notices(
        make_settings(), set(), None, is_tui=False, upgrade_info=upgrade_info
    )


def test_upgrade_notice_rendered():
    notices = _collect(UpgradeInfo(old_version="0.2.0", new_version="0.3.0"))
    assert any(
        n.code == "agent_upgraded"
        and "0.2.0" in n.message
        and "0.3.0" in n.message
        for n in notices
    )


def test_upgrade_notice_without_old_version():
    notices = _collect(UpgradeInfo(old_version=None, new_version="0.3.0"))
    msgs = [n.message for n in notices if n.code == "agent_upgraded"]
    assert msgs and "0.3.0" in msgs[0] and "None" not in msgs[0]


def test_no_upgrade_no_notice():
    assert not [n for n in _collect(None) if n.code == "agent_upgraded"]
