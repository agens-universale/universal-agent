from updater.source import UpdateSource
from updater.check import _git_ls_remote, check_for_update

def _src(commit="old", ref="master"):
    return UpdateSource(url="https://x/y", ref=ref, commit=commit)


def test_update_available_when_remote_differs():
    r = check_for_update(_src(), ls_remote=lambda url, ref: "newcommit")
    assert r.status == "update" and r.remote_commit == "newcommit"


def test_current_when_remote_same():
    r = check_for_update(_src(commit="same"), ls_remote=lambda url, ref: "same")
    assert r.status == "current"


def test_unknown_on_network_none():
    r = check_for_update(_src(), ls_remote=lambda url, ref: None)
    assert r.status == "unknown"


def test_default_branch_uses_head():
    seen = {}
    def _ls(url, ref):
        seen["ref"] = ref
        return "c"
    check_for_update(_src(ref=None), ls_remote=_ls)
    assert seen["ref"] == "HEAD"   # ref отсутствует → проверяем HEAD дефолтной ветки


def test_git_ls_remote_preserves_console_mode(monkeypatch):
    """git ls-remote — дочерний процесс; на Windows он сбрасывает режим ввода
    родительской консоли (см. utils/win_console.py) — без обёртки Ctrl+C после
    проверки обновлений превращается в системный break-сигнал вместо Key-события
    (см. tools/backends/local.py, где exec()/run_python() уже так обёрнуты)."""
    import updater.check as check_mod
    import utils.win_console as wc

    calls = []

    class _FakeCM:
        def __enter__(self):
            calls.append("enter")

        def __exit__(self, *a):
            calls.append("exit")
            return False

    monkeypatch.setattr(wc, "preserve_console_mode", lambda: _FakeCM())

    class _Result:
        returncode = 0
        stdout = "abc123\trefs/heads/master\n"

    def _fake_run(*a, **kw):
        assert calls == ["enter"]   # subprocess вызван внутри preserve_console_mode
        return _Result()

    monkeypatch.setattr(check_mod.subprocess, "run", _fake_run)
    result = _git_ls_remote("https://x/y", "master")
    assert result == "abc123"
    assert calls == ["enter", "exit"]


def test_update_available_from_state():
    from updater.check import update_available_from_state
    st = {"last_update_remote_commit": "new", "last_update_checked_installed": "old"}
    assert update_available_from_state(st, "old") is True             # проверяли на old, remote new → доступно
    assert update_available_from_state(st, "new") is False            # обновился до new (== remote) → нет
    assert update_available_from_state(st, "newer") is False          # обновился ДАЛЬШЕ (checked!=installed) → гаснет
    assert update_available_from_state({}, "old") is False            # нет сохранённого remote
    assert update_available_from_state(st, None) is False             # нет installed


def test_record_check_result_persists_and_preserves_on_none(tmp_path):
    import json
    from updater.check import record_check_result
    p = tmp_path / "state.json"
    record_check_result(1000.0, "abc", "inst", p)
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["last_update_check_ts"] == 1000.0 and d["last_update_remote_commit"] == "abc"
    assert d["last_update_checked_installed"] == "inst"
    record_check_result(2000.0, None, "inst", p)     # unknown/ошибка: стамп обновить, remote/checked сохранить
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["last_update_check_ts"] == 2000.0 and d["last_update_remote_commit"] == "abc"
    assert d["last_update_checked_installed"] == "inst"
