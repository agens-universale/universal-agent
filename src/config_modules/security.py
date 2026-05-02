from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_COMMAND_MODES = {"allow", "ask", "deny"}


def validate_command_moderation_mode(raw_mode: Any) -> str:
    mode = str(raw_mode if raw_mode is not None else "deny").strip().lower()
    if mode not in ALLOWED_COMMAND_MODES:
        raise ValueError("command_moderation.mode должен быть 'allow', 'ask' или 'deny'")
    return mode


class RootBoxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    extra_write_paths: list[str] = Field(default_factory=list)


class UrlPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_schemes: list[str] = Field(default_factory=lambda: ["http", "https"])
    url_blacklist: list[str] = Field(default_factory=list)


class CommandModerationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = "deny"
    allowed_command_examples: list[str] = Field(default_factory=list)
    root_box: RootBoxConfig = Field(default_factory=RootBoxConfig)
    url_policy: UrlPolicyConfig = Field(default_factory=UrlPolicyConfig)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        try:
            return validate_command_moderation_mode(value)
        except ValueError as exc:
            raise ValueError(
                "security.command_moderation.mode должен быть 'allow', 'ask' или 'deny'"
            ) from exc


class SecurityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_moderation: CommandModerationConfig = Field(
        default_factory=CommandModerationConfig
    )


def apply_cli_command_moderation_overrides(
    config_dict: Dict[str, Any],
    *,
    root_box_enabled: Optional[bool] = None,
    command_moderation_mode: Optional[str] = None,
) -> None:
    command_moderation = config_dict.setdefault("command_moderation", {})
    root_box = command_moderation.setdefault("root_box", {})
    if root_box_enabled is not None:
        root_box["enabled"] = bool(root_box_enabled)
    elif "enabled" not in root_box:
        root_box["enabled"] = True

    if command_moderation_mode is not None:
        command_moderation["mode"] = validate_command_moderation_mode(
            command_moderation_mode
        )
    elif "mode" not in command_moderation:
        command_moderation["mode"] = "deny"
