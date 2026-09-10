"""use_local_subagents: bool или список имён (use_server_subagents не трогаем).

Правило: список выбирает КАСТОМНЫХ сабагентов. Встроенные семь доступны всегда,
пока значение не false, — поэтому пустой список означает «только встроенные», а
имя встроенного в списке безвредно.
"""
import logging

import pytest

from gc_core.tools.subagents.selection import subagents_allowlist, subagents_allowlist_ordered, subagents_enabled

BUILTIN = {
    "subagent_worker", "subagent_code_generator", "subagent_code_planner",
    "subagent_planner", "subagent_critic", "subagent_code_reviewer",
    "subagent_special_agent",
}


class TestEnabled:
    @pytest.mark.parametrize("value,expected", [
        (True, True),
        (False, False),
        (None, False),
        (["subagent_worker"], True),
        (["worker"], True),
        ([], True),          # пустой список = только встроенные, а не «никого»
        (["", "  "], True),  # мусорный список тоже включает встроенных
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

    def test_empty_list_is_empty_set_not_none(self):
        assert subagents_allowlist([]) == set()

    def test_junk_entries_dropped(self):
        assert subagents_allowlist(["", "  ", "coder"]) == {"subagent_coder"}


class TestOrderedAllowlist:
    """Порядок нужен для урезания по subagents_max_count: оператор перечислил
    кастомных в порядке приоритета, и отбрасывать надо хвост этого порядка."""

    def test_bool_gives_none(self):
        assert subagents_allowlist_ordered(True) is None
        assert subagents_allowlist_ordered(False) is None

    def test_keeps_config_order(self):
        assert subagents_allowlist_ordered(["gamma", "alpha", "beta"]) == [
            "subagent_gamma", "subagent_alpha", "subagent_beta",
        ]

    def test_dedup_keeps_first_position(self):
        assert subagents_allowlist_ordered(["a", "b", "subagent_a"]) == [
            "subagent_a", "subagent_b",
        ]

    def test_empty_list_is_empty_list(self):
        assert subagents_allowlist_ordered([]) == []


class TestIntegration:
    def _cfg(self, tmp_path, agents_dir, value, **extra):
        cfg = {
            "launch_root": str(tmp_path),
            "backend_api_mode": "chat_completions",
            "use_local_subagents": value,
            "subagents_dir": agents_dir,
        }
        cfg.update(extra)
        return cfg

    def _agents(self, tmp_path, names=("alpha",)):
        d = tmp_path / "agents"
        d.mkdir(exist_ok=True)
        for n in names:
            (d / f"{n}.md").write_text(
                f"---\nname: subagent_{n}\ndescription: {n}.\n---\nРоль.\n",
                encoding="utf-8",
            )
        return str(d)

    async def _split(self, cfg):
        from gc_core.tools import abuild_tools_data

        data, _spec, _descr, _mcp = await abuild_tools_data(cfg)
        names = {n for n in data if n.startswith("subagent_")}
        return names & BUILTIN, sorted(names - BUILTIN)

    async def test_true_gives_all(self, tmp_path):
        builtin, custom = await self._split(self._cfg(tmp_path, self._agents(tmp_path), True))
        assert builtin == BUILTIN
        assert custom == ["subagent_alpha"]

    async def test_false_gives_none(self, tmp_path):
        builtin, custom = await self._split(self._cfg(tmp_path, self._agents(tmp_path), False))
        assert builtin == set()
        assert custom == []

    async def test_empty_list_keeps_builtins_only(self, tmp_path):
        # Ключевое отличие от false: встроенные остаются.
        builtin, custom = await self._split(self._cfg(tmp_path, self._agents(tmp_path), []))
        assert builtin == BUILTIN
        assert custom == []

    async def test_list_narrows_only_custom(self, tmp_path):
        agents = self._agents(tmp_path, ("alpha", "beta"))
        builtin, custom = await self._split(self._cfg(tmp_path, agents, ["alpha"]))
        assert builtin == BUILTIN
        assert custom == ["subagent_alpha"]

    async def test_builtin_name_in_list_is_harmless(self, tmp_path):
        agents = self._agents(tmp_path, ("alpha", "beta"))
        builtin, custom = await self._split(self._cfg(tmp_path, agents, ["alpha", "worker"]))
        assert builtin == BUILTIN
        assert custom == ["subagent_alpha"]

    async def test_unknown_name_warns(self, tmp_path, caplog):
        agents = self._agents(tmp_path)
        with caplog.at_level(logging.WARNING):
            await self._split(self._cfg(tmp_path, agents, ["typo_name"]))
        msgs = [r.getMessage() for r in caplog.records]
        assert any("use_local_subagents_unknown" in m and "subagent_typo_name" in m for m in msgs)

    async def test_builtin_name_in_list_is_not_warned(self, tmp_path, caplog):
        agents = self._agents(tmp_path)
        with caplog.at_level(logging.WARNING):
            await self._split(self._cfg(tmp_path, agents, ["worker"]))
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("use_local_subagents_unknown" in m for m in msgs)


class TestLimitFollowsListOrder:
    """subagents_max_count относится только к кастомным и урезает хвост списка."""

    def _agents(self, tmp_path, names):
        d = tmp_path / "agents"
        d.mkdir(exist_ok=True)
        for n in names:
            (d / f"{n}.md").write_text(
                f"---\nname: subagent_{n}\ndescription: {n}.\n---\nРоль.\n",
                encoding="utf-8",
            )
        return str(d)

    async def _custom(self, tmp_path, value, limit):
        from gc_core.tools import abuild_tools_data

        agents = self._agents(tmp_path, ("alpha", "beta", "gamma"))
        data, _s, _d, _m = await abuild_tools_data({
            "launch_root": str(tmp_path),
            "backend_api_mode": "chat_completions",
            "use_local_subagents": value,
            "subagents_dir": agents,
            "subagents_max_count": limit,
        })
        names = {n for n in data if n.startswith("subagent_")}
        return sorted(names - BUILTIN)

    async def test_list_order_wins_over_filename_order(self, tmp_path):
        # По имени файла первыми были бы alpha и beta; список говорит иначе.
        assert await self._custom(tmp_path, ["gamma", "alpha", "beta"], 2) == [
            "subagent_alpha", "subagent_gamma",
        ]

    async def test_without_list_falls_back_to_filename_order(self, tmp_path):
        assert await self._custom(tmp_path, True, 2) == ["subagent_alpha", "subagent_beta"]

    async def test_builtins_are_not_counted_against_limit(self, tmp_path):
        from gc_core.tools import abuild_tools_data

        agents = self._agents(tmp_path, ("alpha",))
        data, _s, _d, _m = await abuild_tools_data({
            "launch_root": str(tmp_path),
            "backend_api_mode": "chat_completions",
            "use_local_subagents": True,
            "subagents_dir": agents,
            "subagents_max_count": 1,
        })
        names = {n for n in data if n.startswith("subagent_")}
        assert names & BUILTIN == BUILTIN
        assert sorted(names - BUILTIN) == ["subagent_alpha"]

    async def test_dropped_names_are_logged(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            await self._custom(tmp_path, ["gamma", "alpha", "beta"], 2)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("subagents_max_count_exceeded" in m and "subagent_beta" in m for m in msgs)
