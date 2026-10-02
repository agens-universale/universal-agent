from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FsFindInFilesConfig(BaseModel):
    """Settings for ``fs_find_in_files`` — search text inside files (file or dir).

    Mode candidate caps follow the scan-automaton spec (like / or_simple /
    affix_or / complex). Overrides are optional; defaults match the spec.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_file_bytes: int = Field(
        default=16 * 1024 * 1024,
        description=(
            "Unified file-size cap for fs_find_in_files (both directory candidates "
            "and single-file mode). Files larger than this are not opened."
        ),
    )
    max_candidates_like: int = Field(
        default=500_000,
        description="Max walk candidates for like / query_mode=text (directory).",
    )
    max_candidates_or_simple: int = Field(
        default=200_000,
        description="Max walk candidates for or_simple (directory).",
    )
    max_candidates_affix_or: int = Field(
        default=300_000,
        description="Max walk candidates for affix_or (directory).",
    )
    max_candidates_complex: int = Field(
        default=50_000,
        description="Max walk candidates for complex (directory).",
    )
    max_unfiltered_host_reads: int = Field(
        default=1000,
        description=(
            "Sandbox backends only: max candidate files read from the host without a "
            "successful in-sandbox prefilter (it failed, or the pattern has no fixed "
            "substring). Above it the search refuses with guidance to narrow."
        ),
    )
    max_open_files_affix_or: int = Field(
        default=50_000,
        description="Max full-re opens for affix_or (directory content loop).",
    )
    content_workers_threshold: int = Field(
        default=20_000,
        description="N >= this → 2 content workers; below → 1 (directory only).",
    )
    max_exclude_dirs: int = Field(
        default=32,
        description="Hard cap on call-time exclude_dirs length after normalize.",
    )
    top_level_dirs_cap: int = Field(
        default=5,
        description="Max top_level_dirs entries in refuse breakdown (agent response).",
    )
    default_limit: int = Field(
        default=100,
        description="``limit`` used when the caller does not provide one.",
    )
    max_total_matches: int = Field(
        default=300,
        description="Hard ceiling for ``limit_eff = min(requested, max_total_matches)``.",
    )
    exclude_dot_dirs: bool = Field(
        default=True,
        description=(
            "Nested dirs whose name starts with '.' are never descended into. "
            "Hard policy: false does not re-enable nested dot-dirs."
        ),
    )
    exclude_dir_names: list[str] = Field(
        default_factory=list,
        description=(
            "Extra directory base names never descended into when nested "
            "(merged with a non-removable hard floor: venv, __pycache__, "
            "node_modules, .git). Start path itself is always kept. "
            "Clearing this list does not remove the hard floor."
        ),
    )


def parse_find_in_files_config(
    config_dict: Optional[dict[str, Any]] = None,
) -> FsFindInFilesConfig:
    """Read ``fs_tools.find_in_files`` from a raw config dict.

    Invalid nested values fall back to class defaults (same as runtime tools).
    """
    fif_raw: Any = {}
    if isinstance(config_dict, dict):
        fs_raw = config_dict.get("fs_tools", {})
        if isinstance(fs_raw, dict):
            fif_raw = fs_raw.get("find_in_files", {}) or {}
    try:
        return FsFindInFilesConfig.model_validate(
            fif_raw if isinstance(fif_raw, dict) else {}
        )
    except Exception:
        return FsFindInFilesConfig()


class FsFindFilesByNameConfig(BaseModel):
    """Settings for ``fs_find_files_by_name`` — locate files/dirs by name or glob.

    ``default_max_matches`` is used when the caller omits ``max_matches``.
    ``hard_max_matches`` is the absolute ceiling: values above it are clamped.
    ``max_visited`` is a walk safety cap, not a caller argument.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_max_matches: int = Field(
        default=100,
        ge=1,
        description="``max_matches`` used when the caller does not provide one.",
    )
    hard_max_matches: int = Field(
        default=300,
        ge=1,
        description="Hard ceiling: ``max_matches`` is clamped to this value.",
    )
    max_visited: int = Field(
        default=500_000,
        ge=1,
        description="Walk safety cap: stop after this many directory entries.",
    )

    @model_validator(mode="after")
    def _default_not_above_hard(self) -> FsFindFilesByNameConfig:
        if self.default_max_matches > self.hard_max_matches:
            raise ValueError("default_max_matches must be <= hard_max_matches")
        return self


def parse_find_files_by_name_config(
    config_dict: Optional[dict[str, Any]] = None,
) -> FsFindFilesByNameConfig:
    """Read ``fs_tools.find_files_by_name`` from a raw config dict.

    Invalid nested values fall back to class defaults (same as runtime tools).
    """
    ffbn_raw: Any = {}
    if isinstance(config_dict, dict):
        fs_raw = config_dict.get("fs_tools", {})
        if isinstance(fs_raw, dict):
            ffbn_raw = fs_raw.get("find_files_by_name", {}) or {}
    try:
        return FsFindFilesByNameConfig.model_validate(
            ffbn_raw if isinstance(ffbn_raw, dict) else {}
        )
    except Exception:
        return FsFindFilesByNameConfig()


class FsToolsConfig(BaseModel):
    """Caps for FS tools that load whole-file content into memory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_read_bytes: int = Field(
        default=8 * 1024 * 1024,
        description="fs_read_file: refuse files larger than this.",
    )
    max_mutate_bytes: int = Field(
        default=16 * 1024 * 1024,
        description=(
            "fs_apply_file_edits / fs_search_and_replace: "
            "refuse loading files larger than this. "
            "fs_append_file checks a head sample and appends without a full-file load."
        ),
    )
    find_in_files: FsFindInFilesConfig = Field(
        default_factory=FsFindInFilesConfig,
        description="Settings for fs_find_in_files (search text inside files).",
    )
    find_files_by_name: FsFindFilesByNameConfig = Field(
        default_factory=FsFindFilesByNameConfig,
        description="Settings for fs_find_files_by_name (locate files by name/glob).",
    )
