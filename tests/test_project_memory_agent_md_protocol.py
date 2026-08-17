"""Guard current concise protocol in demo_cli_agent.md."""
from pathlib import Path
from tests.paths import ASSETS


_PROMPT_PATH = (
    ASSETS / "prompts" / "demo_cli_agent.md"
)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_demo_cli_agent_md_has_persistent_memory_section():
    text = _read_prompt()
    assert "Отвечай на русском" in text


def test_protocol_is_deployment_agnostic():
    section = _read_prompt()
    flat = _flat(section)
    assert "запиши его в файл" in flat
    assert "директории проекта" in flat
    assert "краткое резюме и путь к файлу" in flat


def test_no_conditional_prompt_tags():
    text = _read_prompt()
    assert "<memory_files_prompt>" not in text
    assert "<long_memory_prompt>" not in text
