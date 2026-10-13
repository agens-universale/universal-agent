"""Prepared request identity survives removal of worker-only fields."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from gc_core.auto_routing.levels import auto_available, resolve_routing_params
from gc_core.auto_routing.policy import RoutingParams
from gc_core.auto_routing.request_equivalence import (
    MainRequestRef, prepare_request_equivalence, validate_request_equivalence,
)
from gc_core.model_router import ModelRouter
from gc_core.processing_responses import _floor_target
from gc_testkit.settings import make_settings


def configs(difference):
    models = {}
    for i, alias in enumerate(('fast', 'mid', 'big')):
        entry = {'model': 'shared-model', 'url': 'https://shared.invalid/v1',
                 'token': 'private-common-token'}
        if difference == 'url':
            entry['url'] = f'https://private-{alias}.invalid/v1'
        if difference == 'token':
            entry['token'] = f'private-{alias}-token'
        if difference == 'headers':
            entry['backend'] = {'chat_completion_headers': {'X-Private': f'private-{alias}'}}
        if difference == 'proxy':
            entry['backend'] = {'proxy': f'http://private-{alias}.invalid'}
        if difference == 'ssl':
            entry['backend'] = {'use_ssl': i != 1}
        models[alias] = entry
    full = make_settings({'models': models,
        'slots': {'light': 'fast', 'default': 'mid', 'max': 'big'},
        'level': 'auto', 'backend.api_mode': 'chat_completions'})
    public_models = {alias: {'model': 'shared-model'} for alias in models}
    public = full.with_overrides({'models': public_models})
    return full, public


def prepared_ctx(full, public):
    facts = prepare_request_equivalence(full, context_id='child-1')
    ctx = SimpleNamespace(settings=public.settings, run_id='child-1',
        prepared_request_equivalence=facts, routing_contract_v2=True)
    ctx.model_router = ModelRouter(ctx, level_provider=lambda: 'light')
    return ctx


@pytest.mark.parametrize('difference', ['url', 'token', 'headers', 'proxy', 'ssl'])
def test_private_difference_preserves_auto_pair_and_floor(difference):
    full, public = configs(difference)
    ctx = prepared_ctx(full, public)
    assert auto_available(public, force_legacy=False) is False
    assert auto_available(ctx, force_legacy=False) is True
    assert resolve_routing_params(ctx).safe_level == 'auto'
    weak = ctx.model_router.resolve('main', level_override='light')
    assert _floor_target(ctx, 'light', weak, RoutingParams(safe_level='default')) == 'default'


def test_equivalent_aliases_do_not_create_auto_or_floor():
    full, public = configs('same')
    ctx = prepared_ctx(full, public)
    assert auto_available(ctx, force_legacy=False) is False
    weak = ctx.model_router.resolve('main', level_override='light')
    assert _floor_target(ctx, 'light', weak, RoutingParams(safe_level='default')) is None


def test_legacy_comparison_ignores_prepared_equivalence():
    full, public = configs('same')
    ctx = prepared_ctx(full, public)
    ctx.routing_contract_v2 = False
    assert auto_available(ctx, force_legacy=False, legacy_call_kits=True) is True
    weak = ctx.model_router.resolve('main', level_override='light')
    assert _floor_target(ctx, 'light', weak, RoutingParams(safe_level='default')) == 'default'


@pytest.mark.parametrize('change', [
    {'context_id': 'another-child'}, {'routes': ()}, {'groups': (0, 1)},
    {'groups': (0, True, 2)}, {'groups': (-1, 0, 1)},
    {'groups': (None, 0, 1)},
    {'routes': (MainRequestRef('unknown', 'shared-model'),
               MainRequestRef('mid', 'shared-model'), MainRequestRef('big', 'shared-model'))},
])
def test_invalid_facts_fail_before_routing(change):
    full, public = configs('url')
    ctx = prepared_ctx(full, public)
    ctx.prepared_request_equivalence = replace(ctx.prepared_request_equivalence, **change)
    with pytest.raises(ValueError):
        validate_request_equivalence(ctx, ctx.prepared_request_equivalence, context_id=ctx.run_id)
    with pytest.raises(ValueError):
        auto_available(ctx, force_legacy=False)


def test_prepared_payload_contains_no_private_values_or_fingerprints():
    full, public = configs('token')
    ctx = prepared_ctx(full, public)
    encoded = json.dumps(asdict(ctx.prepared_request_equivalence))
    for entry in full.settings.models.values():
        for private in (entry.url, entry.token):
            value = str(private)
            assert value not in encoded
            assert sha256(value.encode()).hexdigest() not in encoded
    assert 'authorization' not in encoded.lower()


def test_missing_slot_uses_the_same_fallback_identity():
    full = make_settings({'models': {'only': {'model': 'single'}}, 'level': 'auto'})
    ctx = prepared_ctx(full, full)
    assert auto_available(ctx, force_legacy=False) is False
    assert resolve_routing_params(ctx).safe_level == 'default'
