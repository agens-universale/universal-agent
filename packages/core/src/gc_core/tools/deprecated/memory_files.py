import json
import re
from typing import Any, Dict, Optional

from gc_core.tools.simple_tool_base import AbstractSimpleTool

# Legacy limits for deprecated tool only (not part of runtime AppConfig).
_DEFAULT_MAX_FILE_CHARS = 20_000
_DEFAULT_TOTAL_BUDGET_CHARS = 50_000
_DEFAULT_MAX_FILES = 5

_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROTECTED_FILES = frozenset({"journal"})


def _validate_file_name(name: str) -> Optional[str]:
    if not isinstance(name, str):
        return "file_name must be a string"
    if not _VALID_NAME_RE.match(name):
        return (
            "file_name must be 1-64 chars: lowercase letters, digits, underscores; "
            "must start with a letter."
        )
    return None


def _files_summary(files: Dict[str, str]) -> Dict[str, str]:
    return {k: f"Length - {len(v)} chars" for k, v in files.items()}


def _total_chars(files: Dict[str, str]) -> int:
    return sum(len(v) for v in files.values())


class MemoryFiles(AbstractSimpleTool):
    """
    Multiple named memory registers persisted per-thread.

    Stored in state.shared.memory_files: Dict[str, str].
    Default register: ``journal``.
    """

    tool_name = "memory_files"
    EMPTY_LIST_PROMPT = (
        "No memory files yet. Use action append or write to persist meaningful "
        "decisions, specs, paths, or conclusions for this thread (default file: journal)."
    )

    def __init__(
        self,
        ctx: Any = None,
        state: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state
        cfg = getattr(ctx, "config_dict", None) or {}
        self.MAX_FILE_CHARS = self._cfg_int(cfg, "memory_files_max_file_chars", _DEFAULT_MAX_FILE_CHARS)
        self.TOTAL_BUDGET_CHARS = self._cfg_int(cfg, "memory_files_total_budget_chars", _DEFAULT_TOTAL_BUDGET_CHARS)
        self.MAX_FILES = self._cfg_int(cfg, "memory_files_max_files", _DEFAULT_MAX_FILES)

    @staticmethod
    def _cfg_int(cfg: Dict[str, Any], key: str, default: int) -> int:
        val = cfg.get(key)
        if isinstance(val, int) and val > 0:
            return val
        return default

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "memory_files",
            "description": (
                "Persistent named memory files for the current thread. "
                "Each file is a markdown register that survives summarization. "
                "IMPORTANT: This tool is for INTERNAL AGENT MEMORY ONLY - it does NOT write files to the filesystem. "
                "To create or write files on disk (e.g. .py, .json, .txt files), use the write_to_file tool instead. "
                "Default file is 'journal'. "
                "Use action='list' to see all file names and sizes. "
                "Use action='read' to load a file (default: journal). "
                "Use action='append' to add a delta to a file. "
                "Use action='write' to fully rewrite a file. "
                "Use action='delete' to remove a file (journal cannot be deleted). "
                "Keep `content` concise: store facts, decisions, and evidence only. "
                "Do NOT put large text blobs in a single call-one huge `content` often breaks "
                "tool-call JSON due to max_tokens limits. Split across several append calls if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "append", "write", "delete"],
                        "description": (
                            "list = show all files with sizes; "
                            "read = return file content; "
                            "append = add delta to file end; "
                            "write = replace file entirely; "
                            "delete = remove file (journal protected)."
                        ),
                    },
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Target file name (lowercase, a-z0-9_, starts with letter, max 64 chars). "
                            "Defaults to 'journal' when omitted."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Markdown text for append/write; keep each call moderate-sized. "
                            "For append: ONLY the new delta (do not resend existing content). "
                            "For write: the FULL updated content, but keep it concise-if the "
                            "rewrite is large, split into write + subsequent appends."
                        ),
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        cfg = config_dict or {}
        max_file_chars = cls._cfg_int(cfg, "memory_files_max_file_chars", _DEFAULT_MAX_FILE_CHARS)
        total_budget_chars = cls._cfg_int(
            cfg, "memory_files_total_budget_chars", _DEFAULT_TOTAL_BUDGET_CHARS
        )
        max_files = cls._cfg_int(cfg, "memory_files_max_files", _DEFAULT_MAX_FILES)
        return f'''Tool name: memory_files
Tool description: Persistent named memory files for the current thread (INTERNAL AGENT MEMORY ONLY).
WARNING: This tool does NOT write files to the disk/filesystem. To write .py, .json, .txt or any other files on disk, use write_to_file tool.
Each file is a separate markdown register that survives summarization.
Default file: "journal".

Actions:
- action="list": returns all files with their sizes (chars).
- action="read", file_name="...": returns the content of the file (default: journal).
- action="append", file_name="...", content="...": appends delta to the file end.
- action="write", file_name="...", content="...": replaces the file content entirely.
- action="delete", file_name="...": removes the file. "journal" cannot be deleted.

Limits:
- Max {max_file_chars} chars per file (memory_files_max_file_chars). If exceeded, read the file and rewrite it more compactly.
- Total budget across all files: {total_budget_chars} chars (memory_files_total_budget_chars).
- Max {max_files} files per thread (memory_files_max_files).

File naming: lowercase letters, digits, underscores; must start with a letter; max 64 chars.
Examples: journal, architecture, api_contracts, bugs, decisions.

Payload size:
- Keep each `content` moderate-sized. One huge payload can break tool-call JSON due to max_tokens limits.
- For append: send ONLY the new delta, do not resend existing content.
- For write (full rewrite): if the rewrite is large, use write with a compact opening, then append the rest in follow-up calls.

Workflow:
- Prefer dialog_summary `<memory_files>` when present; use action="read" for registers you need in full.
- Call action="list" only when register names are unknown.
- When you fix a decision, fact, or structure - append or write to an appropriate file.
- Keep files concise: facts, evidence, decisions. NOT long reasoning or chain-of-thought.
- "journal" is the chronological log of important events and decisions.

Input schema: {{"type":"object","properties":{{"action":{{"type":"string","enum":["list","read","append","write","delete"]}}, "file_name":{{"type":"string"}}, "content":{{"type":"string"}}}},"required":["action"],"additionalProperties":false}}

Return format (JSON string):
{{
  "tool": "memory_files",
  "ok": true|false,
  "action": "list|read|append|write|delete",
  "error": null|{{"type":"...","message":"...","files":{{...}}}},
  "files": {{"file_name": "Length - <N> chars", ...}},
  "total_chars": number,
  "hint": "..." (only for list when files is empty - nudge to start using memory),
  "content": "..." (only for read)
}}
'''

    def _get_files(self) -> Dict[str, str]:
        shared = getattr(self._state, "shared", None)
        if shared is not None:
            raw = getattr(shared, "memory_files", None)
            if isinstance(raw, dict):
                return raw
        return {}

    def _set_files(self, files: Dict[str, str]) -> None:
        self._state.shared.memory_files = files

    def _result(
        self,
        *,
        ok: bool,
        action: str,
        files: Optional[Dict[str, str]] = None,
        total_chars: Optional[int] = None,
        content: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
        hint: Optional[str] = None,
    ) -> str:
        if files is None:
            current_files = self._get_files() if self._state is not None else {}
            normalized_files = _files_summary(current_files)
        else:
            normalized_files = files
        normalized_total_chars = (
            _total_chars(self._get_files()) if total_chars is None and self._state is not None else (total_chars or 0)
        )
        payload: Dict[str, Any] = {
            "tool": "memory_files",
            "ok": ok,
            "action": action,
            "error": error,
            "files": normalized_files,
        }
        payload["total_chars"] = normalized_total_chars
        if content is not None:
            payload["content"] = content
        if hint is not None:
            payload["hint"] = hint
        return json.dumps(payload, ensure_ascii=False)

    def _error(self, action: str, error_type: str, message: str, **extra: Any) -> str:
        files = self._get_files()
        err: Dict[str, Any] = {"type": error_type, "message": message}
        err.update(extra)
        return self._result(
            ok=False,
            action=action,
            files=_files_summary(files),
            total_chars=_total_chars(files),
            error=err,
        )

    def __call__(
        self,
        action: str,
        file_name: str = "journal",
        content: str = "",
        **kwargs: Any,
    ) -> str:
        if self._state is None:
            return self._result(
                ok=False,
                action=action,
                files={},
                total_chars=0,
                error={"type": "no_state", "message": "state is missing."},
            )

        if not isinstance(file_name, str) or not file_name.strip():
            file_name = "journal"
        file_name = file_name.strip().lower()

        actions = {
            "list": self._action_list,
            "read": self._action_read,
            "append": self._action_append,
            "write": self._action_write,
            "delete": self._action_delete,
        }
        handler = actions.get(action)
        if handler is None:
            return self._error(action, "invalid_action", f"Unknown action: {action}")

        return handler(file_name=file_name, content=content)

    def _action_list(self, **_: Any) -> str:
        files = self._get_files()
        summary = _files_summary(files)
        total = _total_chars(files)
        hint = self.EMPTY_LIST_PROMPT if not summary else None
        return self._result(
            ok=True,
            action="list",
            files=summary,
            total_chars=total,
            hint=hint,
        )

    def _action_read(self, file_name: str, **_: Any) -> str:
        name_err = _validate_file_name(file_name)
        if name_err:
            return self._error("read", "invalid_name", name_err)
        files = self._get_files()
        text = files.get(file_name)
        if text is None:
            if file_name == "journal":
                return self._result(
                    ok=True,
                    action="read",
                    files=_files_summary(files),
                    total_chars=_total_chars(files),
                    content="(empty - use append to start logging decisions and facts)",
                )
            return self._error("read", "not_found", f"File '{file_name}' does not exist.")
        return self._result(
            ok=True,
            action="read",
            files=_files_summary(files),
            total_chars=_total_chars(files),
            content=text,
        )

    def _action_append(self, file_name: str, content: str, **_: Any) -> str:
        name_err = _validate_file_name(file_name)
        if name_err:
            return self._error("append", "invalid_name", name_err)
        if not isinstance(content, str) or not content.strip():
            return self._error("append", "empty_content", "content must be non-empty for append.")

        files = self._get_files()
        existing = files.get(file_name, "").strip()
        delta = content.strip()
        new_text = f"{existing}\n\n{delta}" if existing else delta

        err = self._check_limits(files, file_name, new_text)
        if err:
            return err

        files[file_name] = new_text
        self._set_files(files)
        return self._result(
            ok=True,
            action="append",
            files=_files_summary(files),
            total_chars=_total_chars(files),
        )

    def _action_write(self, file_name: str, content: str, **_: Any) -> str:
        name_err = _validate_file_name(file_name)
        if name_err:
            return self._error("write", "invalid_name", name_err)
        if not isinstance(content, str) or not content.strip():
            return self._error("write", "empty_content", "content must be non-empty for write.")

        files = self._get_files()
        new_text = content.strip()

        is_new_file = file_name not in files
        if is_new_file and len(files) >= self.MAX_FILES:
            return self._error(
                "write",
                "max_files_exceeded",
                f"Cannot create file: limit of {self.MAX_FILES} files reached.",
                files=_files_summary(files),
            )

        err = self._check_limits(files, file_name, new_text)
        if err:
            return err

        files[file_name] = new_text
        self._set_files(files)
        return self._result(
            ok=True,
            action="write",
            files=_files_summary(files),
            total_chars=_total_chars(files),
        )

    def _action_delete(self, file_name: str, **_: Any) -> str:
        name_err = _validate_file_name(file_name)
        if name_err:
            return self._error("delete", "invalid_name", name_err)
        if file_name in _PROTECTED_FILES:
            return self._error("delete", "protected", f"File '{file_name}' cannot be deleted.")
        files = self._get_files()
        if file_name not in files:
            return self._error("delete", "not_found", f"File '{file_name}' does not exist.")
        del files[file_name]
        self._set_files(files)
        return self._result(
            ok=True,
            action="delete",
            files=_files_summary(files),
            total_chars=_total_chars(files),
        )

    def _check_limits(
        self,
        files: Dict[str, str],
        target_name: str,
        new_text: str,
    ) -> Optional[str]:
        if len(new_text) > self.MAX_FILE_CHARS:
            return self._error(
                "write",
                "file_limit_exceeded",
                f"File '{target_name}' would be {len(new_text)} chars, "
                f"but the per-file limit is {self.MAX_FILE_CHARS} chars. "
                "Read the file first, then rewrite it more compactly.",
            )
        projected_total = (
            sum(len(v) for k, v in files.items() if k != target_name) + len(new_text)
        )
        if projected_total > self.TOTAL_BUDGET_CHARS:
            return self._error(
                "write",
                "total_budget_exceeded",
                f"Writing to '{target_name}' would bring total memory to "
                f"{projected_total} chars (budget: {self.TOTAL_BUDGET_CHARS} chars). "
                "Free space first: read the largest files and rewrite them more compactly, "
                "or delete files you no longer need.",
                files=_files_summary(files),
            )
        return None


__all__ = ["MemoryFiles"]
