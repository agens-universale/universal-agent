"""Относительный путь из ``--paths.*`` и ``agent_overrides`` — от cwd.

До типизированных настроек ``--paths.agent_md_path MY.md`` находил файл в
текущем каталоге; после — путь считался от каталога конфига, и агент молча
шёл без инструкций."""

import os

from gc_core.config import apply_cli_overrides
from gc_testkit.settings import make_settings


def test_relative_cli_paths_resolve_from_cwd(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "MY.md").write_text("инструкции", encoding="utf-8")
    monkeypatch.chdir(work)
    loaded = apply_cli_overrides(
        make_settings(),
        extra_overrides={
            "paths.agent_md_path": "MY.md",
            "summary": {"chat_completions_summary_prompt_path": "p/s.md"},
            "security.write_guard.extra_paths": ["data", "/abs"],
            "agent.max_steps": 7,
        },
    )
    settings = loaded.settings
    assert settings.paths.agent_md_path == str(work / "MY.md")
    assert os.path.isfile(settings.paths.agent_md_path)
    assert settings.summary.chat_completions_summary_prompt_path == str(
        work / "p" / "s.md"
    )
    assert settings.security.write_guard.extra_paths == [
        str(work / "data"),
        "/abs",
    ]
    assert settings.agent.max_steps == 7
