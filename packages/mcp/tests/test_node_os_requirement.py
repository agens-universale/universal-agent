"""Node агента не качается туда, где она не запустится: на macOS ниже
13.5 Node 24 не стартует, и каждый повтор установки качал бы архив заново."""

import pytest

from recommended_mcp import install as inst


class _Reached(Exception):
    """Дошли до загрузки архива — проверка ОС пропустила."""


def _reached(*_args, **_kwargs):
    raise _Reached


def _macos(monkeypatch, release: str) -> None:
    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(
        inst.platform, "mac_ver", lambda: (release, ("", "", ""), "")
    )


@pytest.mark.parametrize("release", ["12.7.4", "13.4.1", "13"])
def test_old_macos_refuses_before_download(monkeypatch, tmp_path, release):
    _macos(monkeypatch, release)
    monkeypatch.setattr(inst, "_fetch_expected_sha256", _reached)
    with pytest.raises(inst.InstallError, match=r"macOS 13\.5"):
        inst._ensure_cached_node(tmp_path)


@pytest.mark.parametrize("release", ["13.5", "14.2.1", ""])
def test_supported_or_unknown_macos_goes_on(monkeypatch, tmp_path, release):
    _macos(monkeypatch, release)
    monkeypatch.setattr(inst, "_fetch_expected_sha256", _reached)
    with pytest.raises(_Reached):
        inst._ensure_cached_node(tmp_path)


def test_other_os_is_not_checked(monkeypatch, tmp_path):
    # Старая «версия macOS» под рукой: проверка, сработай она не на той
    # ОС, отказала бы раньше загрузки.
    monkeypatch.setattr(inst.sys, "platform", "linux")
    monkeypatch.setattr(
        inst.platform, "mac_ver", lambda: ("10.15", ("", "", ""), "")
    )
    monkeypatch.setattr(inst, "_fetch_expected_sha256", _reached)
    with pytest.raises(_Reached):
        inst._ensure_cached_node(tmp_path)
