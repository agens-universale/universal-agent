"""Task 8 (план «Профили v2, волна A»): child-конфиг субагента — deep-copy
изменяемых узлов + Router-резолв модели субагента (спека §7 п.4).

См. .superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-8-brief.md.
"""

from gc_core.config import flatten_app_config
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter
from gc_core.states import SharedStateData, State
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit import run_coro as _await
from gc_testkit.helpers.tool_ctx import local_ctx


def _snapshot(raw: dict) -> dict:
    """Реальный снимок config_dict — через настоящий flatten_app_config, не мок."""
    return flatten_app_config(AppConfig.model_validate(raw))


def _make_config(tmp_path, raw: dict) -> dict:
    cfg = _snapshot(raw)
    cfg.update(
        {
            "use_local_subagents": True,
            "backend_api_mode": "chat_completions",
            "subagent_max_steps": 30,
            "enabled_tools": [],
            "skills_data_path": "",
            "mcp_servers": [],
            "system_prompt_id": "test-prompt",
            "save_logs": True,
            "logs_dump_dir": str(tmp_path),
        }
    )
    return cfg


def _parent_ctx(cfg: dict, *, level: str):
    """Родительский ctx с реальным ExecutionWorkspace (нужен `_build_child_ctx`'у
    isinstance-проверкой) + собственным ModelRouter на том же снимке `cfg`."""
    ctx = local_ctx(cfg)
    ctx.run_id = "parent_run"
    ctx.auth_connector = None
    ctx.logger = None
    ctx.logging_path = ""
    ctx.summary_trace_path = ""
    ctx.tools_data = {}
    ctx.tools_spec = []
    ctx.tools_description = []
    ctx.is_tui = False
    ctx.model_router = ModelRouter(cfg, level_provider=lambda: level)
    return ctx


def _make_orchestrator_state() -> State:
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="System",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    return state


class TestChildConfigMutationIsolation:
    """Регресс shallow-copy: `{**self._config}` расшаривал вложенные dict с
    родителем — мутация ребёнка была бы видна родителю (и всем последующим
    ходам/субагентам сессии, читающим тот же `self._config`)."""

    def test_child_header_mutation_does_not_leak_to_parent(self, tmp_path):
        raw = {"models": {"flash": {}}, "slots": {"default": "flash"}}
        cfg = _make_config(tmp_path, raw)
        cfg["chat_completion_headers"] = {
            "Authorization": "Bearer parent-token"
        }
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )
        child_ctx.config_dict["chat_completion_headers"]["Authorization"] = (
            "Bearer child-token"
        )

        assert (
            cfg["chat_completion_headers"]["Authorization"]
            == "Bearer parent-token"
        )

    def test_child_slots_mutation_does_not_leak_to_parent_router(
        self, tmp_path
    ):
        """Ребёнок наследует модель субагента через `unbound_override`, а не
        через мутацию `slots` (спека §5.2 fix) — сборка child ctx не должна
        трогать ни deep-copied `child_config["slots"]`, ни (тем более) реестр
        родителя/его следующие resolve() в этой же сессии."""
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        # `_build_child_ctx` — корутина: без run_coro она не исполнялась
        # вовсе, и утверждения ниже проверяли нетронутый конфиг, а не
        # отсутствие мутации. Прогон об этом честно предупреждал
        # («coroutine ... was never awaited»), но предупреждение не роняет.
        _await(runner._build_child_ctx(tool_name="subagent_planner"))

        assert cfg["slots"]["default"] == "flash"
        assert ctx.model_router.resolve("main").alias == "flash"


class TestChildModelResolve:
    """Ребёнок получает свой Router: `subagent:<name>` родителя становится
    `main` ребёнка; вложенные вызовы без более точной привязки наследуют её же;
    существующие assign-привязки продолжают резолвиться как у родителя."""

    def test_pinned_subagent_main_resolves_to_pin(self, tmp_path):
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_planner")
        )

        resolved = child_ctx.model_router.resolve("main")
        assert resolved.alias == "deepseek-max"

    def test_pinned_subagent_nested_unbound_consumer_inherits_pin(
        self, tmp_path
    ):
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_planner")
        )

        # "summary" has no assign binding — must inherit the subagent's own model,
        # not the active-level model ("flash") an unpinned subagent would get.
        resolved = child_ctx.model_router.resolve("summary")
        assert resolved.alias == "deepseek-max"

    def test_unpinned_subagent_falls_back_to_active_level(self, tmp_path):
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )  # no pin

        resolved = child_ctx.model_router.resolve("main")
        assert resolved.alias == "flash"

    def test_existing_assign_binding_to_alias_still_resolves_in_child(
        self, tmp_path
    ):
        """Более точная привязка (finish_gate -> конкретный алиас) продолжает
        действовать у ребёнка — не перекрывается слот-оверрайдом субагента."""
        raw = {
            "models": {"flash": {}, "deepseek-max": {}, "glm-flash": {}},
            "slots": {"default": "flash"},
            "assign": {
                "subagents": {"subagent_planner": "deepseek-max"},
                "finish_gate": "glm-flash",
            },
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_planner")
        )

        assert (
            child_ctx.model_router.resolve("finish_gate").alias == "glm-flash"
        )
        assert child_ctx.model_router.resolve("main").alias == "deepseek-max"

    def test_existing_assign_binding_to_level_still_resolves_in_child(
        self, tmp_path
    ):
        """Регресс ревью: явная привязка К УРОВНЮ (не к алиасу), совпадающему со
        слотом активного уровня родителя, — «более точная привязка» по спеке
        §7 п.4/§5.2 — не должна съезжать на модель субагента. До фикса (override
        слота активного уровня) `summary` здесь резолвился бы в "deepseek-max"
        вместо реального default-слота "flash"."""
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {
                "subagents": {"subagent_planner": "deepseek-max"},
                "summary": "default",
            },
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_planner")
        )

        assert child_ctx.model_router.resolve("summary").alias == "flash"
        assert child_ctx.model_router.resolve("main").alias == "deepseek-max"

    def test_child_router_level_provider_is_frozen_not_live(self, tmp_path):
        """level_provider ребёнка — константа уровня хода родителя, не живой
        TUI-провайдер: смена /level посреди работы субагента не должна уводить
        модель уже стартовавшего дочернего хода."""
        raw = {
            "models": {"flash": {}, "smart": {}},
            "slots": {"light": "flash", "max": "smart"},
        }
        cfg = _make_config(tmp_path, raw)
        level_box = {"value": "light"}
        ctx = local_ctx(cfg)
        ctx.run_id = "parent_run"
        ctx.auth_connector = None
        ctx.logger = None
        ctx.logging_path = ""
        ctx.summary_trace_path = ""
        ctx.tools_data = {}
        ctx.tools_spec = []
        ctx.tools_description = []
        ctx.is_tui = False
        ctx.model_router = ModelRouter(
            cfg, level_provider=lambda: level_box["value"]
        )
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )
        assert child_ctx.model_router.resolve("main").alias == "flash"

        level_box["value"] = (
            "max"  # живой /level родителя меняется посреди работы
        )
        assert child_ctx.model_router.resolve("main").alias == "flash"

    async def test_child_state_default_model_is_subagent_resolved_model(
        self, tmp_path
    ):
        """state.default_model ребёнка = резолв субагента (begin_turn на старте
        child-хода, см. SubagentRunner._run_internal)."""
        raw = {
            "models": {
                "flash": {},
                "deepseek-max": {"model": "deepseek-v3-max"},
            },
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        orch = _make_orchestrator_state()
        runner = SubagentRunner(ctx, orch, cfg)
        captured = {}

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured["default_model"] = child_state.default_model
            return "ok"

        await runner.arun(
            "subagent_planner",
            {"task": "plan it", "reason": "r"},
            fake_run_agent,
        )

        assert captured["default_model"] == "deepseek-v3-max"

    def test_force_legacy_propagates_to_child_router(self, tmp_path):
        """Task 10 (открытый пункт Task 8, task-8-report.md concern 3): родительский
        `force_legacy` (CLI `--model` поверх непустого реестра) обязан действовать и
        в дочернем Router субагента — форс-легаси не должен обходиться собственной
        assign-привязкой ребёнка (`finish_gate` здесь указывает на реальный алиас,
        но при force_legacy обязан резолвиться в форс-модель, как у родителя)."""
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {
                "subagents": {"subagent_planner": "deepseek-max"},
                "finish_gate": "deepseek-max",
            },
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        ctx.model_router = ModelRouter(
            cfg, level_provider=lambda: "default", force_legacy=True
        )
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_planner")
        )

        main_resolved = child_ctx.model_router.resolve("main")
        assert main_resolved.alias is None
        assert main_resolved.source == "легаси-форс --model"

        finish_gate_resolved = child_ctx.model_router.resolve("finish_gate")
        assert finish_gate_resolved.alias is None
        assert finish_gate_resolved.source == "легаси-форс --model"

    def test_no_parent_router_yields_no_child_router(self, tmp_path):
        """Легаси/тестовые двойники ctx без model_router: ребёнок тоже без Router,
        но ничего не падает (turn_boundary/воронки устойчивы к model_router=None)."""
        raw = {"models": {"flash": {}}, "slots": {"default": "flash"}}
        cfg = _make_config(tmp_path, raw)
        ctx = local_ctx(cfg)
        ctx.run_id = "parent_run"
        ctx.auth_connector = None
        ctx.logger = None
        ctx.logging_path = ""
        ctx.summary_trace_path = ""
        ctx.tools_data = {}
        ctx.tools_spec = []
        ctx.tools_description = []
        ctx.is_tui = False
        # ctx.model_router намеренно не выставлен.
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )

        assert child_ctx.model_router is None
