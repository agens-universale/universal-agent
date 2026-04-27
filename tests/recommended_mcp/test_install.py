"""Тесты для install.py: диспатч по install.type и platform-фильтр."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from recommended_mcp import install as install_mod
from recommended_mcp.manifest import InstallSpec, MCPEntry


@pytest.fixture(autouse=True)
def _no_real_path_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """_refresh_windows_path лезет в реальный Windows-реестр.
    Мокаем во всех тестах, чтобы не зависеть от состояния хоста."""
    monkeypatch.setattr(install_mod, "_refresh_windows_path", lambda: None)


def _entry(
    name: str = "x",
    install_type: str = "wheel",
    platforms: tuple[str, ...] = (),
) -> MCPEntry:
    return MCPEntry(
        name=name,
        path=Path(f"./{name}"),
        install=InstallSpec(type=install_type, platforms=platforms),
    )


def test_skips_when_platform_mismatch(capsys: pytest.CaptureFixture) -> None:
    entry = _entry(platforms=("linux",))
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("subprocess.run") as sub:
            install_mod.install_entry(entry, root=Path("."))
    assert not sub.called
    assert "skip" in capsys.readouterr().out.lower()


def test_runs_when_platform_matches(tmp_path: Path) -> None:
    pkg = tmp_path / "x"
    (pkg / "dist").mkdir(parents=True)
    wheel = pkg / "dist" / "x-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"fake")

    entry = _entry(platforms=("win32", "linux"))
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("subprocess.run") as sub:
            install_mod.install_entry(entry, root=tmp_path)
    sub.assert_called_once()
    cmd = sub.call_args.args[0]
    assert cmd[0] == sys.executable
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert str(wheel) in cmd


def test_wheel_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "x" / "dist").mkdir(parents=True)
    entry = _entry()
    with pytest.raises(install_mod.InstallError, match="no wheel found"):
        install_mod.install_entry(entry, root=tmp_path)


def test_wheel_multiple_raises(tmp_path: Path) -> None:
    pkg = tmp_path / "x" / "dist"
    pkg.mkdir(parents=True)
    (pkg / "x-0.1.0-py3-none-any.whl").write_bytes(b"a")
    (pkg / "x-0.2.0-py3-none-any.whl").write_bytes(b"b")
    entry = _entry()
    with pytest.raises(install_mod.InstallError, match="multiple wheels"):
        install_mod.install_entry(entry, root=tmp_path)


def test_docker_calls_compose_build(tmp_path: Path) -> None:
    pkg = tmp_path / "x"
    pkg.mkdir()
    (pkg / "docker-compose.yml").write_text("services: {}\n")
    entry = _entry(install_type="docker")
    with patch("subprocess.run") as sub:
        install_mod.install_entry(entry, root=tmp_path)
    cmd = sub.call_args.args[0]
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert cmd[-1] == "build"


def test_docker_missing_compose_raises(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    entry = _entry(install_type="docker")
    with pytest.raises(install_mod.InstallError, match="docker-compose.yml not found"):
        install_mod.install_entry(entry, root=tmp_path)


def test_unknown_type_raises_fail_fast() -> None:
    entry = _entry(install_type="npm")
    with pytest.raises(install_mod.InstallError, match="not supported"):
        install_mod.install_entry(entry, root=Path("."))


# ---------------------------------------------------------------------------
# _ensure_node: установщик Node.js per-platform. Все тесты мокают shutil.which
# и subprocess.call, реальную установку не запускают.
# ---------------------------------------------------------------------------


def _which_from(mapping: dict) -> object:
    """Фабрика для shutil.which-мока: возвращает путь из mapping по имени,
    либо None. Значения — либо путь-строка (статичный ответ), либо список
    (ответы последовательно при каждом вызове, чтобы симулировать
    'сначала нет, после install появился')."""
    iters = {k: (iter(v) if isinstance(v, list) else None) for k, v in mapping.items()}

    def which(name):
        if name in iters and iters[name] is not None:
            return next(iters[name], None)
        return mapping.get(name)

    return which


def test_ensure_node_returns_early_when_npx_present(capsys: pytest.CaptureFixture) -> None:
    with patch("recommended_mcp.install.shutil.which",
               side_effect=_which_from({"npx": "/usr/bin/npx"})):
        with patch("recommended_mcp.install.subprocess.call") as sub:
            result = install_mod._ensure_node()
    assert result == "/usr/bin/npx"
    assert not sub.called
    assert "already installed" in capsys.readouterr().out


def test_ensure_node_windows_runs_winget_and_finds_npx() -> None:
    which = _which_from({
        "npx": [None, "C:/Program Files/nodejs/npx.CMD"],  # до install / после
        "winget": "C:/winget.exe",
    })
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("recommended_mcp.install.subprocess.call",
                       return_value=0) as sub:
                install_mod._ensure_node()
    cmd = sub.call_args.args[0]
    assert cmd[0] == "winget"
    assert "OpenJS.NodeJS.LTS" in cmd
    assert "--silent" in cmd


def test_ensure_node_linux_picks_first_available_package_manager() -> None:
    # apt-get отсутствует, dnf — есть. Должен выбрать dnf и break.
    which = _which_from({
        "npx": [None, "/usr/bin/npx"],
        "apt-get": None,
        "dnf": "/usr/bin/dnf",
        "pacman": "/usr/bin/pacman",  # должен быть проигнорирован (break после dnf)
    })
    with patch.object(install_mod.sys, "platform", "linux"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("recommended_mcp.install.subprocess.call",
                       return_value=0) as sub:
                install_mod._ensure_node()
    cmd = sub.call_args.args[0]
    assert cmd[:4] == ["sudo", "dnf", "install", "-y"]
    assert "nodejs" in cmd and "npm" in cmd


def test_ensure_node_fails_when_no_installer_available() -> None:
    # macOS без brew → нет доступных установщиков → понятная ошибка.
    which = _which_from({"npx": None, "brew": None})
    with patch.object(install_mod.sys, "platform", "darwin"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("recommended_mcp.install.subprocess.call") as sub:
                with pytest.raises(install_mod.InstallError,
                                   match="Cannot auto-install"):
                    install_mod._ensure_node()
    assert not sub.called


def test_ensure_node_fails_when_installer_succeeds_but_npx_still_missing() -> None:
    # Установщик вернул 0, но PATH в текущей сессии не обновился —
    # частый Windows-gotcha. Должна быть понятная ошибка с советом.
    which = _which_from({
        "npx": [None, None],  # ни до, ни после
        "winget": "C:/winget.exe",
    })
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("recommended_mcp.install.subprocess.call", return_value=0):
                with pytest.raises(install_mod.InstallError,
                                   match="still not in PATH"):
                    install_mod._ensure_node()


def test_ensure_node_windows_falls_back_to_msi_when_no_winget() -> None:
    # Windows Sandbox / Server без App Installer: winget нет → идёт
    # по fallback-ветке с прямой установкой MSI.
    which = _which_from({
        "npx": [None, "C:/Program Files/nodejs/npx.CMD"],  # до / после
        "winget": None,
    })
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("urllib.request.urlretrieve") as retrieve:
                with patch("recommended_mcp.install.subprocess.call",
                           return_value=0) as sub:
                    result = install_mod._ensure_node()
    assert result == "C:/Program Files/nodejs/npx.CMD"
    # URL из константы, поддерживает будущий bump версии.
    retrieve.assert_called_once()
    assert retrieve.call_args.args[0] == install_mod._NODE_WIN_MSI_URL
    # msiexec вызван с /i <msi> /qn
    msi_cmd = sub.call_args.args[0]
    assert msi_cmd[0] == "msiexec" and msi_cmd[1] == "/i" and msi_cmd[3] == "/qn"


def test_ensure_node_windows_msi_uses_powershell_when_urllib_fails(
    capsys: pytest.CaptureFixture,
) -> None:
    # Embeddable Python / Server без CA-bundle: urllib падает на SSL,
    # должны откатиться на PowerShell Invoke-WebRequest.
    which = _which_from({
        "npx": [None, "C:/Program Files/nodejs/npx.CMD"],  # до / после
        "winget": None,
    })
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("urllib.request.urlretrieve",
                       side_effect=OSError("SSL: CERTIFICATE_VERIFY_FAILED")):
                with patch("recommended_mcp.install.subprocess.call",
                           return_value=0) as sub:
                    install_mod._ensure_node()
    # Должны быть вызваны: powershell для скачивания + msiexec для установки.
    assert sub.call_count == 2
    ps_cmd = sub.call_args_list[0].args[0]
    assert ps_cmd[0] == "powershell"
    assert any("Invoke-WebRequest" in arg for arg in ps_cmd)
    msi_cmd = sub.call_args_list[1].args[0]
    assert msi_cmd[0] == "msiexec"
    assert "urllib failed" in capsys.readouterr().out


def test_ensure_node_windows_msi_fails_when_both_urllib_and_powershell_fail() -> None:
    # urllib упал (SSL), PowerShell тоже упал (no network вовсе) —
    # понятная ошибка без traceback'а.
    which = _which_from({"npx": None, "winget": None})
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("urllib.request.urlretrieve",
                       side_effect=OSError("network down")):
                with patch("recommended_mcp.install.subprocess.call",
                           return_value=1) as sub:
                    with pytest.raises(install_mod.InstallError,
                                       match="Download failed"):
                        install_mod._ensure_node()
    # PowerShell должен был быть вызван — иначе fallback не сработал.
    assert sub.call_args.args[0][0] == "powershell"


def test_ensure_node_windows_msi_fallback_fails_when_msiexec_nonzero() -> None:
    # msiexec вернул ошибку (например, нет admin-прав на system-wide install).
    # urllib работает, PowerShell-fallback не нужен — subprocess.call
    # вызывается только для msiexec.
    which = _which_from({"npx": None, "winget": None})
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("urllib.request.urlretrieve"):
                with patch("recommended_mcp.install.subprocess.call",
                           return_value=1603):  # типичный код msiexec fatal
                    with pytest.raises(install_mod.InstallError,
                                       match="msiexec failed"):
                        install_mod._ensure_node()


def test_ensure_node_windows_calls_path_refresh_after_msi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # После MSI-fallback PATH в os.environ должен обновляться через
    # _refresh_windows_path, иначе shutil.which не найдёт свеже-
    # поставленный npx и установка завершится Windows PATH-gotcha.
    refresh_calls: list[None] = []
    monkeypatch.setattr(
        install_mod, "_refresh_windows_path",
        lambda: refresh_calls.append(None),
    )
    which = _which_from({
        "npx": [None, "C:/Program Files/nodejs/npx.CMD"],
        "winget": None,
    })
    with patch.object(install_mod.sys, "platform", "win32"):
        with patch("recommended_mcp.install.shutil.which", side_effect=which):
            with patch("urllib.request.urlretrieve"):
                with patch("recommended_mcp.install.subprocess.call",
                           return_value=0):
                    install_mod._ensure_node()
    assert refresh_calls, "_refresh_windows_path должен быть вызван после MSI"
