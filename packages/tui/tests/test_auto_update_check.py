"""Авто-проверка обновления при старте TUI (спека D; персист-вердикт).

Ядро — `_maybe_auto_check(config, state, now, installed_commit, *, check, record_result,
on_available)`: персистентный вердикт (КАЖДЫЙ старт, без сети: сохранённый remote ≠
установленный → on_available(False)) + свежая сетевая перепроверка на КАЖДОМ запуске
(on_available(True) при найденном обновлении). Гейты: resolved_source None / check: off
→ сеть не трогаем; персист-вердикт от них НЕ зависит."""

import pytest

pytest.importorskip("textual")

from gc_core.updater import source as source_mod
from gc_core.updater.check import CheckResult
from gc_core.updater.source import UpdateSource
from gc_tui import app as app_mod


def _src(commit="old", ref="master"):
    return UpdateSource(url="https://x/y", ref=ref, commit=commit)


def _call(config, state, *, now=1000.0, installed="inst", check=lambda src: CheckResult("update", "new", "d")):
    """Прогон ядра с инъекцией; возвращает счётчики check / record / on_available."""
    calls = {"check": 0, "record": [], "avail": []}

    def _check(src):
        calls["check"] += 1
        return check(src)

    app_mod._maybe_auto_check(
        config,
        state,
        now,
        installed,
        check=_check,
        record_result=lambda ts, rc, inst: calls["record"].append((ts, rc, inst)),
        on_available=lambda fresh: calls["avail"].append(fresh),
    )
    return calls


def test_resolved_source_none_no_check(monkeypatch):
    monkeypatch.setattr(source_mod, "resolved_source", lambda: None)
    calls = _call({}, {})
    assert calls["check"] == 0 and calls["avail"] == []


def test_auto_check_off_no_fresh_check(monkeypatch):
    """check: off — свежая перепроверка не идёт; персист-вердикт (пустой стейт) молчит."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    calls = _call({"update": {"check": "off"}}, {})
    assert calls["check"] == 0 and calls["avail"] == []


def test_fresh_check_on_every_launch(monkeypatch):
    """Троттла нет: свежий стамп не мешает — при check: auto проверяем каждый запуск."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    calls = _call({}, {"last_update_check_ts": 1000.0}, now=1000.0 + 60.0)
    assert calls["check"] == 1


def test_due_and_update_records_remote_and_available_fresh(monkeypatch):
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    calls = _call({}, {}, check=lambda src: CheckResult("update", "new", "доступна новая версия"))
    assert calls["check"] == 1
    assert calls["record"] == [(1000.0, "new", "inst")]
    assert calls["avail"] == [True]  # свежая находка → toast разрешён


def test_due_and_current_records_remote_no_available(monkeypatch):
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    calls = _call({}, {}, check=lambda src: CheckResult("current", "same", "актуально"))
    assert calls["record"] == [(1000.0, "same", "inst")]
    assert calls["avail"] == []


def test_check_error_records_ts_only_no_available(monkeypatch):
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())

    def _boom(src):
        raise RuntimeError("network down")

    calls = _call({}, {}, check=_boom)
    assert calls["record"] == [(1000.0, None, "inst")]  # только стамп; прошлый remote не трогаем
    assert calls["avail"] == []


def test_on_result_publishes_verdict(monkeypatch):
    """Ядро отдаёт вердикт наружу (`on_result`) — из него живут глиф хаба и секция,
    которой не нужно повторно ходить в сеть."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    seen = []
    app_mod._maybe_auto_check(
        {},
        {},
        1000.0,
        "inst",
        check=lambda src: CheckResult("current", "same", "установлена последняя версия"),
        record_result=lambda *a: None,
        on_available=lambda fresh: None,
        on_result=seen.append,
    )
    assert [r.status for r in seen] == ["current"]


def test_on_result_unknown_when_check_fails(monkeypatch):
    """Сеть упала — наружу уходит «unknown» (нет вердикта), а не тишина."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    seen = []

    def _boom(src):
        raise RuntimeError("network down")

    app_mod._maybe_auto_check(
        {},
        {},
        1000.0,
        "inst",
        check=_boom,
        record_result=lambda *a: None,
        on_available=lambda fresh: None,
        on_result=seen.append,
    )
    assert [r.status for r in seen] == ["unknown"]


def test_on_result_silent_when_check_off(monkeypatch):
    """check: off — в сеть не идём, вердикта нет."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    seen = []
    app_mod._maybe_auto_check(
        {"update": {"check": "off"}},
        {},
        1000.0,
        "inst",
        check=lambda src: CheckResult("update", "new", "d"),
        record_result=lambda *a: None,
        on_available=lambda fresh: None,
        on_result=seen.append,
    )
    assert seen == []


def test_unknown_records_ts_only_no_available(monkeypatch):
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    calls = _call({}, {}, check=lambda src: CheckResult("unknown", None, "не удалось"))
    assert calls["record"] == [(1000.0, None, "inst")]
    assert calls["avail"] == []


def test_persisted_verdict_available_every_launch_no_toast(monkeypatch):
    """КЛЮЧЕВОЕ: сохранённый remote ≠ установленный → on_available(False) на КАЖДОМ старте,
    без сети (тут её нет — свежий чек вернул unknown); строка есть, toast'а нет."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    state = {"last_update_check_ts": 1000.0, "last_update_remote_commit": "new", "last_update_checked_installed": "old"}
    calls = _call(
        {},
        state,
        now=1000.0 + 3600.0,
        installed="old",
        check=lambda src: CheckResult("unknown", None, "нет соединения"),
    )
    assert calls["avail"] == [False]  # строка показана (персист, без toast)


def test_persisted_verdict_none_when_installed_matches_remote(monkeypatch):
    """После обновления (installed == сохранённый remote) вердикт гаснет — строки нет."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    state = {
        "last_update_check_ts": 1000.0,
        "last_update_remote_commit": "same",
        "last_update_checked_installed": "same",
    }
    calls = _call(
        {}, state, now=1000.0 + 3600.0, installed="same", check=lambda src: CheckResult("current", "same", "актуально")
    )
    assert calls["avail"] == []


def test_persisted_plus_fresh_update(monkeypatch):
    """Персист-вердикт (строка сразу) + due → свежий чек тоже находит: on_available(False), затем True."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: _src())
    state = {"last_update_remote_commit": "new", "last_update_checked_installed": "old"}  # нет ts → due
    calls = _call({}, state, installed="old", check=lambda src: CheckResult("update", "newer", "d"))
    assert calls["avail"] == [False, True]
    assert calls["record"] == [(1000.0, "newer", "old")]


# --- best-effort обёртка: воркер НИКОГДА не роняет старт ---


def test_run_auto_check_safely_swallows_exception(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("blew up")

    monkeypatch.setattr(app_mod, "_maybe_auto_check", _boom)
    app_mod._run_auto_check_safely(
        {},
        {},
        1000.0,
        "inst",
        check=lambda src: None,
        record_result=lambda ts, rc, inst: None,
        on_available=lambda fresh: None,
    )


def test_run_auto_check_safely_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        app_mod,
        "_maybe_auto_check",
        lambda config, state, now, installed, *, check, record_result, on_available, on_result=None: seen.update(
            now=now, installed=installed, check=check
        ),
    )
    app_mod._run_auto_check_safely(
        {},
        {},
        42.0,
        "COMMIT",
        check="C",
        record_result=lambda ts, rc, inst: None,
        on_available=lambda fresh: None,
    )
    assert seen == {"now": 42.0, "installed": "COMMIT", "check": "C"}


@pytest.mark.asyncio
async def test_on_mount_starts_auto_check_worker(tmp_path, monkeypatch):
    """on_mount не падает и планирует авто-чек воркер (resolved_source None → no-op)."""
    monkeypatch.setattr(source_mod, "resolved_source", lambda: None)
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
