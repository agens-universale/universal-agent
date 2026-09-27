"""Старт процесса: конфиг, harbor, тишина в stdout."""

from gc_acp.main import backend_rejection, main


def test_backend_rejection_names_harbor_and_accepts_local():
    assert backend_rejection({"os_backend": "local"}) is None
    assert backend_rejection({}) is None
    text = backend_rejection({"os_backend": "harbor"})
    assert text is not None
    assert "harbor" in text


def test_harbor_exits_before_protocol(monkeypatch, capsys):
    seen = {}

    def load(path, profile_override=None):
        seen["path"] = path
        seen["profile"] = profile_override
        return {"os_backend": "harbor"}, None

    def run(coro):
        coro.close()
        raise AssertionError("protocol started")

    monkeypatch.setattr("gc_acp.main.load_config_ex", load)
    monkeypatch.setattr("gc_acp.main.asyncio.run", run)
    assert main(["--config", "c.yml", "--profile", "work"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "harbor" in captured.err
    assert seen == {"path": "c.yml", "profile": "work"}


def test_unreadable_config_exits_before_protocol(monkeypatch, capsys):
    def load(path, profile_override=None):
        raise ValueError("сломан")

    def run(coro):
        coro.close()
        raise AssertionError("protocol started")

    monkeypatch.setattr("gc_acp.main.load_config_ex", load)
    monkeypatch.setattr(
        "gc_acp.main.explain_broken_config", lambda *args, **kwargs: "сломан"
    )
    monkeypatch.setattr("gc_acp.main.asyncio.run", run)
    assert main(["--config", "missing.yml"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "сломан" in captured.err


async def _no_lines():
    if False:
        yield ""


def test_local_config_is_silent_on_stdout(monkeypatch, capsys, tmp_path):
    def load(path, profile_override=None):
        return {
            "os_backend": "local",
            "threads_data_path": str(tmp_path),
        }, None

    monkeypatch.setattr("gc_acp.main.load_config_ex", load)
    monkeypatch.setattr("gc_acp.main.stdin_lines", _no_lines)
    assert main(["--config", "c.yml"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_unsupported_backend_is_rejected():
    text = backend_rejection({"os_backend": "docker"})
    assert text is not None
    assert "docker" in text
