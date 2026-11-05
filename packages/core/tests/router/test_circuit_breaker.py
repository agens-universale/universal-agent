from gc_core.model_router import CircuitBreaker


def test_fresh_breaker_is_closed():
    breaker = CircuitBreaker()
    assert breaker.is_open("flash") is False


def test_trip_opens_alias():
    breaker = CircuitBreaker()
    breaker.trip("flash", "5xx после ретраев")
    assert breaker.is_open("flash") is True


def test_trip_does_not_affect_other_aliases():
    breaker = CircuitBreaker()
    breaker.trip("flash", "5xx")
    assert breaker.is_open("glm-local") is False


def test_snapshot_reflects_tripped_aliases():
    breaker = CircuitBreaker()
    breaker.trip("flash", "5xx после ретраев")
    assert breaker.snapshot() == {"flash": "5xx после ретраев"}


def test_snapshot_empty_when_nothing_tripped():
    breaker = CircuitBreaker()
    assert breaker.snapshot() == {}


def test_restore_from_snapshot_reproduces_open_state():
    snapshot = {"flash": "401"}
    breaker = CircuitBreaker.restore(snapshot)
    assert breaker.is_open("flash") is True
    assert breaker.is_open("glm-local") is False


def test_restore_from_empty_snapshot_is_closed():
    breaker = CircuitBreaker.restore({})
    assert breaker.is_open("flash") is False


def test_reset_clears_all_tripped_aliases():
    breaker = CircuitBreaker()
    breaker.trip("flash", "5xx")
    breaker.trip("glm-local", "401")
    breaker.reset()
    assert breaker.snapshot() == {}
    assert breaker.is_open("flash") is False


def test_restored_snapshot_is_independent_copy():
    original = {"flash": "5xx"}
    breaker = CircuitBreaker.restore(original)
    breaker.trip("glm-local", "401")
    assert original == {"flash": "5xx"}


def test_replace_from_mutates_in_place_preserving_identity():
    """Restore-in-place (§8, Important-1): содержимое заменяется, объект — тот же.
    Прогретый executor держит ссылку на ЭТОТ объект; подмена ссылки осиротила бы
    его (трипы рана уходили бы в старый breaker)."""
    breaker = CircuitBreaker()
    breaker.trip("flash", "old")
    same_ref = breaker
    breaker.replace_from({"glm-local": "new"})
    assert breaker is same_ref
    assert breaker.is_open("flash") is False
    assert breaker.is_open("glm-local") is True
    assert breaker.snapshot() == {"glm-local": "new"}


def test_replace_from_empty_clears_in_place():
    breaker = CircuitBreaker()
    breaker.trip("flash", "5xx")
    breaker.replace_from({})
    assert breaker.snapshot() == {}


def test_replace_from_is_independent_copy():
    src = {"flash": "5xx"}
    breaker = CircuitBreaker()
    breaker.replace_from(src)
    breaker.trip("glm-local", "401")
    assert src == {"flash": "5xx"}


def test_breaker_reason_keeps_original_reason_of_open_alias():
    from gc_core.model_router import (
        AliasCircuitOpenError,
        ResolvedModel,
        breaker_reason,
    )

    resolved = ResolvedModel(
        alias="gc_v3",
        model="gc",
        url="http://hub",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="slot default",
    )
    exc = AliasCircuitOpenError(resolved, "ReadError")
    assert breaker_reason(exc) == "ReadError"


def test_breaker_reason_of_plain_error_is_its_text():
    from gc_core.model_router import breaker_reason

    assert breaker_reason(RuntimeError("  5xx  ")) == "5xx"
    assert breaker_reason(RuntimeError("")) == "RuntimeError"


def test_failure_text_names_open_alias_once():
    from gc_core.model_router import (
        AliasCircuitOpenError,
        ResolvedModel,
        failure_text,
    )

    resolved = ResolvedModel(
        alias="gc_v3",
        model="gc",
        url="http://hub",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="slot default",
    )
    exc = AliasCircuitOpenError(resolved, "ReadError")
    assert failure_text(exc) == "алиас 'gc_v3' в предохранителе: ReadError"
    assert failure_text(RuntimeError("")) == "RuntimeError"


def test_allow_probe_moves_open_alias_to_probe():
    b = CircuitBreaker()
    b.trip("flash", "ReadError")
    b.allow_probe()
    assert b.is_open("flash") is False
    assert b.known_unavailable("flash") == "ReadError"
    assert b.snapshot() == {}


def test_take_probe_is_one_shot():
    b = CircuitBreaker()
    b.trip("flash", "x")
    b.allow_probe()
    assert b.take_probe("flash") is True
    assert b.take_probe("flash") is False
    assert b.known_unavailable("flash") is None


def test_trip_during_probe_reopens_with_fresh_reason():
    b = CircuitBreaker()
    b.trip("flash", "old")
    b.allow_probe()
    b.trip("flash", "new")
    assert b.is_open("flash") is True
    assert b.take_probe("flash") is False
    assert b.known_unavailable("flash") == "new"


def test_replace_from_takes_state_wholesale_without_probe_rights():
    b = CircuitBreaker()
    b.trip("a", "1")
    b.trip("b", "2")
    b.allow_probe()
    b.replace_from({"a": "3"})
    assert b.is_open("a") is True and b.take_probe("a") is False
    assert b.known_unavailable("b") is None


def test_reset_clears_probe():
    b = CircuitBreaker()
    b.trip("a", "1")
    b.allow_probe()
    b.reset()
    assert b.known_unavailable("a") is None


def test_copy_keeps_both_states():
    b = CircuitBreaker()
    b.trip("a", "1")
    b.allow_probe()
    b.trip("c", "2")
    c = b.copy()
    assert c is not b
    assert c.take_probe("a") is True and c.is_open("c") is True
    assert b.take_probe("a") is True  # копия независима


def test_take_probe_is_atomic_across_threads():
    import threading

    b = CircuitBreaker()
    b.trip("a", "1")
    b.allow_probe()
    wins = []
    threads = [
        threading.Thread(target=lambda: wins.append(b.take_probe("a")))
        for _ in range(16)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


def test_open_reason_reads_only_open_aliases():
    b = CircuitBreaker()
    b.trip("a", "1")
    b.trip("p", "2")
    b.allow_probe()
    b.trip("a", "3")
    assert b.open_reason("a") == "3"
    assert b.open_reason("p") is None
    assert b.open_reason("x") is None
