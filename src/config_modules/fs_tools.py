from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FsFindInFilesConfig(BaseModel):
    """Settings for ``fs_find_in_files`` — search text inside files (file or dir).

    Mode candidate caps follow the scan-automaton spec (like / or_simple /
    affix_or / complex). Overrides are optional; defaults match the spec.
    """

    model_config = ConfigDict(extra="forbid")

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
        default=20,
        description="Max top_level_dirs entries in refuse/partial breakdown.",
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
        description="Do not descend into nested directories whose name starts with a dot.",
    )
    exclude_dir_names: list[str] = Field(
        default_factory=lambda: ["venv", "__pycache__", "node_modules"],
        description="Directory base names never descended into (nested only; start is kept).",
    )


class FsToolsConfig(BaseModel):
    """Caps for FS tools that load whole-file content into memory."""

    model_config = ConfigDict(extra="forbid")

    max_read_bytes: int = Field(
        default=8 * 1024 * 1024,
        description="fs_read_file / fs_search_files: refuse files larger than this.",
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
