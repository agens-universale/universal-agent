"""AppendToFile must behave 1:1 regardless of the physical I/O backend.

VERIFICATION METHOD: differential (oracle) testing. The base tool against a real temp
file is the oracle — the same base tool driven by an in-memory FakeBackend must produce
an IDENTICAL envelope and IDENTICAL resulting file content for the same input.
`_run_both` feeds one case through both backends.
"""

import json

import pytest
from gc_core.backends.local import LocalFsBackend
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_testkit.harbor_fakes import FakeBackend, bind_os_backend, strip_path_fields

EXISTING_CONTENT = "hello world"


def _harbor(files):
    t = AppendToFile.__new__(AppendToFile)
    return bind_os_backend(t, FakeBackend(files))


def _base():
    t = AppendToFile.__new__(AppendToFile)
    return bind_os_backend(t, LocalFsBackend())


def _run_both(tmp_path, existing_content, **kwargs):
    """Run the same case through base (real fs) and harbor (fake backend).

    existing_content=None means the file is absent (for not_found cases).
    Returns (base_env, base_content, harbor_env, harbor_content).
    """
    f = tmp_path / "f.txt"
    files = {}
    if existing_content is not None:
        f.write_text(existing_content, encoding="utf-8")
        files["/work/f.txt"] = existing_content

    base_env = json.loads(_base()(path=str(f), **kwargs))
    base_content = f.read_text(encoding="utf-8") if f.exists() else None

    h = _harbor(files)
    harbor_env = json.loads(h(path="/work/f.txt", **kwargs))
    harbor_content = h._backend.files.get("/work/f.txt")

    return base_env, base_content, harbor_env, harbor_content


# Each entry: (id, existing_content_or_None, call_kwargs)
_CASES = [
    (
        "append_default_separator",
        EXISTING_CONTENT,
        {"content": "appended"},
    ),
    (
        "append_custom_separator",
        EXISTING_CONTENT,
        {"content": "appended", "separator": "---"},
    ),
    (
        "append_empty_separator_explicit",
        EXISTING_CONTENT,
        {"content": "appended", "separator": ""},
    ),
    (
        "missing_file_create_false_fails",
        None,
        {"content": "data"},
    ),
    (
        "missing_file_create_true",
        None,
        {"content": "data", "create_if_missing": True},
    ),
    (
        "make_dirs",
        None,
        {"content": "data", "create_if_missing": True, "make_dirs": True},
    ),
    (
        "missing_file_create_true_parent_absent_make_dirs_false",
        None,
        {"content": "data", "create_if_missing": True, "make_dirs": False},
    ),
]


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_identical_to_base(tmp_path, case):
    _id, existing_content, kw = case

    # make_dirs case: place the file in a subdir that does not exist yet
    if _id == "missing_file_create_true_parent_absent_make_dirs_false":
        import os

        subdir = tmp_path / "no_such_dir"
        base_path = str(subdir / "f.txt")
        harbor_path = "/work/no_such_dir/f.txt"

        # Base: parent does not exist on real fs → base returns parent_not_found
        t_base = _base()
        base_env = json.loads(t_base(path=base_path, **kw))

        # Harbor: FakeBackend has no files → is_dir("/work/no_such_dir") is False
        h = _harbor({})
        harbor_env = json.loads(h(path=harbor_path, **kw))

        assert strip_path_fields(harbor_env, "path") == strip_path_fields(base_env, "path"), _id
        return

    if _id == "missing_file_create_true":
        # File absent; parent exists on real fs (tmp_path).
        # Seed FakeBackend with a sentinel so is_dir("/work") returns True.
        import os

        f = tmp_path / "f.txt"
        # No file on disk (existing_content is None)
        files = {"/work/.keep": ""}  # sentinel: makes is_dir("/work") == True

        base_env = json.loads(_base()(path=str(f), **kw))
        base_content = f.read_text(encoding="utf-8") if f.exists() else None

        h = _harbor(files)
        harbor_env = json.loads(h(path="/work/f.txt", **kw))
        harbor_content = h._backend.files.get("/work/f.txt")

        assert strip_path_fields(harbor_env, "path") == strip_path_fields(base_env, "path"), _id
        assert harbor_content == base_content, _id
        return

    if _id == "make_dirs":
        import os

        subdir = tmp_path / "deep" / "nested"
        base_path = str(subdir / "f.txt")
        harbor_path = "/work/deep/nested/f.txt"

        t_base = _base()
        base_env = json.loads(t_base(path=base_path, **kw))
        base_content = open(base_path, encoding="utf-8").read() if os.path.exists(base_path) else None

        h = _harbor({})
        harbor_env = json.loads(h(path=harbor_path, **kw))
        harbor_content = h._backend.files.get(harbor_path)

        assert strip_path_fields(harbor_env, "path") == strip_path_fields(base_env, "path"), _id
        assert harbor_content == base_content, _id
        return

    b_env, b_content, h_env, h_content = _run_both(tmp_path, existing_content, **kw)
    assert strip_path_fields(h_env, "path") == strip_path_fields(b_env, "path"), _id
    assert h_content == b_content, _id


def test_default_separator_inserts_double_newline(tmp_path):
    """Default separator must be \\n\\n, NOT empty string."""
    b_env, b_content, h_env, h_content = _run_both(
        tmp_path, EXISTING_CONTENT, content="appended"
    )  # `content` here is the tool kwarg, not the file param
    assert h_content == EXISTING_CONTENT + "\n\n" + "appended"
    assert h_content == b_content


def test_explicit_empty_separator_appends_flush(tmp_path):
    """Passing separator='' must join with no separator at all."""
    b_env, b_content, h_env, h_content = _run_both(tmp_path, EXISTING_CONTENT, content="appended", separator="")
    assert h_content == EXISTING_CONTENT + "appended"
    assert h_content == b_content


def test_missing_file_not_found_envelope():
    """When file is missing and create_if_missing=False, error.type == not_found."""
    h = _harbor({})
    env = json.loads(h(path="/missing/f.txt", content="x"))
    assert env["ok"] is False
    assert env["error"]["type"] == "not_found"


def test_create_if_missing_creates_file():
    """create_if_missing=True must create the file with content when missing but parent exists."""
    # Seed a sentinel so is_dir("/work") returns True (parent "exists" in the container).
    h = _harbor({"/work/.keep": ""})
    env = json.loads(h(path="/work/new.txt", content="hello", create_if_missing=True))
    assert env["ok"] is True
    assert env["created"] is True
    assert h._backend.files["/work/new.txt"] == "\n\nhello"


def test_bytes_appended_matches_base(tmp_path):
    """bytes_appended must equal the actual on-disk delta, same as base."""
    b_env, _, h_env, _ = _run_both(tmp_path, EXISTING_CONTENT, content="abc")
    assert h_env["bytes_appended"] == b_env["bytes_appended"]
    assert h_env["bytes_appended"] > 0
