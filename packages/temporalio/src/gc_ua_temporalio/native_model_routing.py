"""Public router identity; transport configuration is resolved only on worker."""

from gc_core.model_router import ModelRouter


def public_router_identity(router):
    override = router._unbound_override
    return {
        "level": router.active_level(),
        "force_legacy": router.force_legacy,
        "unbound_override": None
        if override is None
        else {
            "alias": override.alias,
            "model": override.model,
            "source": override.source,
        },
    }


def materialize_router_identity(identity, ctx):
    """Resolve an existing configured alias through the ordinary ModelRouter.

    The workflow supplies neither URL nor auth nor backend overrides. Exact
    consumer bindings still take precedence over the child's unbound override.
    """
    if identity is None:
        return ctx.model_router
    if set(identity) != {"level", "force_legacy", "unbound_override"}:
        raise ValueError("Unexpected native router identity fields")
    if (
        not isinstance(identity["level"], str)
        or identity["force_legacy"] is not ctx.model_router.force_legacy
    ):
        raise ValueError("Native router identity differs from pinned policy")
    router = ModelRouter(
        ctx.config_dict,
        level_provider=lambda: identity["level"],
        breaker=ctx.model_router.breaker,
        force_legacy=identity["force_legacy"],
    )
    override = identity["unbound_override"]
    if override is None:
        return router
    if set(override) != {"alias", "model", "source"} or not isinstance(
        override["source"], str
    ):
        raise ValueError("Unexpected native model override fields")
    resolved = (
        router._resolve_legacy(source=override["source"])
        if override["alias"] is None
        else router._resolve_alias(
            override["alias"], source=override["source"], check_breaker=False
        )
    )
    if resolved.model != override["model"]:
        raise ValueError(
            "Native model identity differs from pinned configuration"
        )
    return router.with_override(resolved)
