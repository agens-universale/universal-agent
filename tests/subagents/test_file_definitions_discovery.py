"""Дискавери папки с кастомными сабагентами: порядок, коллизии, лимит, битые файлы.

Единственный отказ, роняющий старт, — обязательные тулы (проверяется в
test_file_subagent). Здесь всё деградирует до варнинга: один битый файл не должен
лишать оператора остальных.
"""
import logging

import pytest

from tools.subagents.file_definitions import discover_subagent_definitions

BUILTIN = {"subagent_worker", "subagent_coder"}


def _write(tmp_path, filename, name, body="Роль."):
    path = tmp_path / filename
    path.write_text(
        f"---\nname: {name}\ndescription: Описание {name}.\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def _discover(cfg, caplog=None):
    logger = logging.getLogger("test_discovery")
    return discover_subagent_definitions(cfg, builtin_names=BUILTIN, logger=logger)


class TestAbsence:
    def test_no_key_reads_nothing_and_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            assert _discover({}) == []
        assert caplog.records == []

    def test_blank_path_is_absence(self, caplog):
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            assert _discover({"subagents_dir": "   "}) == []
        assert caplog.records == []

    def test_missing_dir_warns_once(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            assert _discover({"subagents_dir": str(tmp_path / "нет")}) == []
        assert len(caplog.records) == 1
        assert "subagents_dir_missing" in caplog.records[0].getMessage()

    def test_empty_dir(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            assert _discover({"subagents_dir": str(tmp_path)}) == []
        assert caplog.records == []


class TestReading:
    def test_reads_md_files(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha")
        _write(tmp_path, "b.md", "subagent_beta")
        names = [d.name for d in _discover({"subagents_dir": str(tmp_path)})]
        assert sorted(names) == ["subagent_alpha", "subagent_beta"]

    def test_ignores_non_md_and_subdirs(self, tmp_path):
        _write(tmp_path, "a.md", "subagent_alpha")
        (tmp_path / "readme.txt").write_text("не определение", encoding="utf-8")
        nested = tmp_path / "nested"
        nested.mkdir()
        _write(nested, "deep.md", "subagent_deep")
        names = [d.name for d in _discover({"subagents_dir": str(tmp_path)})]
        assert names == ["subagent_alpha"]

    def test_broken_file_does_not_block_others(self, tmp_path, caplog):
        (tmp_path / "broken.md").write_text("нет frontmatter", encoding="utf-8")
        _write(tmp_path, "good.md", "subagent_good")
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            names = [d.name for d in _discover({"subagents_dir": str(tmp_path)})]
        assert names == ["subagent_good"]
        assert any("subagent_definition_invalid" in r.getMessage() for r in caplog.records)


class TestCollisions:
    def test_builtin_wins(self, tmp_path, caplog):
        _write(tmp_path, "mine.md", "subagent_worker")
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            assert _discover({"subagents_dir": str(tmp_path)}) == []
        assert any("subagent_name_collision" in r.getMessage() for r in caplog.records)

    def test_first_by_filename_wins(self, tmp_path, caplog):
        _write(tmp_path, "z_second.md", "subagent_dup", body="Проиграл.")
        _write(tmp_path, "a_first.md", "subagent_dup", body="Победил.")
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            found = _discover({"subagents_dir": str(tmp_path)})
        assert len(found) == 1
        # Сортировка по имени файла — единственное, что делает «первого» определённым.
        assert found[0].role == "Победил."
        assert found[0].source_path.endswith("a_first.md")
        assert any("subagent_name_collision" in r.getMessage() for r in caplog.records)


class TestMaxCount:
    def test_default_limit_is_20(self, tmp_path, caplog):
        for i in range(25):
            _write(tmp_path, f"a{i:02d}.md", f"subagent_n{i:02d}")
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            found = _discover({"subagents_dir": str(tmp_path)})
        assert len(found) == 20

    def test_overflow_names_are_listed_not_silent(self, tmp_path, caplog):
        for i in range(3):
            _write(tmp_path, f"a{i}.md", f"subagent_n{i}")
        with caplog.at_level(logging.WARNING, logger="test_discovery"):
            found = _discover({"subagents_dir": str(tmp_path), "subagents_max_count": 2})
        assert [d.name for d in found] == ["subagent_n0", "subagent_n1"]
        msgs = [r.getMessage() for r in caplog.records]
        assert any("subagents_max_count_exceeded" in m and "subagent_n2" in m for m in msgs)

    def test_config_value_overrides_default(self, tmp_path):
        for i in range(5):
            _write(tmp_path, f"a{i}.md", f"subagent_n{i}")
        found = _discover({"subagents_dir": str(tmp_path), "subagents_max_count": 1})
        assert len(found) == 1
