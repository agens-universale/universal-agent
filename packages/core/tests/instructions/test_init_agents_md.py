from gc_core.agent_store import FileAgentStore
from gc_core.instructions.init_agents_md import abuild_init_prompt
from gc_testkit import run_coro
from gc_testkit.memory_agent_store import MemoryAgentStore


def build_init_prompt(*, cwd: str, can_ask: bool) -> str:
    """Шаблон — пакетный, через файловое хранилище по умолчанию."""
    return run_coro(
        abuild_init_prompt(store=FileAgentStore(), cwd=cwd, can_ask=can_ask)
    )


def test_no_file_branch_instructs_create(tmp_path):
    p = build_init_prompt(cwd=str(tmp_path), can_ask=True)
    assert "write_to_file" in p
    assert "create it from scratch" in p
    assert "ask_human" not in p


def test_exists_can_ask_branch_instructs_ask(tmp_path):
    (tmp_path / "AGENTS.md").write_text("old", encoding="utf-8")
    p = build_init_prompt(cwd=str(tmp_path), can_ask=True)
    assert "ask_human" in p
    assert "UPDATE" in p and "RECREATE" in p


def test_exists_headless_branch_updates_no_ask(tmp_path):
    (tmp_path / "AGENTS.md").write_text("old", encoding="utf-8")
    p = build_init_prompt(cwd=str(tmp_path), can_ask=False)
    assert "UPDATE it in place" in p
    assert "Do NOT recreate" in p
    assert "do NOT ask" in p
    assert "ask_human" not in p


def test_prompt_is_universal_and_agent_writes(tmp_path):
    """Промпт — универсальный (code/general-folder + tight rules), агент пишет сам.

    Контент согласованного черновика (init-prompt-universal-draft), но концовка —
    под write_to_file: НЕ старое 'Return the markdown as final answer'."""
    p = build_init_prompt(cwd=str(tmp_path), can_ask=True)
    # universal: каталог может быть не-кодовым
    assert "general working folder" in p
    # tight-writing правила из черновика
    assert "Do not repeat yourself" in p
    assert "run a single" in p  # «how to run a single test»
    # agent-writes контракт (старая концовка убрана)
    assert "write_to_file" in p
    assert (
        "Return the complete AGENTS.md markdown as your final answer" not in p
    )


async def test_template_comes_from_the_agent_store(tmp_path):
    """Шаблон /init — промпт хранилища агента (``init_agents_md``), а не
    файл пакета напрямую."""
    store = MemoryAgentStore(
        prompts={"init_agents_md": "Шаблон {existing_block}"}, fallback=None
    )
    text = await abuild_init_prompt(
        store=store, cwd=str(tmp_path), can_ask=True
    )
    assert text.startswith("Шаблон There is no AGENTS.md yet")


async def test_missing_template_names_the_store_resource(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError, match="init_agents_md"):
        await abuild_init_prompt(
            store=MemoryAgentStore(fallback=None),
            cwd=str(tmp_path),
            can_ask=True,
        )
