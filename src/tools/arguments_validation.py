import json
from typing import Any, Dict

from tools.connect_mcp.validation import _validate_tool_arguments


class WrongFunctionArgumentsException(Exception):
    def __init__(
        self,
        message: str,
        received_args: str,
        expected_schema: dict | None = None,
        tool_name: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.received_args = received_args
        self.expected_schema = expected_schema
        self.tool_name = tool_name

    def to_llm_feedback(self) -> str:
        """
        Формирует четкое и структурированное сообщение об ошибке для LLM.
        """
        tool_label = f"`{self.tool_name}`" if self.tool_name else "tool"
        feedback = f"❌ Wrong arguments for {tool_label}: {self.message}\n\n"

        args_display = self.received_args if self.received_args else "(empty)"
        feedback += f"You sent: `{args_display}`\n\n"

        # Special hint: model is trying to write a filesystem file via memory_files
        if self.tool_name == "memory_files" and self.received_args:
            try:
                import json as _json
                _sent = _json.loads(self.received_args) if isinstance(self.received_args, str) else self.received_args
                if isinstance(_sent, dict) and "path" in _sent:
                    feedback += (
                        "💡 Hint: `memory_files` is for INTERNAL AGENT MEMORY only — it does NOT write files to disk.\n"
                        "To create files on the filesystem (e.g. fibonacci.py, test_fibonacci.py, data.json), "
                        "use the `write_to_file` tool with `path` (absolute path) and `content` arguments.\n\n"
                    )
            except Exception:
                pass

        if self.expected_schema:
            params = self.expected_schema.get("parameters", {}) if isinstance(self.expected_schema, dict) else {}
            properties = params.get("properties", {}) or {}
            required_fields = params.get("required", []) or []
            required_entries = []
            optional_entries = []
            for field, spec in properties.items():
                ftype = spec.get("type", "any")
                fdesc = spec.get("description", "")
                fdesc_short = fdesc.split(".")[0] if fdesc else ""
                entry = f'  "{field}": <{ftype}>'
                if fdesc_short:
                    entry += f"  // {fdesc_short}"
                if field in required_fields:
                    required_entries.append(entry)
                else:
                    optional_entries.append(entry)
            if required_entries or optional_entries:
                all_field_entries = required_entries + optional_entries
                all_field_lines = []
                if required_entries:
                    all_field_lines.append("  // REQUIRED")
                    all_field_lines.extend(required_entries)
                if optional_entries:
                    all_field_lines.append("  // OPTIONAL")
                    all_field_lines.extend(optional_entries)
                # Add comma after each field entry (non-comment lines), except the last one
                body_lines = []
                last_field_idx = max(
                    i for i, line in enumerate(all_field_lines) if not line.strip().startswith("//")
                )
                for i, line in enumerate(all_field_lines):
                    is_comment = line.strip().startswith("//")
                    is_last_field = (not is_comment and i == last_field_idx)
                    if is_comment or is_last_field:
                        body_lines.append(line)
                    else:
                        body_lines.append(line + ",")
                body = "\n".join(body_lines)
                feedback += f"Correct call format:\n```json\n{{\n{body}\n}}\n```\n\n"

        feedback += "Fix the arguments and retry the tool call."
        return feedback


def parse_and_validate_tool_arguments(
    *,
    tool_name: str,
    arguments: str | Dict[str, Any] | None,
    tool_spec: Dict[str, Any],
    allow_empty_for_empty_schema: bool,
) -> Dict[str, Any]:
    params_schema = tool_spec.get("parameters", {}) if isinstance(tool_spec, dict) else {}
    params_schema = params_schema if isinstance(params_schema, dict) else {}
    properties = params_schema.get("properties", {}) or {}
    required = params_schema.get("required", []) or []
    is_empty_schema = not properties and not required

    if isinstance(arguments, dict):
        parsed_args = arguments
    else:
        arg_str = "" if arguments is None else str(arguments).strip()
        if arg_str == "":
            if allow_empty_for_empty_schema and is_empty_schema:
                parsed_args = {}
            else:
                raise WrongFunctionArgumentsException(
                    message="аргументы инструмента пустые, ожидается валидный JSON-объект",
                    received_args=str(arguments),
                    expected_schema=tool_spec,
                    tool_name=tool_name,
                )
        else:
            try:
                parsed_args = json.loads(arg_str)
            except Exception as e:  # noqa: BLE001
                raise WrongFunctionArgumentsException(
                    message=f"некорректный JSON: {e}",
                    received_args=str(arguments),
                    expected_schema=tool_spec,
                    tool_name=tool_name,
                )

    if not isinstance(parsed_args, dict):
        raise WrongFunctionArgumentsException(
            message="аргументы инструмента должны быть JSON-объектом",
            received_args=str(arguments),
            expected_schema=tool_spec,
            tool_name=tool_name,
        )

    try:
        _validate_tool_arguments(tool_name, params_schema, parsed_args)
    except Exception as e:  # noqa: BLE001
        raise WrongFunctionArgumentsException(
            message=str(e),
            received_args=json.dumps(parsed_args, ensure_ascii=False),
            expected_schema=tool_spec,
            tool_name=tool_name,
        )

    return parsed_args


__all__ = [
    "WrongFunctionArgumentsException",
    "parse_and_validate_tool_arguments",
]
