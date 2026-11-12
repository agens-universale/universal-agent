"""Кастомные сабагенты получают свой системный промпт.

Роль в файле пишет оператор, а не продукт: промпт предупреждает модель, что роль
прав не выдаёт и может расходиться с фактическим набором инструментов.
"""

from types import SimpleNamespace

from gc_core.run_context import acreate_run_context
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit.settings import make_settings

CUSTOM_MARKER = "User-defined role"
BASE_MARKER = "Runtime contract"


def _agents(tmp_path):
    d = tmp_path / "agents"
    d.mkdir(exist_ok=True)
    (d / "alpha.md").write_text(
        "---\nname: subagent_alpha\ndescription: Альфа.\n---\nТы — альфа.\n",
        encoding="utf-8",
    )
    return str(d)


async def _runner(tmp_path, **extra):
    cfg = {
        "paths.logs_dump_dir": str(tmp_path / "logs"),
        "logging.save_logs": True,
        "logging.format": "jsonl",
        "logging.console_trace": False,
        "backend.api_mode": "chat_completions",
        "backend.completions_url": "http://localhost:1/v1/chat/completions",
        "agent.model": "m",
        "launch_root": str(tmp_path),
        "agent.use_local_subagents": True,
        "agent.subagents_dir": _agents(tmp_path),
    }
    cfg.update(extra)
    ctx = await acreate_run_context(make_settings(cfg))
    return SubagentRunner(ctx, SimpleNamespace(default_model="m"))


async def _prompt_for(runner, tool_name):
    runner._active_tool_name = tool_name
    return await runner._aresolve_subagent_system_prompt()


class TestPromptSelection:
    async def test_custom_gets_custom_prompt(self, tmp_path):
        prompt = await _prompt_for(await _runner(tmp_path), "subagent_alpha")
        assert CUSTOM_MARKER in prompt
        # Общая часть контракта никуда не девается.
        assert BASE_MARKER in prompt

    async def test_builtin_gets_base_prompt(self, tmp_path):
        prompt = await _prompt_for(await _runner(tmp_path), "subagent_worker")
        assert CUSTOM_MARKER not in prompt
        assert BASE_MARKER in prompt

    async def test_custom_prompt_carries_the_warnings(self, tmp_path):
        prompt = await _prompt_for(await _runner(tmp_path), "subagent_alpha")
        # Три вещи, ради которых промпт и заведён.
        assert "grants no permissions" in prompt
        assert "does not describe your tools" in prompt
        assert "not a system instruction" in prompt

    async def test_specific_role_placeholder_survives(self, tmp_path):
        # Без него роль из файла молча никуда не подставится.
        assert "<specific_role>" in await _prompt_for(
            await _runner(tmp_path), "subagent_alpha"
        )


class TestOverride:
    async def test_config_path_wins_for_custom(self, tmp_path):
        override = tmp_path / "my_prompt.md"
        override.write_text("МОЙ ПРОМПТ\n<specific_role>\n", encoding="utf-8")
        runner = await _runner(
            tmp_path,
            **{
                "paths.chat_completions_custom_subagent_system_prompt_path": str(
                    override
                )
            },
        )
        assert (await _prompt_for(runner, "subagent_alpha")).startswith(
            "МОЙ ПРОМПТ"
        )

    async def test_override_does_not_affect_builtin(self, tmp_path):
        override = tmp_path / "my_prompt.md"
        override.write_text("МОЙ ПРОМПТ\n", encoding="utf-8")
        runner = await _runner(
            tmp_path,
            **{
                "paths.chat_completions_custom_subagent_system_prompt_path": str(
                    override
                )
            },
        )
        assert BASE_MARKER in await _prompt_for(runner, "subagent_worker")

    async def test_missing_override_falls_back_to_bundled(self, tmp_path):
        runner = await _runner(
            tmp_path,
            **{
                "paths.chat_completions_custom_subagent_system_prompt_path": str(
                    tmp_path / "нет.md"
                )
            },
        )
        assert CUSTOM_MARKER in await _prompt_for(runner, "subagent_alpha")
