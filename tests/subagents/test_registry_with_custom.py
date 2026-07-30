"""Реестр как функция от конфига: встроенные + кастомные из папки."""
import pytest

from tools.subagents.custom.file_definitions import SubagentDefinitionError
from tools.subagents.registry import (
    SUBAGENT_CLASSES,
    builtin_subagent_names,
    get_subagent_class,
    get_subagents_tools_data,
    is_subagent_tool,
    resolve_subagent_definitions,
)

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
        assert len(get_subagents_tools_data({})) == BUILTIN_COUNT

    def test_builtins_still_resolve_to_classes(self):
        assert get_subagent_class("subagent_worker") is SUBAGENT_CLASSES[0]
        assert get_subagent_class("unknown_tool") is None
        assert is_subagent_tool("fs_read_file") is False


class TestWithCustom:
    def test_custom_appears_next_to_builtins(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha")
        cfg = {"subagents_dir": str(tmp_path)}
        data = get_subagents_tools_data(cfg)
        assert len(data) == BUILTIN_COUNT + 1
        assert data["subagent_alpha"]["description"] == "Описание subagent_alpha."
        assert data["subagent_alpha"]["spec"]["name"] == "subagent_alpha"

    def test_custom_is_recognized_and_resolvable(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha")
        cfg = {"subagents_dir": str(tmp_path)}
        assert is_subagent_tool("subagent_alpha", cfg) is True
        factory = get_subagent_class("subagent_alpha", cfg)
        assert factory is not None
        assert factory.definition.name == "subagent_alpha"

    def test_custom_invisible_without_config(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha")
        assert is_subagent_tool("subagent_alpha") is False

    def test_definitions_are_memoized_in_config(self, tmp_path):
        # Реестр дёргается на каждый вызов тула; читать папку каждый раз нельзя.
        _write(tmp_path, "a.md", "subagent_alpha")
        cfg = {"subagents_dir": str(tmp_path)}
        first = resolve_subagent_definitions(cfg)
        _write(tmp_path, "b.md", "subagent_beta")
        assert resolve_subagent_definitions(cfg) is first
        # Свежий конфиг видит обоих — кэш привязан к словарю, а не к процессу.
        assert len(resolve_subagent_definitions({"subagents_dir": str(tmp_path)})) == 2


class TestRequiredToolsFailFast:
    def test_missing_required_tool_raises(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha", extra="tools: [fs_read_file]\n")
        with pytest.raises(SubagentDefinitionError, match="turn_finish"):
            get_subagents_tools_data({"subagents_dir": str(tmp_path)})

    def test_error_is_not_swallowed_as_invalid_file(self, tmp_path):
        # Битый файл деградирует до варнинга, а вынутый обязательный тул — нет:
        # он даёт агента, который молча не работает.
        _write(tmp_path, "a.md", "subagent_alpha", extra="stop_tools: [turn_finish]\n")
        with pytest.raises(SubagentDefinitionError):
            resolve_subagent_definitions({"subagents_dir": str(tmp_path)})
