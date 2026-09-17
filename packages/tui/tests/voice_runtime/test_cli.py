"""gc-voice CLI: маршрутизация подкоманд и сценарии status/stop/remove на моках."""
import io
import os
import sys
from types import SimpleNamespace

from gc_tui.voice_runtime import cli as cli
from gc_tui.voice_runtime.state import VoiceState

ST = VoiceState(version="v0.8.3", root="/data/voice/speaches",
                port=8735, model="Systran/faster-whisper-small")


def test_status_not_installed(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_state", lambda: None)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "не установлен" in out and "gc-voice install" in out


def test_status_installed_running(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_state", lambda: ST)
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: 4242)
    monkeypatch.setattr(cli, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "port_alive", lambda st: True)
    monkeypatch.setattr(cli, "live_leases", lambda **kw: [111])
    monkeypatch.setattr(cli, "managed_voice",
                        lambda: {"url": "http://127.0.0.1:8735/v1/audio/transcriptions"})
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "v0.8.3" in out and "4242" in out and "8735" in out and "111" in out


def test_status_names_an_outdated_engine(monkeypatch, capsys):
    """`gc-voice status` печатал версию, ни с чем её не сравнивая: узнать, что движок
    отстал, было неоткуда, кроме экрана после апгрейда агента."""
    from gc_tui.voice_runtime.state import SPEACHES_PIN
    old = VoiceState(version="v0.0.1-old", root="/data/voice/speaches",
                     port=8735, model="m")
    monkeypatch.setattr(cli, "load_state", lambda: old)
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(cli, "port_alive", lambda st: False)
    monkeypatch.setattr(cli, "live_leases", lambda **kw: [])
    monkeypatch.setattr(cli, "managed_voice", lambda: None)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"доступно {SPEACHES_PIN}" in out
    assert "Ctrl+U" in out and "gc-voice install" in out     # куда идти обновляться


def test_status_survives_cp1251_console(monkeypatch):
    """Русская Windows-консоль (cp1251): '→' вне кодировки не роняет CLI."""
    monkeypatch.setattr(cli, "load_state", lambda: ST)
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(cli, "port_alive", lambda st: False)
    monkeypatch.setattr(cli, "live_leases", lambda **kw: [])
    monkeypatch.setattr(cli, "managed_voice", lambda: None)
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1251")
    monkeypatch.setattr(sys, "stdout", stream)
    assert cli.main(["status"]) == 0
    stream.flush()
    assert b"speaches" in stream.buffer.getvalue()


def test_stop_kills_service(monkeypatch, capsys):
    killed = []
    cleared = []
    monkeypatch.setattr(cli, "load_state", lambda: ST)
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: 4242)
    monkeypatch.setattr(cli, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "terminate_pid", lambda pid, **kw: killed.append(pid))
    monkeypatch.setattr(cli, "remove_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(cli, "clear_agent_owned", lambda name: cleared.append(name))
    assert cli.main(["stop"]) == 0
    assert killed == [4242]
    assert cleared == ["speaches"]                 # погашен — пометка владельца снята


def test_serve_claim_clears_stale_owner_marker(monkeypatch):
    """Человек занимает pid-файл под форграундный serve. Пометка «подняли агенты» от
    прошлой, уже мёртвой жизни сервиса обязана уйти: иначе следующий выходящий TUI
    прочитает её и погасит этот форграунд — законно с точки зрения выключателя."""
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: 111)
    monkeypatch.setattr(cli, "is_pid_alive", lambda pid: False)     # старый pid мёртв
    monkeypatch.setattr(cli, "write_pid", lambda name, pid, pids_dir=None: None)
    cleared = []
    monkeypatch.setattr(cli, "clear_agent_owned", lambda name: cleared.append(name))
    assert cli._claim_pid_file() is True
    assert cleared == ["speaches"]


def test_serve_leaves_live_foreign_pid_alone(monkeypatch):
    """Чужой ЖИВОЙ pid-файл serve не трогает — и пометку владельца тоже не трогает:
    сервис работает, и он не наш."""
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: 111)
    monkeypatch.setattr(cli, "is_pid_alive", lambda pid: True)
    cleared = []
    monkeypatch.setattr(cli, "clear_agent_owned", lambda name: cleared.append(name))
    assert cli._claim_pid_file() is False
    assert cleared == []


def test_remove_unwires_and_clears(monkeypatch, tmp_path, capsys):
    root = tmp_path / "speaches"
    root.mkdir()
    st = VoiceState(version="v0.8.3", root=str(root), port=8735, model="m")
    events = []
    monkeypatch.setattr(cli, "load_state", lambda: st)
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(cli, "unwire_voice", lambda: events.append("unwire") or True)
    monkeypatch.setattr(cli, "clear_state", lambda: events.append("clear"))
    assert cli.main(["remove"]) == 0
    assert events == ["unwire", "clear"]
    assert not root.exists()                       # каталог снесён
    assert "HF-кэш" in capsys.readouterr().out     # модели не трогаем — сказано


def test_unknown_command_is_error():
    assert cli.main(["bogus"]) == 2


def test_remove_reports_partial_delete(monkeypatch, tmp_path, capsys):
    """Залоченный .venv (Windows): remove честно сообщает о неполном удалении."""
    root = tmp_path / "speaches"
    root.mkdir()
    st = VoiceState(version="v0.8.3", root=str(root), port=8735, model="m")
    monkeypatch.setattr(cli, "load_state", lambda: st)
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(cli, "unwire_voice", lambda: True)
    monkeypatch.setattr(cli, "clear_state", lambda: None)
    monkeypatch.setattr(cli, "force_rmtree", lambda p: False)   # «файлы заняты»
    # Код возврата — не косметика: по нему судят и скрипты, и /settings (там от него
    # зависит, скажут человеку «удалено» или «файлы заняты»).
    assert cli.main(["remove"]) == 1
    out = capsys.readouterr().out
    assert "НЕ полностью" in out and "повторите" in out


# --- сервис, поднятый снаружи (gc-voice serve): его видно и о нём говорят -----


def _serve_env(monkeypatch, tmp_path, run):
    """Обвязка forground-serve: состояние, команда запуска и подменённый subprocess."""
    monkeypatch.setattr(cli, "load_state", lambda: ST)
    monkeypatch.setattr(cli, "build_run_spec", lambda st: SimpleNamespace(env={}))
    monkeypatch.setattr(cli, "build_command_argv", lambda spec: ["uvicorn"])
    monkeypatch.setattr(cli, "voice_pids_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.subprocess, "run", run)


def test_serve_writes_its_pid_while_running(monkeypatch, tmp_path, capsys):
    """Без pid-файла stop/status/remove слепы к этому процессу: погасить его командой
    нельзя, а на Windows он держит gc-voice.exe и ломает обновление агента."""
    seen = {}

    def _run(argv, **kw):
        seen["pid_while_running"] = cli.read_pid(cli.SERVICE_NAME, tmp_path)
        class R:
            returncode = 0
        return R()

    _serve_env(monkeypatch, tmp_path, _run)
    assert cli.main(["serve"]) == 0
    assert seen["pid_while_running"] == os.getpid()
    assert cli.read_pid(cli.SERVICE_NAME, tmp_path) is None      # на выходе — прибрали за собой


def test_serve_keeps_a_foreign_pid_file(monkeypatch, tmp_path):
    """Сервис уже поднят кем-то другим: чужой pid-файл не перезаписываем и не сносим —
    иначе рабочий сервис станет невидим для stop."""
    cli.write_pid(cli.SERVICE_NAME, 4242, tmp_path)
    monkeypatch.setattr(cli, "is_pid_alive", lambda pid: pid == 4242)

    class R:
        returncode = 1

    _serve_env(monkeypatch, tmp_path, lambda argv, **kw: R())
    assert cli.main(["serve"]) == 1
    assert cli.read_pid(cli.SERVICE_NAME, tmp_path) == 4242


def test_status_tells_about_a_service_started_outside(monkeypatch, capsys):
    """Порт отвечает, pid-файла нет — говорить «не запущен» значит врать в глаза."""
    monkeypatch.setattr(cli, "load_state", lambda: ST)
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(cli, "port_alive", lambda st: True)
    monkeypatch.setattr(cli, "live_leases", lambda **kw: [])
    monkeypatch.setattr(cli, "managed_voice", lambda: None)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "8735" in out and "снаружи" in out
    assert "не запущен" not in out


def test_stop_tells_about_a_service_started_outside(monkeypatch, capsys):
    """То же для stop: «not running» при живом порте — ложь, и человек уходит ни с чем."""
    monkeypatch.setattr(cli, "load_state", lambda: ST)
    monkeypatch.setattr(cli, "read_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(cli, "port_alive", lambda st: True)
    assert cli.main(["stop"]) == 1
    out = capsys.readouterr().out
    assert "снаружи" in out and "not running" not in out
