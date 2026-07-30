"""Парсер файла-определения кастомного сабагента.

Файл — YAML-frontmatter + тело-роль. Тело подставляется в `<specific_role>`
общего шаблона промпта, поэтому пустое тело бессмысленно и отвергается.
"""
import pytest

from tools.subagents.file_definitions import (
    SubagentDefinitionError,
    parse_subagent_file,
)

VALID = """---
name: subagent_db_migrator
description: Пишет и проверяет миграции БД.
tools:
  - fs_read_file
  - turn_finish
  - session_read_stored_tool_output
stop_tools:
  - exec_python
---
Ты — инженер по миграциям.

Второй абзац.
"""

MINIMAL = """---
name: subagent_doc_writer
description: Пишет документацию.
---
Ты — технический писатель.
"""


class TestValidFiles:
    def test_full_file(self):
        d = parse_subagent_file("/a/db.md", VALID)
        assert d.name == "subagent_db_migrator"
        assert d.description == "Пишет и проверяет миграции БД."
        assert d.tools == ("fs_read_file", "turn_finish", "session_read_stored_tool_output")
        assert d.stop_tools == ("exec_python",)
        assert d.source_path == "/a/db.md"
        assert d.role.startswith("Ты — инженер по миграциям.")
        assert "Второй абзац." in d.role

    def test_minimal_file(self):
        d = parse_subagent_file("/a/doc.md", MINIMAL)
        assert d.name == "subagent_doc_writer"
        # Ключа нет — «все тулы доступны». Это НЕ то же самое, что пустой список.
        assert d.tools is None
        assert d.stop_tools == ()

    def test_body_is_stripped_but_keeps_inner_layout(self):
        text = MINIMAL.replace("Ты — технический писатель.\n", "\n\nПервая\n\nВторая\n\n")
        d = parse_subagent_file("/a/doc.md", text)
        assert d.role == "Первая\n\nВторая"


class TestName:
    @pytest.mark.parametrize(
        "bad",
        ["worker", "Subagent_worker", "subagent_Worker", "subagent-worker", "subagent_", "subagent_a b"],
    )
    def test_rejects_bad_names(self, bad):
        text = MINIMAL.replace("subagent_doc_writer", bad)
        with pytest.raises(SubagentDefinitionError, match="name"):
            parse_subagent_file("/a/x.md", text)

    def test_accepts_digits_and_underscores(self):
        text = MINIMAL.replace("subagent_doc_writer", "subagent_pg_v2_writer")
        assert parse_subagent_file("/a/x.md", text).name == "subagent_pg_v2_writer"


class TestRequiredFields:
    def test_missing_name(self):
        text = MINIMAL.replace("name: subagent_doc_writer\n", "")
        with pytest.raises(SubagentDefinitionError, match="name"):
            parse_subagent_file("/a/x.md", text)

    def test_missing_description(self):
        text = MINIMAL.replace("description: Пишет документацию.\n", "")
        with pytest.raises(SubagentDefinitionError, match="description"):
            parse_subagent_file("/a/x.md", text)

    def test_blank_description(self):
        text = MINIMAL.replace("description: Пишет документацию.", "description: '   '")
        with pytest.raises(SubagentDefinitionError, match="description"):
            parse_subagent_file("/a/x.md", text)

    def test_empty_body(self):
        text = MINIMAL.replace("Ты — технический писатель.\n", "   \n\n")
        with pytest.raises(SubagentDefinitionError, match="без роли"):
            parse_subagent_file("/a/x.md", text)


class TestFrontmatterShape:
    def test_no_frontmatter_at_all(self):
        with pytest.raises(SubagentDefinitionError, match="frontmatter"):
            parse_subagent_file("/a/x.md", "Просто текст без заголовка.\n")

    def test_unterminated_frontmatter(self):
        with pytest.raises(SubagentDefinitionError, match="frontmatter"):
            parse_subagent_file("/a/x.md", "---\nname: subagent_x\nописание без конца\n")

    def test_broken_yaml(self):
        text = "---\nname: subagent_x\n  description: [непарная\n---\nРоль.\n"
        with pytest.raises(SubagentDefinitionError, match="YAML|frontmatter"):
            parse_subagent_file("/a/x.md", text)

    def test_frontmatter_is_not_a_mapping(self):
        with pytest.raises(SubagentDefinitionError, match="frontmatter"):
            parse_subagent_file("/a/x.md", "---\n- a\n- b\n---\nРоль.\n")

    def test_unknown_key_is_rejected(self):
        # Согласовано с extra="forbid" во всём конфиге: молча проглоченная
        # опечатка `discription:` хуже отказа.
        text = MINIMAL.replace("description:", "discription:")
        with pytest.raises(SubagentDefinitionError, match="discription"):
            parse_subagent_file("/a/x.md", text)

    def test_dropped_qwen_fields_are_rejected_explicitly(self):
        # model/approvalMode из исходного формата не поддерживаются. Молча
        # игнорировать нельзя — оператор будет думать, что они работают.
        for field in ("model: inherit", "approvalMode: yolo"):
            text = MINIMAL.replace("---\nname:", f"---\n{field}\nname:")
            with pytest.raises(SubagentDefinitionError):
                parse_subagent_file("/a/x.md", text)


class TestListNormalization:
    def test_trims_and_drops_empties(self):
        text = MINIMAL.replace("---\nname:", "---\ntools: ['  fs_read_file  ', '', '   ']\nname:")
        assert parse_subagent_file("/a/x.md", text).tools == ("fs_read_file",)

    def test_dedup_keeps_first_order(self):
        text = MINIMAL.replace(
            "---\nname:", "---\ntools: [b_tool, a_tool, b_tool]\nname:"
        )
        assert parse_subagent_file("/a/x.md", text).tools == ("b_tool", "a_tool")

    def test_empty_list_is_valid_and_distinct_from_absent(self):
        text = MINIMAL.replace("---\nname:", "---\ntools: []\nname:")
        d = parse_subagent_file("/a/x.md", text)
        assert d.tools == ()
        assert d.tools is not None

    def test_non_list_value_is_rejected(self):
        text = MINIMAL.replace("---\nname:", "---\ntools: fs_read_file\nname:")
        with pytest.raises(SubagentDefinitionError, match="tools"):
            parse_subagent_file("/a/x.md", text)
