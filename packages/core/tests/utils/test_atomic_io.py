
import pytest
from gc_core.utils.atomic_io import atomic_replace


def _winerr(code):
    exc = PermissionError("Access is denied")
    exc.winerror = code
    return exc


def test_success_replaces_file(tmp_path):
    src = tmp_path / "a.tmp"
    dst = tmp_path / "a.json"
    dst.write_text("old", encoding="utf-8")
    src.write_text("new", encoding="utf-8")

    atomic_replace(str(src), str(dst))

    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_retries_transient_winerror_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def _fake_replace(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _winerr(5)  # ACCESS_DENIED дважды, затем успех

    monkeypatch.setattr("gc_core.utils.atomic_io.os.replace", _fake_replace)
    monkeypatch.setattr("gc_core.utils.atomic_io.time.sleep", lambda *_a, **_k: None)

    atomic_replace("src", "dst")
    assert calls["n"] == 3


def test_sharing_violation_also_retried(monkeypatch):
    calls = {"n": 0}

    def _fake_replace(a, b):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _winerr(32)  # SHARING_VIOLATION

    monkeypatch.setattr("gc_core.utils.atomic_io.os.replace", _fake_replace)
    monkeypatch.setattr("gc_core.utils.atomic_io.time.sleep", lambda *_a, **_k: None)

    atomic_replace("src", "dst")
    assert calls["n"] == 2


def test_persistent_transient_error_raises_after_bounded_attempts(monkeypatch):
    calls = {"n": 0}

    def _fake_replace(a, b):
        calls["n"] += 1
        raise _winerr(5)

    monkeypatch.setattr("gc_core.utils.atomic_io.os.replace", _fake_replace)
    monkeypatch.setattr("gc_core.utils.atomic_io.time.sleep", lambda *_a, **_k: None)

    with pytest.raises(PermissionError):
        atomic_replace("src", "dst", attempts=4)
    assert calls["n"] == 4  # ровно attempts попыток, затем re-raise


def test_non_windows_error_raises_immediately(monkeypatch):
    calls = {"n": 0}

    def _fake_replace(a, b):
        calls["n"] += 1
        raise OSError("ENOENT")  # winerror отсутствует → POSIX-путь, без ретрая

    monkeypatch.setattr("gc_core.utils.atomic_io.os.replace", _fake_replace)
    monkeypatch.setattr("gc_core.utils.atomic_io.time.sleep", lambda *_a, **_k: None)

    with pytest.raises(OSError):
        atomic_replace("src", "dst")
    assert calls["n"] == 1  # без ретрая
