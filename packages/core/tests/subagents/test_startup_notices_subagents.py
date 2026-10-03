"""Проблемы кастомных сабагентов доезжают до /notices в TUI.

Молча пропущенный файл — та же беда, что `stop_tools` с опечаткой: оператор
что-то написал, оно не сработало, и в логе он это не увидит.
"""

from gc_core.startup_notices import collect_startup_notices
from gc_testkit.settings import make_settings


def _write(d, filename, body):
    (d / filename).write_text(body, encoding="utf-8")


def _valid(name):
    return f"---\nname: subagent_{name}\ndescription: Описание {name}.\n---\nРоль.\n"


def _notices(cfg):
    return collect_startup_notices(
        make_settings(cfg), set(), None, is_tui=True
    )


def _codes(cfg):
    return [n.code for n in _notices(cfg)]


def _msg(cfg, code):
    return next(n.message for n in _notices(cfg) if n.code == code)


class TestAbsence:
    def test_no_dir_no_notices(self):
        assert _notices({}) == []

    def test_blank_dir_no_notices(self):
        assert _notices({"subagents_dir": "   "}) == []

    def test_clean_folder_is_silent(self, tmp_path):
        _write(tmp_path, "a.md", _valid("alpha"))
        assert _codes({"subagents_dir": str(tmp_path)}) == []


class TestEachProblemSurfaces:
    def test_broken_file(self, tmp_path):
        _write(
            tmp_path,
            "broken.md",
            "---\nname: subagent_x\ndiscription: опечатка\n---\nРоль.\n",
        )
        cfg = {"subagents_dir": str(tmp_path)}
        assert "subagent_definition_invalid" in _codes(cfg)
        msg = _msg(cfg, "subagent_definition_invalid")
        # Имя файла и причина — без них уведомление бесполезно.
        assert "broken.md" in msg
        assert "discription" in msg

    def test_collision_with_builtin(self, tmp_path):
        _write(tmp_path, "collide.md", _valid("worker"))
        cfg = {"subagents_dir": str(tmp_path)}
        msg = _msg(cfg, "subagent_name_collision")
        assert "subagent_worker" in msg
        assert "collide.md" in msg
        assert "встроенным" in msg

    def test_collision_between_custom_names_the_winner(self, tmp_path):
        _write(tmp_path, "a_first.md", _valid("dup"))
        _write(tmp_path, "z_second.md", _valid("dup"))
        msg = _msg({"subagents_dir": str(tmp_path)}, "subagent_name_collision")
        assert "z_second.md" in msg
        assert "a_first.md" in msg

    def test_unknown_name_in_list(self, tmp_path):
        _write(tmp_path, "a.md", _valid("alpha"))
        cfg = {
            "subagents_dir": str(tmp_path),
            "use_local_subagents": ["typo_name"],
        }
        msg = _msg(cfg, "use_local_subagents_unknown")
        assert "subagent_typo_name" in msg

    def test_limit_exceeded_lists_dropped(self, tmp_path):
        for n in ("alpha", "beta", "gamma"):
            _write(tmp_path, f"{n}.md", _valid(n))
        cfg = {"subagents_dir": str(tmp_path), "subagents_max_count": 1}
        msg = _msg(cfg, "subagents_max_count_exceeded")
        assert "subagent_beta" in msg and "subagent_gamma" in msg

    def test_missing_dir(self, tmp_path):
        cfg = {"subagents_dir": str(tmp_path / "нет")}
        assert "subagents_dir_missing" in _codes(cfg)

    def test_required_tools_ban_ignored(self, tmp_path):
        _write(
            tmp_path,
            "a.md",
            "---\nname: subagent_alpha\ndescription: Альфа.\nstop_tools: [turn_finish]\n---\nРоль.\n",
        )
        msg = _msg(
            {"subagents_dir": str(tmp_path)}, "subagent_required_tools_kept"
        )
        assert "turn_finish" in msg


class TestAllAtOnce:
    def test_four_problems_give_four_notices(self, tmp_path):
        _write(
            tmp_path,
            "broken.md",
            "---\nname: subagent_x\ndiscription: опечатка\n---\nРоль.\n",
        )
        _write(tmp_path, "collide.md", _valid("worker"))
        for n in ("alpha", "beta", "gamma"):
            _write(tmp_path, f"{n}.md", _valid(n))
        cfg = {
            "subagents_dir": str(tmp_path),
            "subagents_max_count": 2,
            "use_local_subagents": ["gamma", "alpha", "beta", "typo"],
        }
        codes = _codes(cfg)
        assert set(codes) == {
            "subagent_definition_invalid",
            "subagent_name_collision",
            "use_local_subagents_unknown",
            "subagents_max_count_exceeded",
        }
        assert all(n.level == "warning" for n in _notices(cfg))


class TestResilience:
    def test_notices_do_not_crash_startup(self, tmp_path, monkeypatch):
        """Сбой дискавери не должен ронять старт — уведомления вторичны."""
        from gc_core import startup_notices as mod

        def _boom(*_a, **_kw):
            raise RuntimeError("дискавери сломался")

        # Уведомления собирает дискавери реестра (``subagent_diagnostics`` →
        # ``_discover``) — роняем именно его.
        monkeypatch.setattr(
            "gc_core.tools.subagents.registry._discover",
            _boom,
        )
        _write(tmp_path, "bad.md", "без фронтматтера")
        assert (
            mod.collect_startup_notices(
                make_settings({"subagents_dir": str(tmp_path)}),
                set(),
                None,
                is_tui=True,
            )
            == []
        )

    def test_run_context_keeps_discovery_diagnostics(self, tmp_path):
        # Дискавери на контексте рана запоминается вместе с диагностикой:
        # повтор не читает диск снова.
        from gc_core.tools.subagents.registry import (
            resolve_subagent_definitions,
            subagent_diagnostics,
        )
        from gc_testkit.settings import fake_ctx

        _write(tmp_path, "a.md", _valid("alpha"))
        _write(tmp_path, "bad.md", "без фронтматтера")
        ctx = fake_ctx({"subagents_dir": str(tmp_path)})
        resolve_subagent_definitions(ctx)
        (tmp_path / "b.md").write_text(_valid("beta"), encoding="utf-8")

        assert list(resolve_subagent_definitions(ctx)) == ["subagent_alpha"]
        assert subagent_diagnostics(ctx) == list(ctx.subagent_diagnostics)
        assert subagent_diagnostics(ctx)
