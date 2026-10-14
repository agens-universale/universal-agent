"""Root preparation keeps routing facts across the private-config boundary."""

import hashlib
import json
import logging
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter

from gc_core.auto_routing.policy import RoutingParams
from gc_core.auto_routing.request_equivalence import MainRequestRef, PreparedRequestEquivalence
from gc_core.execution.contracts import ExecutionFailure
from gc_core.model_router import ModelRouter
from gc_core.processing_responses import _floor_target
from gc_core.run_preparation import PreparedPrompts
from gc_core_temporalio.activities import AgentActivities
from gc_core_temporalio.activity_config import DEFAULT_ACTIVITY_SETTINGS
from gc_core_temporalio.contracts import ActivityCall, PreparedContext, RunRequest
from gc_core_temporalio.public_config import public_config_snapshot
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import fake_ctx, make_settings


@pytest.fixture
def preparation(monkeypatch, tmp_path):
    import gc_core.run_preparation as prompts
    import gc_core.tools.subagents.registry as registry

    monkeypatch.setattr(prompts, 'load_run_prompts', lambda *a, **k: PreparedPrompts())
    monkeypatch.setattr(registry, 'resolve_subagent_definitions', lambda *a, **k: {})

    def build(difference='url', shape='private_only', mode='auto'):
        models = {}
        for alias in ('fast', 'mid', 'big'):
            entry = {'model': 'shared-model', 'url': 'https://shared.invalid/v1',
                     'token': 'fake-common-token'}
            if difference == 'url':
                entry['url'] = f'https://{alias}.invalid/v1'
            elif difference == 'token':
                entry['token'] = f'fake-{alias}-token'
            elif difference == 'headers':
                entry['backend'] = {'chat_completion_headers': {'X-Private': f'fake-{alias}-header'}}
            if shape == 'lower_collapsed' and alias == 'big':
                entry['model'] = 'visible-big-model'
            if shape == 'upper_collapsed' and alias == 'fast':
                entry['model'] = 'visible-fast-model'
            models[alias] = entry
        loaded = make_settings({'models': models, 'level': mode,
            'slots': {'light': 'fast', 'default': 'mid', 'max': 'big'},
            'backend.api_mode': 'chat_completions', 'backend.vision': False,
            'agent.auto_routing': True, 'agent.auto_routing_safe_level': 'auto',
            'logging.save_logs': False})

        async def skills():
            return []

        ctx = fake_ctx(loaded=loaded, run_id='worker-root-scope', logging_path='',
            summary_trace_path='', trace_id='fake-trace', project_gc_dir=str(tmp_path),
            workspace=SimpleNamespace(root=str(tmp_path), shell='sh', os_name='posix',
                root_writable=True, backend=SimpleNamespace(is_sandbox=False)),
            tools_spec=[], tools_description=[], mcp_status={}, registered_tool_names=set(),
            mcp_session_owner='fake-owner', tools_data={}, logger=logging.getLogger(__name__),
            skills_registry=SimpleNamespace(list_skills=skills), agent_store=MemoryAgentStore(fallback=None))
        ctx.model_router = ModelRouter(ctx, level_provider=lambda: 'light')
        native = SimpleNamespace(context_provider=None, stores={'files': SimpleNamespace()},
            public_config=public_config_snapshot, activity_settings=DEFAULT_ACTIVITY_SETTINGS)
        return ctx, native

    return build


async def prepare(native, ctx, data):
    result = await AgentActivities._prepare(native, ActivityCall('test', 'activity-owner', 'prepare', data), ctx)
    payloads = await DataConverter.default.encode([result])
    [decoded] = await DataConverter.default.decode(payloads, [PreparedContext])
    return decoded


async def root_turn(monkeypatch, ctx, native, *, root_patch=True, contract_v2=True, change=None):
    import gc_core_temporalio.workflow as root_module

    observed = {'events': []}

    def patched(name):
        observed['events'].append(name)
        return root_patch if name == 'root-routing-request-equivalence-v1' else contract_v2

    class Operations(SimpleNamespace):
        def __init__(self, *_):
            hooks = ('prompt_loader', 'state_preparer', 'guard_probe', 'tool_effect',
                     'skills_loader', 'mcp_cleanup', 'prompt_resource', 'summary_trace_sink', 'project_memory_io')
            super().__init__(**dict.fromkeys(hooks), activity_settings=DEFAULT_ACTIVITY_SETTINGS)

        async def call(self, name, data, result_type):
            assert name == 'universal_agent.context.prepare'
            assert result_type is PreparedContext
            observed['events'].append('prepare')
            observed['data'] = data
            result = await prepare(native, ctx, data)
            observed['prepared'] = result
            return change(result) if change else result

    monkeypatch.setattr(root_module.workflow, 'info', lambda: SimpleNamespace(
        workflow_id='root-workflow', first_execution_run_id='chain'))
    monkeypatch.setattr(root_module.workflow, 'patched', patched)
    monkeypatch.setattr(root_module, 'WorkflowOperations', Operations)
    monkeypatch.setattr(root_module, 'install_subagents', lambda *a, **k: SimpleNamespace())
    root = UniversalAgentWorkflow()
    await root._prepare_turn(RunRequest('test', 'files', 'test', 'thread', ['task']), 'owner', SimpleNamespace())
    return root.ctx, observed


@pytest.mark.parametrize('difference', ['url', 'token', 'headers'])
@pytest.mark.parametrize('shape', ['private_only', 'lower_collapsed', 'upper_collapsed'])
async def test_actual_root_preparation_preserves_private_request_pairs(monkeypatch, preparation, difference, shape):
    worker, native = preparation(difference, shape)
    root, observed = await root_turn(monkeypatch, worker, native)
    assert root.auto_available is True
    assert root.routing_params.safe_level == 'auto'
    assert root.model_router.live_level() == 'auto'
    assert root.prepared_request_equivalence.groups == (0, 1, 2)
    assert root.prepared_request_equivalence.context_id == root.run_id == worker.run_id
    assert observed['events'].index('root-routing-request-equivalence-v1') < observed['events'].index('prepare')
    assert observed['data']['require_request_equivalence'] is True
    for weak, strong in (('light', 'default'), ('default', 'max')):
        resolved = root.model_router.resolve('main', level_override=weak)
        assert _floor_target(root, weak, resolved, RoutingParams(safe_level=strong)) == strong
    recorded = json.dumps(asdict(observed['prepared']))
    for entry in worker.settings.models.values():
        values = [str(entry.url), str(entry.token)]
        if entry.backend:
            values.extend((entry.backend.chat_completion_headers or {}).values())
        for value in values:
            assert value not in recorded
            assert hashlib.sha256(value.encode()).hexdigest() not in recorded


async def test_equivalent_root_aliases_do_not_enable_auto(monkeypatch, preparation):
    worker, native = preparation('same')
    root, _ = await root_turn(monkeypatch, worker, native)
    assert root.auto_available is False
    assert root.routing_params.safe_level == 'default'
    assert root.prepared_request_equivalence.groups == (0, 0, 0)
    assert _floor_target(root, 'light', root.model_router.resolve('main', level_override='light'),
                         RoutingParams(safe_level='default')) is None


@pytest.mark.parametrize('contract_v2,available', [(True, False), (False, True)])
async def test_absent_root_marker_keeps_old_input_and_comparison(monkeypatch, preparation, contract_v2, available):
    worker, native = preparation()
    root, observed = await root_turn(monkeypatch, worker, native, root_patch=False, contract_v2=contract_v2)
    assert observed['data'] == {'store_id': 'files', 'settings_fingerprint': None}
    assert getattr(observed['prepared'], 'request_equivalence', None) is None
    assert root.prepared_request_equivalence is None
    assert root.auto_available is available


async def test_root_dto_decodes_old_result_without_facts(preparation):
    worker, native = preparation()
    prepared = await prepare(native, worker, {'store_id': 'files'})
    payloads = await DataConverter.default.encode([prepared])
    body = json.loads(payloads[0].data)
    body.pop('request_equivalence', None)
    payload = Payload(metadata=dict(payloads[0].metadata), data=json.dumps(body).encode())
    [decoded] = await DataConverter.default.decode([payload], [PreparedContext])
    assert decoded.request_equivalence is None


@pytest.mark.parametrize('problem', ['missing', 'foreign_scope', 'child_scope', 'routes', 'groups'])
async def test_new_root_rejects_missing_or_invalid_facts_before_routing(monkeypatch, preparation, problem):
    worker, native = preparation()

    def corrupt(prepared):
        facts = PreparedRequestEquivalence(worker.run_id,
            tuple(MainRequestRef(a, 'shared-model') for a in ('fast', 'mid', 'big')), (0, 1, 2))
        if problem == 'missing':
            facts = None
        elif problem in ('foreign_scope', 'child_scope'):
            facts = replace(facts, context_id=problem)
        elif problem == 'routes':
            facts = replace(facts, routes=(MainRequestRef('unknown', 'shared-model'), *facts.routes[1:]))
        else:
            facts = replace(facts, groups=(False, 1, 2))
        prepared.request_equivalence = facts
        return prepared

    with pytest.raises(ExecutionFailure, match='routing|equivalence'):
        await root_turn(monkeypatch, worker, native, change=corrupt)


@pytest.mark.parametrize('flag', [1, 'true', None])
async def test_root_prepare_rejects_invalid_requirement_flag(preparation, flag):
    worker, native = preparation()
    with pytest.raises(ExecutionFailure):
        await prepare(native, worker, {'store_id': 'files', 'require_request_equivalence': flag})


@pytest.mark.parametrize('mode', ['light', 'default', 'max'])
async def test_prepared_root_preserves_manual_level(monkeypatch, preparation, mode):
    worker, native = preparation(mode=mode)
    root, _ = await root_turn(monkeypatch, worker, native)
    assert root.model_router.live_level() == mode
    assert root.model_router.active_level() == mode
    assert root.prepared_request_equivalence.groups == (0, 1, 2)


async def test_child_reconstructs_recorded_parent_facts_with_separate_child_scope(monkeypatch, preparation):
    import gc_core_temporalio.child_workflow as child_module
    from gc_core.auto_routing.request_equivalence import prepare_request_equivalence
    from gc_core.states import State
    from gc_core.tools.subagents.runner_transfer import capture_subagent_transfer
    from gc_core_temporalio.child_contracts import ChildRequest, PreparedChild

    parent, native = preparation()
    prepared_parent = await prepare(native, parent, {'store_id': 'files', 'require_request_equivalence': True})
    child, _ = preparation()
    child.run_id = 'child-scope'
    prepared_child = await prepare(native, child, {'store_id': 'files'})
    child_facts = prepare_request_equivalence(child, context_id=child.run_id)
    prepared = PreparedChild(prepared_child, None, request_equivalence=child_facts)
    state = State(verbose=False, model='m', tools_spec=[], tools_descr=[], skills_list=[],
                  max_steps=5, system_prompt_id='v1', use_server_subagents=False)
    observed = {}

    class Operations(SimpleNamespace):
        def __init__(self, *_):
            hooks = ('prompt_loader', 'state_preparer', 'guard_probe', 'tool_effect',
                     'skills_loader', 'mcp_cleanup', 'prompt_resource', 'summary_trace_sink', 'project_memory_io')
            super().__init__(**dict.fromkeys(hooks), activity_settings=DEFAULT_ACTIVITY_SETTINGS)

        async def call(self, *_):
            return prepared

        async def release_context(self):
            pass

    async def arun(runner, *_args, **_kwargs):
        observed['parent'] = runner._ctx
        observed['child'] = runner._preparation.ctx
        return 'done'

    async def signal(*_args):
        pass

    monkeypatch.setattr(child_module, 'WorkflowOperations', Operations)
    monkeypatch.setattr(child_module.SubagentRunner, 'arun', arun)
    monkeypatch.setattr(child_module.workflow, 'patched', lambda _: True)
    monkeypatch.setattr(child_module.workflow, 'info', lambda: SimpleNamespace(workflow_id='child', run_id='child-run'))
    monkeypatch.setattr(child_module.workflow, 'get_external_workflow_handle', lambda _: SimpleNamespace(signal=signal))
    request = ChildRequest(RunRequest('test', 'files', 'test', 'thread', ['task']), 'parent',
        prepared_parent, state.to_execution_snapshot(), capture_subagent_transfer(state, parent),
        {'frozen_level': 'default', 'frozen_live_level': 'auto', 'force_legacy': False, 'independent_auto': True},
        'subagent_worker', {}, 'parent-call', 'owner', 1, 0)
    result = await child_module.UniversalSubagentWorkflow().run(request)
    assert result.output == 'done'
    assert observed['parent'].auto_available is True
    assert observed['parent'].routing_params.safe_level == 'auto'
    assert observed['parent'].prepared_request_equivalence == prepared_parent.request_equivalence
    assert observed['child'].prepared_request_equivalence == child_facts
    assert observed['parent'].run_id != observed['child'].run_id
