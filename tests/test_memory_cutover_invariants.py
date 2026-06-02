"""Merge-blocking invariants for working_memory cutover (spec steps 0 and 6)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Единственные допустимые вхождения memory_files вне deprecated-зоны (fail-fast reject).
_ALLOWED_MEMORY_FILES_RELPATHS = frozenset(
    {
        "src/tools/deprecated/memory_files.py",
        "src/persistence/schema.py",
    }
)


def _relpath(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_memory_files_only_in_allowed_locations() -> None:
    """rg \"memory_files\" src/ — только tools/deprecated и schema reject."""
    unexpected: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "memory_files" not in path.read_text(encoding="utf-8"):
            continue
        rel = _relpath(path)
        if rel not in _ALLOWED_MEMORY_FILES_RELPATHS:
            unexpected.append(rel)
    assert unexpected == []


def test_no_journal_in_summarize() -> None:
    """rg \"journal\" src/summarize/ — пусто (legacy memory step contract)."""
    summarize = SRC / "summarize"
    hits: list[str] = []
    for path in sorted(summarize.rglob("*.py")):
        if "journal" in path.read_text(encoding="utf-8"):
            hits.append(_relpath(path))
    assert hits == []
