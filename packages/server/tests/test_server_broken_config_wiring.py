"""Сломанный конфиг — понятный отказ gc-serve с кодом 1, а не трейсбек."""

from gc_server import cli


def _server_config(tmp_path, text="{}\n"):
    path = tmp_path / "config_server.yml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_broken_config_server_yaml_exits_with_its_path(tmp_path, capsys):
    path = _server_config(tmp_path, "server: [unclosed\n")
    assert cli.main(["--server-config", path]) == 1
    err = capsys.readouterr().err
    assert "config_server.yml" in err
    assert "Traceback" not in err


def test_broken_agent_config_exits_with_explanation(tmp_path, capsys):
    bad = tmp_path / "config.yml"
    bad.write_text("agent: [unclosed\n", encoding="utf-8")
    code = cli.main(
        ["--config", str(bad), "--server-config", _server_config(tmp_path)]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert str(bad) in err
    assert "Traceback" not in err


def test_moved_key_exits_with_the_new_place(tmp_path, capsys):
    path = _server_config(tmp_path, "defaults:\n  system: x\n")
    assert cli.main(["--server-config", path]) == 1
    assert "webhook.default_system" in capsys.readouterr().err
