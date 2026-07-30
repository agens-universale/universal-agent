"""use_local_subagents: bool или список имён (use_server_subagents не трогаем)."""
import logging

import pytest

from tools.subagents.selection import (
    filter_subagents_by_allowlist,
    subagents_allowlist,
    subagents_enabled,
)


class TestEnabled:
    @pytest.mark.parametrize("value,expected", [
        (True, True),
        (False, False),
        (None, False),
        (["subagent_worker"], True),
        (["worker"], True),
        ([], False),          # пустой список = ни одного, не «все»
        (["", "  "], False),  # список из мусора тоже пуст
    ])
    def test_truthiness(self, value, expected):
        assert subagents_enabled(value) is expected


class TestAllowlist:
    def test_bool_means_no_narrowing(self):
        assert subagents_allowlist(True) is None
        assert subagents_allowlist(False) is None
        assert subagents_allowlist(None) is None

    def test_prefix_is_optional(self):
        # Все имена сабагентов начинаются с subagent_ по построению, поэтому
        # 'worker' и 'subagent_worker' — заведомо одно и то же.
        assert subagents_allowlist(["worker"]) == {"subagent_worker"}
        assert subagents_allowlist(["subagent_worker"]) == {"subagent_worker"}

    def test_mixed_forms_collapse(self):
        assert subagents_allowlist(["worker", "subagent_worker"]) == {"subagent_worker"}

    def test_empty_list_is_empty_set_not_none(self):
        # Отличие от None принципиально: None = «все», set() = «ни одного».
        assert subagents_allowlist([]) == set()

    def test_junk_entries_dropped(self):
        assert subagents_allowlist(["", "  ", "coder"]) == {"subagent_coder"}


class TestFilter:
    AVAILABLE = ("subagent_worker", "subagent_coder", "subagent_db_migrator")

    def test_bool_keeps_everything(self):
        assert filter_subagents_by_allowlist(self.AVAILABLE, True) == set(self.AVAILABLE)

    def test_list_narrows(self):
        got = filter_subagents_by_allowlist(self.AVAILABLE, ["worker", "db_migrator"])
        assert got == {"subagent_worker", "subagent_db_migrator"}

    def test_custom_and_builtin_are_equal_citizens(self):
        # Кастомного можно оставить, встроенных выкинуть — и наоборот.
        assert filter_subagents_by_allowlist(self.AVAILABLE, ["db_migrator"]) == {
            "subagent_db_migrator"
        }

    def test_empty_list_yields_nothing(self):
        assert filter_subagents_by_allowlist(self.AVAILABLE, []) == set()

    def test_unknown_name_warns_and_is_skipped(self, caplog):
        with caplog.at_level(logging.WARNING):
            got = filter_subagents_by_allowlist(self.AVAILABLE, ["worker", "typo_name"])
        assert got == {"subagent_worker"}
        msgs = [r.getMessage() for r in caplog.records]
        assert any("use_local_subagents_unknown" in m and "subagent_typo_name" in m for m in msgs)


class TestIntegration:
    def _cfg(self, tmp_path, agents_dir, value):
        return {
            "launch_root": str(tmp_path),
            "backend_api_mode": "chat_completions",
            "use_local_subagents": value,
            "subagents_dir": agents_dir,
        }

    def _agents(self, tmp_path):
        d = tmp_path / "agents"
        d.mkdir()
        (d / "alpha.md").write_text(
            "---\nname: subagent_alpha\ndescription: Альфа.\n---\nРоль.\n", encoding="utf-8"
        )
        return str(d)

    def _subagent_names(self, cfg):
        from tools import build_tools_data

        data, _spec, _descr, _mcp = build_tools_data(cfg)
        return {n for n in data if n.startswith("subagent_")}

    def test_true_gives_all(self, tmp_path):
        names = self._subagent_names(self._cfg(tmp_path, self._agents(tmp_path), True))
        assert "subagent_alpha" in names
        assert "subagent_worker" in names

    def test_list_narrows_across_both_sources(self, tmp_path):
        cfg = self._cfg(tmp_path, self._agents(tmp_path), ["alpha", "worker"])
        assert self._subagent_names(cfg) == {"subagent_alpha", "subagent_worker"}

    def test_list_can_pick_only_custom(self, tmp_path):
        cfg = self._cfg(tmp_path, self._agents(tmp_path), ["alpha"])
        assert self._subagent_names(cfg) == {"subagent_alpha"}

    def test_false_and_empty_list_give_none(self, tmp_path):
        agents = self._agents(tmp_path)
        assert self._subagent_names(self._cfg(tmp_path, agents, False)) == set()
        assert self._subagent_names(self._cfg(tmp_path, agents, [])) == set()

    def test_filtered_out_subagent_cannot_be_dispatched(self, tmp_path):
        """Отфильтрованного нет в tools_spec, но реестр о нём знает.

        Без проверки в диспетчере прямой вызов исполнился бы в обход конфига.
        """
        from tools import apply_tool

        cfg = self._cfg(tmp_path, self._agents(tmp_path), ["alpha"])
        out = apply_tool(
            tool_name="subagent_worker",
            arguments="{}",
            config_dict=cfg,
            tools_data_={},
        )
        assert "subagent_worker" in out
        # Не запустился: вернулся отказ «нет такого тула», а не результат сабагента.
        from tools import is_unknown_tool_output

        assert is_unknown_tool_output(out)
