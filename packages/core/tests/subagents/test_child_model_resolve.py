"""Task 8 (план «Профили v2, волна A»): child-конфиг субагента — deep-copy
изменяемых узлов + Router-резолв модели субагента (спека §7 п.4).

См. .superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-8-brief.md.
"""

from gc_core.model_router import ModelRouter
from copy import deepcopy

from gc_core.execution.state import SharedStateData
from gc_core.states import State
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit import run_coro as _await
from gc_testkit.helpers.tool_ctx import local_ctx
from gc_testkit.settings import make_settings


def _make_config(tmp_path, raw: dict, **extra):
    """Настоящие настройки (строгая валидация, как из файла) из ``raw``."""
    return make_settings(
        {
            **raw,
            "agent.use_local_subagents": True,
            "backend.api_mode": "chat_completions",
            "agent.subagent_max_steps": 30,
            "paths.skills_data_path": "",
            "agent.system_prompt_id": "test-prompt",
            "logging.save_logs": True,
            "paths.logs_dump_dir": str(tmp_path),
            **extra,
        }
    )


def _parent_ctx(cfg, *, level: str):
    """Родительский ctx с реальным ExecutionWorkspace (нужен `_build_child_ctx`'у
    isinstance-проверкой) + собственным ModelRouter на тех же настройках `cfg`."""
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
    ctx.model_router = ModelRouter(cfg, choice_provider=lambda: level)
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
    ходам/субагентам сессии, читающим те же настройки)."""

    def test_child_header_mutation_does_not_leak_to_parent(self, tmp_path):
        raw = {"models": {"flash": {}}, "slots": {"default": "flash"}}
        cfg = _make_config(
            tmp_path,
            raw,
            **{
                "backend.chat_completion_headers": {
                    "Authorization": "Bearer parent-token"
                }
            },
        )
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state())

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )
        child_headers = child_ctx.settings.backend.chat_completion_headers
        assert child_headers["Authorization"] == "Bearer parent-token"
        # Модель неизменяема, но словарь внутри — нет: ребёнок обязан получить
        # свою копию, а не ссылку на словарь родителя.
        child_headers["Authorization"] = "Bearer child-token"

        assert (
            cfg.settings.backend.chat_completion_headers["Authorization"]
            == "Bearer parent-token"
        )

    def test_child_slots_mutation_does_not_leak_to_parent_router(
        self, tmp_path
    ):
        """Ребёнок наследует модель субагента через `unbound_override`, а не
        через мутацию `slots` (спека §5.2 fix) — сборка child ctx не должна
        трогать ни слоты настроек, ни (тем более) реестр родителя/его
        следующие resolve() в этой же сессии."""
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state())

        # `_build_child_ctx` — корутина: без run_coro она не исполнялась
        # вовсе, и утверждения ниже проверяли нетронутый конфиг, а не
        # отсутствие мутации. Прогон об этом честно предупреждал
        # («coroutine ... was never awaited»), но предупреждение не роняет.
        _await(runner._build_child_ctx(tool_name="subagent_planner"))

        assert cfg.settings.slots.default == "flash"
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
        runner = SubagentRunner(ctx, _make_orchestrator_state())

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_planner")
        )

        resolved = child_ctx.model_router.resolve("main")
        assert resolved.alias == "deepseek-max"

    def test_pinned_subagent_judges_and_summary_go_default_others_inherit_pin(
        self, tmp_path
    ):
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state())

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_planner")
        )

        # Таблица `unbound_level`: суммаризация привязанного ребёнка — уровнем
        # default (решение PR140 Р1 1б), прочие непривязанные — пином ребёнка.
        assert child_ctx.model_router.resolve("summary").alias == "flash"
        assert (
            child_ctx.model_router.resolve("intent_scope").alias
            == "deepseek-max"
        )

    def test_child_breaker_error_suggests_model_command(self, tmp_path):
        """Пин и активная модель в предохранителе — честная ошибка с
        подсказкой /model, не /level."""
        import pytest

        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        ctx.model_router.breaker.trip("flash", "5xx")
        ctx.model_router.breaker.trip("deepseek-max", "5xx")
        runner = SubagentRunner(ctx, _make_orchestrator_state())

        with pytest.raises(RuntimeError) as excinfo:
            _await(runner._build_child_ctx(tool_name="subagent_planner"))

        message = str(excinfo.value)
        assert "Выберите другую модель /model или профиль /profile." in (
            message
        )
        assert "/level" not in message

    def test_child_freezes_parent_applied_choice_not_pending(self, tmp_path):
        """Ребёнок берёт применённый выбор родителя: ожидающий выбор (тихая
        отметка) ещё не действует ни у родителя, ни у ребёнка."""
        raw = {
            "models": {"flash": {}, "smart": {}},
            "slots": {"light": "flash", "max": "smart"},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="max")
        parent_state = _make_orchestrator_state()
        parent_state.applied_choice = "light"
        ctx.active_state = parent_state
        runner = SubagentRunner(ctx, parent_state)

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )

        assert child_ctx.model_router.live_choice() == "light"
        assert child_ctx.model_router.resolve("main").alias == "flash"

    def test_unpinned_subagent_falls_back_to_active_level(self, tmp_path):
        raw = {
            "models": {"flash": {}, "deepseek-max": {}},
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state())

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
        runner = SubagentRunner(ctx, _make_orchestrator_state())

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
        runner = SubagentRunner(ctx, _make_orchestrator_state())

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_planner")
        )

        assert child_ctx.model_router.resolve("summary").alias == "flash"
        assert child_ctx.model_router.resolve("main").alias == "deepseek-max"

    def test_child_router_choice_provider_is_frozen_not_live(self, tmp_path):
        """choice_provider ребёнка — константа уровня хода родителя, не живой
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
            cfg, choice_provider=lambda: level_box["value"]
        )
        runner = SubagentRunner(ctx, _make_orchestrator_state())

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
        runner = SubagentRunner(ctx, orch)
        captured = {}

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured["default_model"] = child_state.default_model
            return "ok"

        await runner.arun(
            "subagent_planner",
            {"task": "plan it", "reason": "r"},
            fake_run_agent,
        )

        assert captured["default_model"] == "deepseek-v3-max"

    def test_child_router_is_marked_as_subagent_child(self, tmp_path):
        """Дочерний Router субагента явно помечен флагом is_subagent_child=True
        (спека автороутинга §5.1): потребители фоллбэков (processing_responses)
        читают этот флаг для пропуска class-2-обработки."""
        raw = {"models": {"flash": {}}, "slots": {"default": "flash"}}
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="default")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)
        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )
        assert child_ctx.model_router.is_subagent_child is True

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
            cfg, choice_provider=lambda: "default", force_legacy=True
        )
        runner = SubagentRunner(ctx, _make_orchestrator_state())

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
        runner = SubagentRunner(ctx, _make_orchestrator_state())

        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )

        assert child_ctx.model_router is None

    def test_unbound_child_routes_itself_from_default_in_auto(self, tmp_path):
        raw = {
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "backend": {"api_mode": "chat_completions"},
            "model": "auto",
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="auto")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)
        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )
        assert (
            child_ctx.model_router.is_subagent_child
            and child_ctx.model_router.auto_allowed
        )
        assert child_ctx.model_router.live_choice() == "auto"
        child_state = _await(runner._build_child_state("worker"))
        child_ctx.attach_state(child_state)
        assert (
            child_ctx.model_router.resolve("main").alias == "mid"
        )  # старт с default
        child_state.current_level = "light"
        assert (
            child_ctx.model_router.resolve("main").alias == "fast"
        )  # решения ребёнка меняют модель

    def test_unbound_child_in_auto_starts_when_the_start_level_is_dead(
        self, tmp_path
    ):
        """Алиас стартового уровня в предохранителе: старт ребёнка в auto не
        отказывает — уровень выберет его точка решения, ближайшим живым."""
        from gc_core import processing_responses as pr

        raw = {
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "backend": {"api_mode": "chat_completions"},
            "model": "auto",
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="auto")
        ctx.model_router.breaker.trip("mid", "5xx после ретраев")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)
        routed = []

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            routed.append(
                (await pr.aroute_before_call(child_state, child_ctx))[2]
            )
            return "ok"

        _await(
            runner.arun(
                "subagent_planner",
                {"task": "plan it", "reason": "r"},
                fake_run_agent,
            )
        )

        assert routed == ["max"]

    def test_unbound_child_on_a_one_model_config_has_no_auto(self, tmp_path):
        """Непривязанный ребёнок наследует сырой режим родителя, а доступность
        считает по своему конфигу той же функцией ядра: одна модель — работает
        default."""
        raw = {
            "models": {"only": {}},
            "slots": {"default": "only"},
            "backend": {"api_mode": "chat_completions"},
            "model": "auto",
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="auto")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)
        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )
        assert child_ctx.model_router.live_choice() == "auto"
        assert child_ctx.auto_available is False
        child_state = _await(runner._build_child_state("worker"))
        child_ctx.attach_state(child_state)
        child_state.current_level = "light"  # даже при чужом решении
        assert child_ctx.model_router.active_target() == "default"

    def test_bound_child_has_no_auto(self, tmp_path):
        raw = {
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "assign": {"subagents": {"subagent_worker": "big"}},
            "backend": {"api_mode": "chat_completions"},
            "model": "auto",
        }
        cfg = _make_config(tmp_path, raw)
        ctx = _parent_ctx(cfg, level="auto")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)
        child_ctx = _await(
            runner._build_child_ctx(tool_name="subagent_worker")
        )
        assert child_ctx.model_router.auto_allowed is False
        assert child_ctx.model_router.resolve("main").alias == "big"


class TestChildCtxToolsSurviveModelSelection:
    """Реальные тулы ребёнка доходят до отправки при смене модели; строгая
    проекция зависит от выбранной модели и не меняет канонический реестр."""

    async def test_child_tools_reach_transport_with_per_model_strict_flags(
        self, tmp_path, monkeypatch
    ):
        from gc_core import processing_responses as pr

        raw = {
            "models": {
                "loose": {
                    "model": "loose-model",
                    "url": "http://loose/v1/chat/completions",
                    "token": "",
                    "backend": {
                        "openai_strict_tools_schema": False,
                        "send_tool_strict": False,
                    },
                },
                "strict": {
                    "model": "strict-model",
                    "url": "http://strict/v1/chat/completions",
                    "token": "",
                    "backend": {
                        "openai_strict_tools_schema": True,
                        "send_tool_strict": True,
                    },
                },
            },
            "slots": {"light": "loose", "default": "strict", "max": "strict"},
            "model": "auto",
            "backend": {
                "openai_strict_tools_schema": False,
                "send_tool_strict": True,
            },
        }
        cfg = _make_config(tmp_path, raw)
        parent = _parent_ctx(cfg, level="auto")
        runner = SubagentRunner(parent, _make_orchestrator_state())
        child_ctx = await runner._build_child_ctx("subagent_worker")
        child_state = await runner._build_child_state("subagent_worker")
        child_ctx.attach_state(child_state)
        canonical_state = deepcopy(child_state.tools)
        canonical_context = deepcopy(child_ctx.tools_spec)
        sent = []

        async def fake_network(*, payload_factory, **kwargs):
            payload = payload_factory(100)
            sent.append((deepcopy(payload), kwargs["url"]))
            return {
                "id": "child-response",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }, payload

        monkeypatch.setattr(
            pr, "arequest_chat_completions_with_policy", fake_network
        )
        assert child_ctx.tools_data
        assert canonical_state == canonical_context
        for level in ("light", "max"):
            child_state.current_level = level
            (
                messages,
                response_id,
                _headers,
            ) = await pr._arequest_backend_canonical_once(
                [{"role": "user", "content": "read the project"}],
                child_state,
                child_ctx,
            )
            assert response_id == "child-response"
            assert messages
            assert child_state.tools == canonical_state
            assert child_ctx.tools_spec == canonical_context

        loose, loose_url = sent[0]
        strict, strict_url = sent[1]
        assert loose["model"] == "loose-model"
        assert strict["model"] == "strict-model"
        assert loose_url == "http://loose/v1/chat/completions"
        assert strict_url == "http://strict/v1/chat/completions"
        expected_names = {tool["name"] for tool in canonical_state}
        assert expected_names
        assert {
            tool["function"]["name"] for tool in loose["tools"]
        } == expected_names
        assert {
            tool["function"]["name"] for tool in strict["tools"]
        } == expected_names
        loose_read = next(
            tool["function"]
            for tool in loose["tools"]
            if tool["function"]["name"] == "fs_read_file"
        )
        strict_read = next(
            tool["function"]
            for tool in strict["tools"]
            if tool["function"]["name"] == "fs_read_file"
        )
        assert "strict" not in loose_read
        assert strict_read["strict"] is True
        loose_params = loose_read["parameters"]
        strict_params = strict_read["parameters"]
        assert set(loose_params["required"]) < set(loose_params["properties"])
        assert set(strict_params["required"]) == set(
            strict_params["properties"]
        )
        assert strict_params["additionalProperties"] is False
        assert "x-openai-strict" not in loose_params
        assert (
            "x-openai-strict"
            not in child_ctx.tools_data["fs_read_file"]["spec"]["parameters"]
        )


class TestChildLeavesItsLevelAfterRun:
    """Уровень решения auto ребёнка снимается по концу его прогона
    (`state_ops.leave` во внутреннем `finally` `SubagentRunner._arun_internal`),
    симметрично `AgentExecutor.arun`
    (`test_active_state_cleared_after_agent_executor_run_completes`): иначе
    `child_ctx.model_router.active_target()` после возврата показывал бы уровень
    последнего решения ребёнка вместо нейтрального default."""

    RAW = {
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
        "backend": {"api_mode": "chat_completions"},
        "model": "auto",
    }

    def _run(self, tmp_path, fake_run_agent):
        cfg = _make_config(tmp_path, self.RAW)
        ctx = _parent_ctx(cfg, level="auto")
        runner = SubagentRunner(ctx, _make_orchestrator_state(), cfg)
        return _await(
            runner.arun(
                "subagent_planner",
                {"task": "plan it", "reason": "r"},
                fake_run_agent,
            )
        )

    def test_level_is_left_after_the_child_run_completes(self, tmp_path):
        seen = {}

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            child_state.current_level = (
                "light"  # имитация решения к концу прогона
            )
            seen.update(state=child_state, ctx=child_ctx)
            return "ok"

        self._run(tmp_path, fake_run_agent)

        assert seen["state"].current_level is None
        assert seen["ctx"].model_router.active_target() == "default"

    def test_level_is_left_when_the_child_run_fails(self, tmp_path):
        seen = {}

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            child_state.current_level = "light"
            seen.update(state=child_state, ctx=child_ctx)
            raise RuntimeError("прогон ребёнка упал")

        out = self._run(tmp_path, fake_run_agent)

        assert "subagent_run_failed" in out
        assert seen["state"].current_level is None
        assert seen["ctx"].model_router.active_target() == "default"
