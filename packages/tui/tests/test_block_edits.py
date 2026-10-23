"""Билдер правок блока `gc_tui.services.block_edits` (спека P4 §6, §9.9).

Один билдер для живой ленты и превью: запись журнала → `BlockEdit` модели
блоков и `DiffBlock` под блоком.
"""

from __future__ import annotations

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.thread_store import edits_journal_path

from gc_tui.services.block_edits import (
    diff_block_for,
    edit_of_call,
    ran_files_of,
)
from gc_tui.services.diff_render import copyable_hunks
from gc_tui.services.edits_lookup import EditsLookup

HUNKS = "@@ -1,2 +1,3 @@\n a\n-b\n+c\n+d\n"


def _rec(path="a.py", *, call_id="c1", op="edit", **kw) -> EditRecord:
    base = dict(
        path=path,
        turn_id="t1",
        ts=1.0,
        call_id=call_id,
        op=op,
        hunks=HUNKS,
        add=2,
        del_=1,
        before_sha256=None,
        after_sha256=None,
        flags=[],
    )
    base.update(kw)
    return EditRecord(**base)


def _lookup(tmp_path, records) -> EditsLookup:
    threads = tmp_path / "threads"
    threads.mkdir()
    journal = EditsJournal(edits_journal_path(str(threads), "cli", "th1"))
    for rec in records:
        journal.append(rec)
    return EditsLookup(str(threads), "cli", "th1", launch_root=str(tmp_path))


def test_edit_of_call_without_journal_or_record(tmp_path):
    assert edit_of_call(None, "c1") is None
    lookup = _lookup(tmp_path, [_rec(call_id="c1")])
    assert edit_of_call(lookup, "nope") is None
    assert edit_of_call(lookup, "") is None


def test_edit_of_call_counts_verb_and_copy(tmp_path):
    record = _rec(call_id="c1", op="create")
    lookup = _lookup(tmp_path, [record])
    edit = edit_of_call(lookup, "c1")
    assert (edit.key, edit.path, edit.verb) == ("c1", "a.py", "Added")
    assert (edit.add, edit.rem, edit.diff_lines) == (2, 1, 4)
    assert edit.has_record is True
    assert edit.size_bytes is None
    assert edit.copy_text == copyable_hunks(HUNKS, path="a.py")
    assert edit.record == record


def test_edit_of_call_binary_size(tmp_path):
    record = _rec(
        "logo.png",
        hunks="",
        add=0,
        del_=0,
        flags=["no_diff"],
        size_bytes=12_288,
    )
    edit = edit_of_call(_lookup(tmp_path, [record]), "c1")
    assert edit.verb == "Edited"
    assert edit.diff_lines == 0
    assert edit.size_bytes == 12_288
    assert edit.copy_text == ""


def test_ran_files_of_verbs_and_unrecorded(tmp_path):
    def shell(path, op, **kw):
        return _rec(
            path,
            call_id=None,
            op=op,
            command_call_id="x1",
            command_paths=6,
            **kw,
        )

    lookup = _lookup(
        tmp_path,
        [
            shell("new.py", "create"),
            shell("old.py", "shell"),
            shell("gone.py", "delete", hunks="", flags=["no_diff"]),
            _rec("other.py", call_id=None, op="shell", command_call_id="x2"),
        ],
    )
    files, unrecorded = ran_files_of(lookup, "x1")
    assert [(f.path, f.verb) for f in files] == [
        ("new.py", "Added"),
        ("old.py", "Edited"),
        ("gone.py", "Deleted"),
    ]
    assert [f.key for f in files] == [
        "x1:new.py",
        "x1:old.py",
        "x1:gone.py",
    ]
    assert unrecorded == 3
    assert ran_files_of(lookup, "x2")[1] == 0
    assert ran_files_of(None, "x1") == ([], 0)
    assert ran_files_of(lookup, "nope") == ([], 0)


def test_diff_block_for_is_under_block():
    block = diff_block_for(
        _rec(), rows=6, column="member", compact=False, event_no=3
    )
    assert block.has_class("diff-member")
    assert block.in_block is True
