import hashlib

from gc_core.agent_edits.delete_records import DeleteBudget, delete_record


def _rec(key, before, *, mode=None, budget=None, max_chars=0):
    return delete_record(
        key,
        "t1",
        before,
        mode=mode,
        budget=budget,
        context_lines=3,
        max_chars=max_chars,
    )


def test_text_delete_carries_all_lines():
    rec = _rec("a.py", b"x\ny\nz\n", mode=0o644, budget=DeleteBudget(100))
    assert (rec.op, rec.add, rec.del_, rec.flags) == ("delete", 0, 3, [])
    assert rec.call_id is None
    assert rec.hunks.startswith("diff --git a/a.py b/a.py\n")
    assert "-x\n-y\n-z\n" in rec.hunks
    assert rec.before_sha256 == hashlib.sha256(b"x\ny\nz\n").hexdigest()
    assert rec.mode == 0o644


def test_binary_unknown_and_over_budget_have_no_lines():
    budget = DeleteBudget(4)
    assert _rec("b", b"\x00\x01", budget=budget).flags == ["no_diff"]
    unknown = _rec("c", None, budget=budget)
    assert (unknown.flags, unknown.hunks) == (["no_diff"], "")
    assert _rec("d", b"12345\n", budget=budget).flags == ["no_diff"]
    assert _rec("e", b"1\n", budget=budget).del_ == 1


def test_no_budget_means_already_reserved():
    assert _rec("a", b"x\n", budget=None).del_ == 1


def test_binary_does_not_spend_budget():
    budget = DeleteBudget(2)
    _rec("b", b"\x00\x01", budget=budget)
    assert budget.take(2) is True


def test_long_delete_truncated_by_max_chars():
    rec = _rec(
        "a", b"line\n" * 1000, budget=DeleteBudget(10**6), max_chars=200
    )
    assert rec.del_ == 1000
    assert "truncated" in rec.flags
