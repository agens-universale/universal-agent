from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FsToolsConfig(BaseModel):
    """Caps for FS tools that load whole-file content into memory."""

    model_config = ConfigDict(extra="forbid")

    max_read_bytes: int = Field(
        default=8 * 1024 * 1024,
        description="fs_read_file / fs_search_files: refuse files larger than this.",
    )
    max_mutate_bytes: int = Field(
        default=100 * 1024 * 1024,
        description=(
            "fs_apply_file_edits / fs_search_and_replace: "
            "refuse loading files larger than this. "
            "fs_append_file checks a head sample and appends without a full-file load."
        ),
    )
