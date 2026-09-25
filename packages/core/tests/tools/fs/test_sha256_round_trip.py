"""End-to-end regression: sha256 returned by mutating fs tools must be accepted
as ``expected_file_sha256`` by the next mutating op — i.e. an agent must never
catch ``stale_file`` from its own write.

Context — defect D-3 (commit 259eb2ad, "sha256 in fs envelopes"): previously
mutating tools returned no hash, so an agent holding the sha from its last read
hit stale_file right after its own write (5 self-inflicted retries in the 1C
bench trace). The fix made every mutating envelope carry sha256. Three later
commits (1887765c "encoding on file ops", 333940b8 "fix encoding harbor",
6dabdecd "limits for search/read") rewrote the encode/hash path of write and
append and added encoding-aware decode on the edit side — so the sha hand-off
between write/append and apply_file_edits/search_and_replace could silently
regress on non-UTF-8 content or newline normalization. The existing
test_1c_trace_core_fixes.py only checks each tool's sha in isolation against
the file on disk; it never chains write_sha -> expected_file_sha256. These
tests close that gap across encodings, newline modes, append, and the
write->edit->edit chain.
"""

import hashlib
import json

import pytest
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.helpers.tool_ctx import with_local_ctx

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _disk_sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(tool, *, path, content, **kw) -> dict:
    return json.loads(tool(path=str(path), content=content, **kw))


def _edit(tool, *, path, edits, expected_file_sha256, **kw) -> dict:
    return json.loads(
        tool(
            path=str(path),
            edits=edits,
            expected_file_sha256=expected_file_sha256,
            **kw,
        )
    )


def _sr(tool, *, path, search, replace, expected_file_sha256, **kw) -> dict:
    return json.loads(
        tool(
            path=str(path),
            search=search,
            replace=replace,
            expected_file_sha256=expected_file_sha256,
            **kw,
        )
    )


def _assert_fresh_sha_ok(env: dict, path, *, applied_expected=True) -> str:
    """A mutating op must succeed, return the real on-disk sha, and carry no
    stale_file error. Returns the new sha for chaining."""
    assert env["ok"] is True, env
    # "applied" only exists on edit/search-and-replace envelopes, not write/append
    if "applied" in env:
        assert env["applied"] is applied_expected, env
    assert env.get("error") is None, env
    sha = env["sha256"]
    assert sha is not None, env
    assert sha == _disk_sha(path), (
        f"returned sha256 != on-disk sha256; envelope sha={sha}"
    )
    assert "stale_file" not in json.dumps(env), env
    return sha


# --------------------------------------------------------------------------- #
# 1. write -> apply_file_edits: own write sha must be accepted (no stale_file)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content,search,replace",
    [
        ("hello world\n", "o", "0"),
        ("Привет, мир\nещё строка\n", "мир", "earth"),
        ("unicode: ✓ ★ 日本語 — em-dash here\n", "unicode", "UNICODE"),
    ],
    ids=["ascii", "cyrillic", "mixed_unicode"],
)
def test_write_sha_accepted_by_apply_file_edits(
    tmp_path, content, search, replace
):
    p = tmp_path / "a.txt"
    w = _write(with_local_ctx(WriteToFile), path=p, content=content)
    sha = _assert_fresh_sha_ok(w, p)

    e = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": search, "replace": replace}],
        expected_file_sha256=sha,
    )
    assert e["ok"] is True, e
    assert e["applied"] is True, e
    assert e["error"] is None, e
    assert "stale_file" not in json.dumps(e), e
    assert e["sha256"] == _disk_sha(p)


def test_write_then_edit_then_edit_chain_never_stale(tmp_path):
    """Two consecutive edits, each feeding the previous op's returned sha as
    expected_file_sha256 — the exact agent loop that D-3 fixed."""
    p = tmp_path / "chain.txt"
    w = _write(
        with_local_ctx(WriteToFile), path=p, content="alpha beta gamma\n"
    )
    sha0 = _assert_fresh_sha_ok(w, p)

    e1 = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": "beta", "replace": "BETA"}],
        expected_file_sha256=sha0,
    )
    sha1 = _assert_fresh_sha_ok(e1, p)

    e2 = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": "gamma", "replace": "GAMMA"}],
        expected_file_sha256=sha1,
    )
    _assert_fresh_sha_ok(e2, p)
    assert p.read_text(encoding="utf-8") == "alpha BETA GAMMA\n"


# --------------------------------------------------------------------------- #
# 2. write -> search_and_replace: same hand-off
# --------------------------------------------------------------------------- #


def test_write_sha_accepted_by_search_and_replace(tmp_path):
    p = tmp_path / "sr.txt"
    w = _write(with_local_ctx(WriteToFile), path=p, content="foo bar baz\n")
    sha = _assert_fresh_sha_ok(w, p)

    r = _sr(
        with_local_ctx(SearchAndReplace),
        path=p,
        search="bar",
        replace="BAR",
        expected_file_sha256=sha,
    )
    assert r["ok"] is True, r
    assert r["error"] is None, r
    assert "stale_file" not in json.dumps(r), r
    assert r["sha256"] == _disk_sha(p)
    assert p.read_text(encoding="utf-8") == "foo BAR baz\n"


# --------------------------------------------------------------------------- #
# 3. append -> apply_file_edits: append sha must be accepted too
# --------------------------------------------------------------------------- #


def test_append_sha_accepted_by_apply_file_edits(tmp_path):
    p = tmp_path / "app.txt"
    _write(with_local_ctx(WriteToFile), path=p, content="seed\n")

    a = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="tail line", separator="\n"
        )
    )
    assert a["ok"] is True, a
    assert a["sha256"] == _disk_sha(p), a
    sha = a["sha256"]

    e = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": "tail", "replace": "TAIL"}],
        expected_file_sha256=sha,
    )
    assert e["ok"] is True, e
    assert e["applied"] is True, e
    assert e["error"] is None, e
    assert "stale_file" not in json.dumps(e), e
    assert e["sha256"] == _disk_sha(p)


def test_append_then_edit_chain_never_stale(tmp_path):
    p = tmp_path / "appchain.txt"
    _write(with_local_ctx(WriteToFile), path=p, content="base")
    # grow the file in two append steps, editing after each
    a1 = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="one", separator="\n"
        )
    )
    assert a1["sha256"] == _disk_sha(p)

    e1 = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": "one", "replace": "ONE"}],
        expected_file_sha256=a1["sha256"],
    )
    sha1 = _assert_fresh_sha_ok(e1, p)

    a2 = json.loads(
        with_local_ctx(AppendToFile)(
            path=str(p), content="two", separator="\n"
        )
    )
    assert a2["sha256"] == _disk_sha(p)

    e2 = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": "two", "replace": "TWO"}],
        expected_file_sha256=a2["sha256"],
    )
    _assert_fresh_sha_ok(e2, p)
    assert p.read_text(encoding="utf-8") == "base\nONE\nTWO"


# --------------------------------------------------------------------------- #
# 4. encodings & newline normalization — the path the 1887765c rewrite touched
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "encoding,content,search,replace",
    [
        ("utf-8", "Привет, мир\nline two\n", "мир", "world"),
        ("utf-8-sig", "Привет, мир\nline two\n", "мир", "world"),
        ("cp1251", "Привет, мир\nline two\n", "мир", "world"),
        ("latin-1", "Hello, world\nline two\n", "world", "monde"),
    ],
    ids=["utf-8", "utf-8-sig", "cp1251", "latin-1"],
)
def test_write_with_encoding_sha_accepted_by_edit(
    tmp_path, encoding, content, search, replace
):
    p = tmp_path / f"enc_{encoding.replace('-', '_')}.txt"

    w = _write(
        with_local_ctx(WriteToFile), path=p, content=content, encoding=encoding
    )
    assert w["ok"] is True, w
    assert w["encoding_used"] == encoding, w
    assert w["sha256"] == _disk_sha(p), w
    sha = w["sha256"]

    e = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": search, "replace": replace}],
        expected_file_sha256=sha,
        encoding=encoding,
    )
    assert e["ok"] is True, e
    assert e["applied"] is True, e
    assert e["error"] is None, e
    assert "stale_file" not in json.dumps(e), e
    assert e["sha256"] == _disk_sha(p)


@pytest.mark.parametrize("newline", ["preserve", "lf", "crlf"])
def test_write_with_newline_mode_sha_accepted_by_edit(tmp_path, newline):
    # content with mixed line endings to exercise normalization
    content = "first\r\nsecond\rthird\n"
    p = tmp_path / f"nl_{newline}.txt"

    w = _write(
        with_local_ctx(WriteToFile), path=p, content=content, newline=newline
    )
    assert w["ok"] is True, w
    assert w["sha256"] == _disk_sha(p), w
    sha = w["sha256"]

    e = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": "second", "replace": "2nd"}],
        expected_file_sha256=sha,
    )
    assert e["ok"] is True, e
    assert e["applied"] is True, e
    assert e["error"] is None, e
    assert "stale_file" not in json.dumps(e), e
    assert e["sha256"] == _disk_sha(p)


# --------------------------------------------------------------------------- #
# 5. negative control: a genuinely stale sha is still rejected
#    (ensures the tests above aren't trivially passing because the guard is off)
# --------------------------------------------------------------------------- #


def test_genuinely_stale_sha_is_rejected(tmp_path):
    """Write a file, then mutate it out-of-band, then feed the old sha — must
    get stale_file. Proves the acceptance tests above are meaningful."""
    p = tmp_path / "stale.txt"
    w = _write(with_local_ctx(WriteToFile), path=p, content="original\n")
    old_sha = w["sha256"]

    # out-of-band mutation (simulates another process / the agent's own earlier
    # untracked change between the write and the edit)
    p.write_bytes(b"original\nsneaky extra line\n")

    e = _edit(
        with_local_ctx(ApplyFileEdits),
        path=p,
        edits=[{"search": "original", "replace": "O"}],
        expected_file_sha256=old_sha,
    )
    assert e["ok"] is False, e
    assert e["applied"] is False, e
    assert e["error"] is not None, e
    assert e["error"]["type"] == "stale_file", e
    assert e["error"]["expected_file_sha256"] == old_sha.lower()
    assert e["error"]["actual_file_sha256"] == _disk_sha(p)


def test_genuinely_stale_sha_rejected_search_and_replace(tmp_path):
    p = tmp_path / "stale_sr.txt"
    w = _write(with_local_ctx(WriteToFile), path=p, content="aaa bbb\n")
    old_sha = w["sha256"]

    p.write_bytes(b"aaa bbb\nccc\n")

    r = _sr(
        with_local_ctx(SearchAndReplace),
        path=p,
        search="bbb",
        replace="B",
        expected_file_sha256=old_sha,
    )
    assert r["ok"] is False, r
    assert r["error"] is not None, r
    assert r["error"]["type"] == "stale_file", r
