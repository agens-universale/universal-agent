"""Реестр как функция от конфига: встроенные + кастомные из папки."""

import logging

from gc_core.tools.subagents.registry import (
    SUBAGENT_CLASSES,
    builtin_subagent_names,
    get_subagent_class,
    get_subagents_tools_data,
    is_subagent_tool,
    resolve_subagent_definitions,
)
from gc_testkit.settings import fake_ctx, make_settings

BUILTIN_COUNT = len(SUBAGENT_CLASSES)


def _write(tmp_path, filename, name, extra=""):
    (tmp_path / filename).write_text(
        f"---\nname: {name}\ndescription: Описание {name}.\n{extra}---\nРоль.\n",
        encoding="utf-8",
    )


class TestAbsence:
    def test_no_config_means_builtins_only(self):
        # Так реестр зовут тесты и любой код, которому кастомные не нужны.
        data = get_subagents_tools_data()
        assert len(data) == BUILTIN_COUNT
        assert set(data) == builtin_subagent_names()

    def test_config_without_dir_means_builtins_only(self):
        assert len(get_subagents_tools_data(make_settings())) == BUILTIN_COUNT

    def test_builtins_still_resolve_to_classes(self):
        assert get_subagent_class("subagent_worker") is SUBAGENT_CLASSES[0]
        assert get_subagent_class("unknown_tool") is None
        assert is_subagent_tool("fs_read_file") is False


class TestWithCustom:
    def test_custom_appears_next_to_builtins(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha")
        cfg = make_settings({"subagents_dir": str(tmp_path)})
        data = get_subagents_tools_data(cfg)
        assert len(data) == BUILTIN_COUNT + 1
        assert (
            data["subagent_alpha"]["description"] == "Описание subagent_alpha."
        )
        assert data["subagent_alpha"]["spec"]["name"] == "subagent_alpha"

    def test_custom_is_recognized_and_resolvable(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha")
        cfg = make_settings({"subagents_dir": str(tmp_path)})
        assert is_subagent_tool("subagent_alpha", cfg) is True
        factory = get_subagent_class("subagent_alpha", cfg)
        assert factory is not None
        assert factory.definition.name == "subagent_alpha"

    def test_custom_invisible_without_config(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha")
        assert is_subagent_tool("subagent_alpha") is False

    def test_definitions_are_memoized_on_run_context(self, tmp_path):
        # Реестр дёргается на каждый вызов тула; читать папку каждый раз нельзя.
        _write(tmp_path, "a.md", "subagent_alpha")
        cfg = {"subagents_dir": str(tmp_path)}
        ctx = fake_ctx(cfg)
        first = resolve_subagent_definitions(ctx)
        assert ctx.subagent_definitions is first
        _write(tmp_path, "b.md", "subagent_beta")
        assert resolve_subagent_definitions(ctx) is first
        # Свежий контекст видит обоих — кэш привязан к рану, а не к процессу.
        assert len(resolve_subagent_definitions(fake_ctx(cfg))) == 2

    def test_seeded_definitions_skip_discovery(self, tmp_path):
        # Durable-исполнитель кладёт готовые определения в ctx — диск не читается.
        _write(tmp_path, "a.md", "subagent_alpha")
        ctx = fake_ctx({"subagents_dir": str(tmp_path)})
        ctx.subagent_definitions = {}
        assert resolve_subagent_definitions(ctx) == {}
        assert is_subagent_tool("subagent_alpha", ctx) is False


class TestRequiredToolsAlwaysGranted:
    def test_allowlist_without_required_loads_fine(self, tmp_path):
        # Раньше это была ошибка старта; теперь ядро доливается само.
        _write(
            tmp_path, "a.md", "subagent_alpha", extra="tools: [fs_read_file]\n"
        )
        data = get_subagents_tools_data(
            make_settings({"subagents_dir": str(tmp_path)})
        )
        assert "subagent_alpha" in data

    def test_stop_tools_ban_warns_and_still_loads(self, tmp_path, caplog):
        _write(
            tmp_path,
            "a.md",
            "subagent_alpha",
            extra="stop_tools: [turn_finish]\n",
        )
        with caplog.at_level(logging.WARNING):
            defs = resolve_subagent_definitions(
                make_settings({"subagents_dir": str(tmp_path)})
            )
        assert "subagent_alpha" in defs
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "subagent_required_tools_kept" in m and "turn_finish" in m
            for m in msgs
        )
