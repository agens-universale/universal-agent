"""Light-switch: первый включает, последний выключает; мёртвые lease прибираются."""
import voice_runtime.leases as leases


def _patch_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("voice_runtime.leases.voice_leases_dir", lambda: tmp_path)


def test_acquire_release_last_one_off(monkeypatch, tmp_path):
    _patch_dir(monkeypatch, tmp_path)
    alive = lambda pid: pid in (100, 200)
    leases.acquire(100)
    leases.acquire(200)
    assert sorted(leases.live_leases(alive=alive)) == [100, 200]
    assert leases.release(100, alive=alive) is False   # 200 ещё жив
    assert leases.release(200, alive=alive) is True    # последний — гасит


def test_dead_leases_cleaned(monkeypatch, tmp_path):
    _patch_dir(monkeypatch, tmp_path)
    leases.acquire(100)   # «убитый» TUI: lease остался, процесса нет
    leases.acquire(200)
    alive = lambda pid: pid == 200
    assert leases.live_leases(alive=alive) == [200]
    assert not (tmp_path / "100.lease").exists()       # мёртвый прибран
    assert leases.release(200, alive=alive) is True


def test_release_without_acquire_is_safe(monkeypatch, tmp_path):
    _patch_dir(monkeypatch, tmp_path)
    assert leases.release(999, alive=lambda pid: False) is True


def test_garbage_files_ignored(monkeypatch, tmp_path):
    _patch_dir(monkeypatch, tmp_path)
    (tmp_path / "not-a-pid.lease").write_text("x", encoding="utf-8")
    assert leases.live_leases(alive=lambda pid: False) == []


def _patch_pids(monkeypatch, tmp_path):
    monkeypatch.setattr("voice_runtime.leases.voice_pids_dir", lambda: tmp_path)


def test_no_marker_means_not_ours(monkeypatch, tmp_path):
    """Пометку ставит только тот, кто спавнил. Её отсутствие = «сервис не наш»."""
    _patch_pids(monkeypatch, tmp_path)
    assert leases.is_agent_owned("speaches") is False


def test_mark_and_clear_agent_owned(monkeypatch, tmp_path):
    _patch_pids(monkeypatch, tmp_path)
    leases.mark_agent_owned("speaches")
    assert leases.is_agent_owned("speaches") is True
    leases.clear_agent_owned("speaches")
    assert leases.is_agent_owned("speaches") is False


def test_clear_without_marker_is_safe(monkeypatch, tmp_path):
    _patch_pids(monkeypatch, tmp_path)
    leases.clear_agent_owned("speaches")          # нечего снимать — не падаем
    assert leases.is_agent_owned("speaches") is False


def test_marker_creates_missing_dir(monkeypatch, tmp_path):
    """Первый спавн на чистой машине: каталога pids ещё нет."""
    _patch_pids(monkeypatch, tmp_path / "pids")
    leases.mark_agent_owned("speaches")
    assert leases.is_agent_owned("speaches") is True


def test_marker_is_per_service_name(monkeypatch, tmp_path):
    _patch_pids(monkeypatch, tmp_path)
    leases.mark_agent_owned("speaches")
    assert leases.is_agent_owned("other") is False
