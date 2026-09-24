"""
Tests for stable, accurate projectDirListing per SPEC:
docs_md/actual/2026-07-21-project-dir-listing-stable.md

Covers:
- D2: stable sort (dirs-first, then casefold)
- D3: limit counts shown names, not raw index
- D4: cut_second >= 120
- D5: level markers + one final hint only on truncation
- D7: get_listing and get_listing_from_backend agree
"""
from gc_core.utils.project_data import cut_second, get_listing, get_listing_from_backend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Stand-in FsBackend: returns a predefined listdir, no real FS."""

    is_sandbox = True

    def __init__(self, tree: dict[str, list[tuple[str, bool]]]):
        self._tree = tree

    async def listdir(self, path: str) -> list[tuple[str, bool]]:
        return self._tree.get(path, [])

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name


def _root_names(listing: str) -> list[str]:
    """Root-level lines (no indent), excluding final hint."""
    names = []
    for line in listing.split("\n"):
        if not line or line.startswith("    "):
            continue
        if line.startswith("[") and "truncated" in line.lower():
            continue
        names.append(line.strip())
    return names


def _children_of(listing: str, dirname: str) -> list[str]:
    """Indented children under a root dir like ``src/``."""
    target = dirname if dirname.endswith("/") else dirname + "/"
    lines = listing.split("\n")
    children: list[str] = []
    in_block = False
    for line in lines:
        if line == target:
            in_block = True
            continue
        if in_block:
            if line.startswith("    "):
                children.append(line.strip())
            else:
                break
    return children


# ---------------------------------------------------------------------------
# D4 — cut_second default
# ---------------------------------------------------------------------------

def test_cut_second_at_least_120():
    assert cut_second >= 120, f"cut_second={cut_second}, expected >= 120"


async def test_gc_dir_excluded_from_listing(tmp_path):
    """``.gc`` is project-local state (denied except skills via tools) — hide from map."""
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "skills").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").touch()

    listing = await get_listing(str(tmp_path))
    assert ".gc" not in listing
    assert "src/" in listing
    assert "README.md" in listing


# ---------------------------------------------------------------------------
# D2 — stable sort: dirs first, casefold, repeatable
# ---------------------------------------------------------------------------

async def test_get_listing_stable_order_repeatable(tmp_path):
    """Multiple calls return the same order."""
    names = ["zzz_file", "aaa_dir", "M_file", "m_dir", "File2", "file1"]
    for name in names:
        p = tmp_path / name
        p.mkdir() if name.endswith("_dir") else p.touch()

    assert await get_listing(str(tmp_path)) == await get_listing(str(tmp_path))


async def test_get_listing_sort_order_dirs_first_then_alpha(tmp_path):
    """Dirs before files; within each group sorted casefold."""
    entries = [
        ("zzz_file", False),
        ("aaa_dir", True),
        ("M_dir", True),
        ("File1", False),
        ("aaa_file", False),
        ("m_file", False),
        ("Zzz_file", False),
    ]
    for name, is_dir in entries:
        p = tmp_path / name
        p.mkdir() if is_dir else p.touch()

    listing = await get_listing(str(tmp_path))
    root_names = _root_names(listing)

    dirs = [n for n in root_names if n.endswith("/")]
    files = [n for n in root_names if not n.endswith("/") and not n.startswith("...")]
    assert dirs and files
    assert max(root_names.index(d) for d in dirs) < min(root_names.index(f) for f in files)
    assert dirs == sorted(dirs, key=lambda n: n.casefold())
    assert files == sorted(files, key=lambda n: n.casefold())


async def test_backend_listing_agrees_with_filesystem_listing(tmp_path):
    """D7: get_listing and get_listing_from_backend produce identical structure."""
    names = ["b_file", "A_dir", "c_file", "z_dir", "M_file"]
    for name in names:
        p = tmp_path / name
        p.mkdir() if name.endswith("_dir") else p.touch()
        if name.endswith("_dir"):
            (p / "nested.txt").touch()

    fs_listing = await get_listing(str(tmp_path))

    tree = {str(tmp_path): [(n, (tmp_path / n).is_dir()) for n in names]}
    for name in names:
        if name.endswith("_dir"):
            tree[str(tmp_path / name)] = [("nested.txt", False)]
    backend = _FakeBackend(tree)
    be_listing = await get_listing_from_backend(backend, str(tmp_path))

    assert fs_listing == be_listing


# ---------------------------------------------------------------------------
# D3 — limit counts shown names, not raw index
# ---------------------------------------------------------------------------

async def test_truncation_ignores_stop_folders_in_counter(tmp_path, monkeypatch):
    """
    Classic as-is bug: stop_folders early in listdir inflate enumerate index,
    so truncate fires before any (or enough) visible names are shown.

    With cut_first=2 and 3 stop folders + 4 visible files, shown/total must be
    2/4 (visible only), not a premature empty/near-empty cut.
    """
    from gc_core.utils import project_data

    for stop in (".git", "node_modules", "__pycache__"):
        (tmp_path / stop).mkdir()
    for name in ("d.txt", "a.txt", "c.txt", "b.txt"):
        (tmp_path / name).touch()

    monkeypatch.setattr(project_data, "cut_first", 2)
    listing = await get_listing(str(tmp_path))

    assert "(truncated 2/4)" in listing, f"Expected shown/total over visible only:\n{listing}"
    root = _root_names(listing)
    visible = [n for n in root if not n.startswith("...")]
    assert visible == ["a.txt", "b.txt"], f"Expected first 2 sorted visible:\n{listing}"
    assert "projectDirListing truncated" in listing


async def test_truncation_uses_shown_count_not_raw_index(tmp_path, monkeypatch):
    """Marker reflects filtered total, not raw listdir length."""
    from gc_core.utils import project_data

    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).touch()

    # Raw listdir length = 5; filtered visible = 3. Limit 1 → marker 1/3, not 1/5.
    monkeypatch.setattr(project_data, "cut_first", 1)
    listing = await get_listing(str(tmp_path))

    assert "(truncated 1/3)" in listing, f"Expected 1/3 over filtered names:\n{listing}"
    assert "(truncated 1/5)" not in listing


# ---------------------------------------------------------------------------
# D5 — truncate markers
# ---------------------------------------------------------------------------

async def test_no_truncation_marker_when_under_limit(tmp_path, monkeypatch):
    """When no truncation occurs, no level markers and no final hint."""
    from gc_core.utils import project_data

    for name in ["a.txt", "b.py", "c_dir"]:
        p = tmp_path / name
        p.mkdir() if name.endswith("_dir") else p.touch()
        if name.endswith("_dir"):
            (p / "inner.py").touch()

    monkeypatch.setattr(project_data, "cut_second", 10)
    listing = await get_listing(str(tmp_path))

    assert "(truncated" not in listing
    assert "listing truncated" not in listing.lower()
    assert "..." not in listing


async def test_level_truncation_marker_shown_total(tmp_path, monkeypatch):
    """Truncated level shows ``...(truncated N/total)``, not bare ``...``."""
    from gc_core.utils import project_data

    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt", "g.txt"]:
        (tmp_path / name).touch()

    monkeypatch.setattr(project_data, "cut_first", 3)
    listing = await get_listing(str(tmp_path))

    assert "(truncated 3/7)" in listing
    orphan_dots = [l for l in listing.split("\n") if l.strip() in ("...", "...(truncated")]
    assert not orphan_dots


async def test_final_hint_appears_only_when_truncation_occurred(tmp_path, monkeypatch):
    """Final hint only when at least one level was truncated; then last line."""
    from gc_core.utils import project_data

    monkeypatch.setattr(project_data, "cut_first", 1000)
    listing_ok = await get_listing(str(tmp_path))
    assert "listing truncated" not in listing_ok.lower()

    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]:
        (tmp_path / name).touch()

    monkeypatch.setattr(project_data, "cut_first", 2)
    listing_trunc = await get_listing(str(tmp_path))
    lines = [l for l in listing_trunc.strip().split("\n") if l.strip()]
    assert "listing truncated" in lines[-1].lower()
    assert sum(1 for l in lines if "listing truncated" in l.lower()) == 1


async def test_final_hint_once_even_with_multiple_truncations(tmp_path, monkeypatch):
    """Root + child truncation → still exactly one final hint."""
    from gc_core.utils import project_data

    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"]:
        (tmp_path / name).touch()

    child = tmp_path / "big_dir"
    child.mkdir()
    for i in range(10):
        (child / f"file{i}.txt").touch()

    monkeypatch.setattr(project_data, "cut_first", 3)
    monkeypatch.setattr(project_data, "cut_second", 5)
    listing = await get_listing(str(tmp_path))

    hint_lines = [l for l in listing.split("\n") if "listing truncated" in l.lower()]
    assert len(hint_lines) == 1


async def test_backend_and_fs_agree_on_truncation_markers(tmp_path, monkeypatch):
    """D7: both entrypoints emit the same markers when truncating."""
    from gc_core.utils import project_data

    entries = [f"file{i}.txt" for i in range(10)]
    for name in entries:
        (tmp_path / name).touch()

    monkeypatch.setattr(project_data, "cut_first", 4)
    fs_listing = await get_listing(str(tmp_path))
    backend = _FakeBackend({str(tmp_path): [(n, False) for n in entries]})
    be_listing = await get_listing_from_backend(backend, str(tmp_path))

    assert fs_listing == be_listing


# ---------------------------------------------------------------------------
# Integration: children of src/ under cut_second (the original bug surface)
# ---------------------------------------------------------------------------

async def test_src_like_children_both_tui_dirs_visible(tmp_path, monkeypatch):
    """
    Acceptance §6: project root listing must show both ``tui/`` and
    ``agent_edits/`` as children of ``src/`` when src has ~53 packages
    (cut_second=120).

    Names are created in intentionally bad order; sort + raised limit must keep
    both packages visible. With legacy cut_second=30, ``tui/`` would be truncated.
    """
    from gc_core.utils import project_data

    # Intentionally bad creation order: tui/agent_edits last, modules reverse.
    # 50 modules + core + tui + agent_edits = 53 (matches typical src/).
    child_names = [f"module{i}" for i in range(49, -1, -1)] + [
        "tui",
        "agent_edits",
        "core",
    ]
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    for name in child_names:
        (src_dir / name).mkdir()
        if name in ("tui", "agent_edits"):
            (src_dir / name / "module.py").touch()
    (tmp_path / "README.md").touch()

    listing = await get_listing(str(tmp_path))
    src_children = _children_of(listing, "src/")

    assert "tui/" in src_children, f"tui/ missing under src/:\n{listing}"
    assert "agent_edits/" in src_children, f"agent_edits/ missing under src/:\n{listing}"
    assert not any("(truncated" in c for c in src_children), (
        f"src/ children must not be truncated at cut_second={cut_second}:\n{listing}"
    )

    # Guard: old default would hide tui/ (last among 53 dirs after casefold sort).
    monkeypatch.setattr(project_data, "cut_second", 30)
    listing_old = await get_listing(str(tmp_path))
    old_children = _children_of(listing_old, "src/")
    assert "tui/" not in old_children, (
        "fixture invalid: cut_second=30 should hide tui/ so the 120 case is meaningful"
    )
    assert any("30/53" in c for c in old_children), f"Expected level marker:\n{listing_old}"
