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


def test_start_key_is_taken_from_a_real_process():
    """Отпечаток снимается с ЖИВОГО процесса и повторяется между вызовами.

    Проверяем на настоящем pid, а не на подмене: механика платформенная (ctypes на
    Windows, /proc на Linux), и молчаливый None превратил бы защиту в фикцию —
    lease писался бы без отпечатка, а `_lease_is_ours` верил бы каждому тёзке."""
    import os

    key = leases.process_start_key(os.getpid())
    assert key and key == leases.process_start_key(os.getpid())
    assert leases.process_start_key(0) is None      # заведомо не наш процесс


def test_reused_pid_does_not_keep_the_light_on(monkeypatch, tmp_path):
    """Номер PID достался ПОСТОРОННЕМУ процессу — lease всё равно мёртвый.

    Находка ревью коллеги: живость клиента определялась по имени файла плюс
    `is_pid_alive`, а содержимое lease не читалось вовсе. TUI, убитый жёстко (kill -9,
    падение, ребут), оставлял файл на диске; система со временем выдавала тот же номер
    чужому процессу — и с этого момента `release` никогда не возвращал True: speaches
    не гас даже при всех закрытых окнах агента, держа в памяти whisper-модель."""
    _patch_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(leases, "process_start_key", lambda pid: "start-A")
    leases.acquire(100)                                  # lease нашего TUI
    monkeypatch.setattr(leases, "process_start_key", lambda pid: "start-B")   # чужой процесс

    assert leases.live_leases(alive=lambda pid: True) == []
    assert not (tmp_path / "100.lease").exists()          # прибран как мёртвый
    assert leases.release(999, alive=lambda pid: True) is True   # свет можно гасить


def test_same_process_keeps_its_lease(monkeypatch, tmp_path):
    """Тот же процесс — тот же отпечаток: lease живой, гасить нельзя."""
    _patch_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(leases, "process_start_key", lambda pid: "start-A")
    leases.acquire(100)
    assert leases.live_leases(alive=lambda pid: True) == [100]
    assert leases.release(999, alive=lambda pid: True) is False


def test_lease_without_a_start_key_is_trusted(monkeypatch, tmp_path):
    """Старый формат (или система, где время старта не узнать) — верим как раньше.

    Иначе обновление агента погасило бы сервис под живыми окнами: их lease написан
    прежней версией и отпечатка не содержит."""
    _patch_dir(monkeypatch, tmp_path)
    (tmp_path / "100.lease").write_text("100", encoding="utf-8")
    monkeypatch.setattr(leases, "process_start_key", lambda pid: "start-A")
    assert leases.live_leases(alive=lambda pid: True) == [100]


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
