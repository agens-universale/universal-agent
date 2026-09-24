"""Deterministic, pack-aware resolver for the CLI -s/--skill flag.

Pure: no I/O, no dependency on the registry or cli. Takes a SkillCatalog
snapshot + the raw flag value, returns a Resolved (exact canonical skill id)
or a Failed (the caller — cli.py — decides process exit).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# A single-character typo in a ~10-char id yields a SequenceMatcher ratio of
# roughly 0.9-0.95 (e.g. "suprcoder"/"supercoder"); unrelated input falls well
# below. 0.8 catches realistic typos while rejecting noise.
FUZZY_CUTOFF = 0.8


@dataclass(frozen=True)
class SkillCatalog:
    canonical_ids: frozenset[str]
    bare_to_canonical: dict[str, tuple[str, ...]]
    pack_bootstrap: dict[str, str]


@dataclass(frozen=True)
class Resolved:
    canonical_id: str
    note: str | None = None


@dataclass(frozen=True)
class Failed:
    reason: str  # 'empty' | 'not_found' | 'ambiguous' | 'fuzzy_ambiguous'
    suggestions: list[str]


ResolveResult = Resolved | Failed


def _norm(s: str) -> str:
    # Input-side '_'->'-' tolerance only. Catalog ids are kebab-case (pack ids
    # are kebab by convention), so this intentionally does NOT replicate
    # SkillsRegistry's tail-only / bidirectional normalization
    # (registry.py:478-481,491) — parity is unnecessary given kebab catalog ids.
    return s.strip().replace("_", "-")


def _resolve_exact(name: str, catalog: SkillCatalog) -> ResolveResult | None:
    """Tiers 1-3 on an already-normalized name. None => no exact match."""
    if ":" in name:
        if name in catalog.canonical_ids:
            return Resolved(name)
        return None
    if name in catalog.pack_bootstrap:
        target = catalog.pack_bootstrap[name]
        return Resolved(target, note=f"pack '{name}' -> bootstrap '{target}'")
    if name in catalog.bare_to_canonical:
        ids = catalog.bare_to_canonical[name]
        if len(ids) == 1:
            return Resolved(ids[0])
        return Failed("ambiguous", sorted(ids))
    return None


def resolve_skill_arg(raw: str, catalog: SkillCatalog) -> ResolveResult:
    name = _norm(raw or "")
    if not name:
        return Failed("empty", [])

    exact = _resolve_exact(name, catalog)
    if exact is not None:
        return exact

    pool = sorted(
        set(catalog.canonical_ids)
        | set(catalog.bare_to_canonical)
        | set(catalog.pack_bootstrap)
    )
    matches = difflib.get_close_matches(name, pool, n=5, cutoff=FUZZY_CUTOFF)
    if not matches:
        return Failed("not_found", [])

    def _ratio(cand: str) -> float:
        return difflib.SequenceMatcher(None, name, cand).ratio()

    top = _ratio(matches[0])
    # Exact == is safe: top and each _ratio(m) come from the same
    # SequenceMatcher.ratio() = 2*M/T computation, deterministic for equal
    # M,T. Do NOT relax to math.isclose — it would merge genuine near-ties.
    best = [m for m in matches if _ratio(m) == top]
    if len(best) > 1:
        return Failed("fuzzy_ambiguous", matches)

    chosen = best[0]
    res = _resolve_exact(chosen, catalog)
    if isinstance(res, Resolved):
        return Resolved(
            res.canonical_id,
            note=f"'{raw.strip()}' not found; resolved to '{res.canonical_id}' (closest match '{chosen}')",
        )
    if isinstance(res, Failed):
        return res  # chosen bare maps to >1 canonical -> still ambiguous
    return Failed("not_found", matches)  # defensive: unreachable in practice
