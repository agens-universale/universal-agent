"""Охранники команд чужого профиля: подсказка вместо трейсбека.

`uv tool install` кладёт в PATH ВСЕ пять команд дистрибутива, поэтому в
headless-профиле `gc-cli` присутствуют и `gc-ua`, и `gc-serve`, хотя пакетов
`gc_tui` / `gc_server` там физически нет. Шимы в ядре обязаны поймать это и
напечатать одну строку с командой установки.

Дыра, ради которой заведён файл: до разбиения на пакеты модули звались `tui` и
`server`, и множества имён в шимах остались со старыми значениями. Охранник
перестал узнавать собственный случай — пользователь профиля gc-cli получал
`ModuleNotFoundError: No module named 'gc_tui'` с трейсбеком (проверено вживую
18.08.2026). Существующий тест TUI это не ловил: он подменяет `textual`, то
есть случай «пакет есть, зависимости нет», а не «пакета нет вовсе».
"""

from __future__ import annotations

import builtins

import pytest

from gc_core import app_version, serve_entry, tui_entry


def _hide_package(monkeypatch, package: str) -> None:
    """Сделать вид, что верхнеуровневого пакета нет в окружении."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == package or name.startswith(package + "."):
            raise ModuleNotFoundError(f"No module named '{package}'", name=package)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


@pytest.mark.parametrize(
    "entry, package, dist",
    [
        (tui_entry, "gc_tui", "gc-tui"),
        (serve_entry, "gc_server", "gc-server"),
    ],
)
def test_missing_profile_prints_hint_without_traceback(
    monkeypatch, capsys, entry, package, dist
):
    _hide_package(monkeypatch, package)

    rc = entry.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert err.count("\n") <= 1, "подсказка обязана оставаться однострочной"
    assert dist in err
    assert app_version.PROFILE_SUBDIRS[dist] in err, (
        "в подсказке должен стоять реальный подкаталог манифеста"
    )


@pytest.mark.parametrize("entry", [tui_entry, serve_entry])
def test_unrelated_import_error_is_not_masked(monkeypatch, entry):
    """Чужая поломка должна лететь трейсбеком, а не превращаться в «поставьте профиль»."""
    _hide_package(monkeypatch, "some_unrelated_dependency")

    def boom(*_args, **_kwargs):
        raise ModuleNotFoundError(
            "No module named 'some_unrelated_dependency'",
            name="some_unrelated_dependency",
        )

    monkeypatch.setattr(builtins, "__import__", boom)
    with pytest.raises(ModuleNotFoundError):
        entry.main()


def test_install_hint_uses_real_subdirs():
    """Подсказка собирается из PROFILE_SUBDIRS, а не из строки рядом с текстом.

    Именно расхождение этих двух источников и отправляло пользователя в
    несуществующий каталог `packaging/`.
    """
    for dist, subdir in app_version.PROFILE_SUBDIRS.items():
        hint = app_version.install_hint(dist)
        assert f"#subdirectory={subdir}" in hint
        assert hint.startswith("uv tool install")
        assert "\n" not in hint


def test_no_stale_packaging_dir_in_hints():
    """Каталога packaging/ не существует — ни одна подсказка не должна его называть."""
    for dist in app_version.PROFILE_SUBDIRS:
        assert "packaging/" not in app_version.install_hint(dist)
    assert "packaging/" not in tui_entry._MISSING_TUI_MSG
    assert "packaging/" not in serve_entry._MISSING_SERVER_MSG
