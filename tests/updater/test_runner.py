from updater.source import UpdateSource
from updater.runner import build_install_command, install_location_phrase, spawn_detached_update


def test_command_with_ref():
    src = UpdateSource(url="https://x/y", ref="master", commit="c")
    cmd = build_install_command(src, extra_args=["--system-certs"])
    assert "uv" in cmd[0].lower()               # shutil.which может вернуть абсолютный путь
    assert cmd[1:5] == ["tool", "install", "--force", "--reinstall"]
    assert "git+https://x/y@master" in cmd
    assert cmd[-1] == "--system-certs"


def test_command_default_branch_no_ref():
    src = UpdateSource(url="https://x/y", ref=None, commit="c")
    cmd = build_install_command(src, extra_args=[])
    assert "git+https://x/y" in cmd            # без @ref → дефолтная ветка
    assert not any("@" in a for a in cmd)


def test_spawn_detached_injects_command(tmp_path, monkeypatch):
    import updater.runner as runner
    monkeypatch.setattr(runner.tempfile, "tempdir", str(tmp_path))  # не сорить в %TEMP%
    captured = {}
    def _spawn(script_argv):
        captured["argv"] = script_argv
    spawn_detached_update(["uv", "tool", "install", "x"], parent_pid=4321,
                          restart_cmd=["gc-ua"], spawn=_spawn)
    assert captured["argv"]                    # спавн вызван с чем-то исполняемым
    assert "4321" in " ".join(map(str, captured["argv"]))   # pid ожидания вшит


def test_install_location_phrase_platform_dependent(monkeypatch):
    """I4: «в отдельном окне» правда только на Windows (видимый CREATE_NEW_CONSOLE)
    — на POSIX спавн идёт без консоли (start_new_session), окно не появляется."""
    import updater.runner as runner
    monkeypatch.setattr(runner.os, "name", "nt")
    assert install_location_phrase() == "в отдельном окне"
    monkeypatch.setattr(runner.os, "name", "posix")
    assert install_location_phrase() == "в фоне"


def test_posix_script_shell_quotes_args(tmp_path, monkeypatch):
    import updater.runner as runner
    monkeypatch.setattr(runner.os, "name", "posix")
    monkeypatch.setattr(runner.tempfile, "tempdir", str(tmp_path))  # не сорить в %TEMP%
    paths = {}
    def _spawn(argv):
        paths["argv"] = argv
    spawn_detached_update(["uv", "tool", "install", "a b", "x$y"],
                          parent_pid=111, restart_cmd=None, spawn=_spawn)
    script = paths["argv"][-1]                       # sh <path>
    body = open(script, encoding="utf-8").read()
    assert "'a b'" in body                            # пробел → shlex-кавычки
    assert "'x$y'" in body                            # $ заэкранирован (не подстановка)
