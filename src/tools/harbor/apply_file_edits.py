"""Harbor variant of fs_apply_file_edits: applies search/replace edits via HarborBackend."""
import json
from tools.fs.apply_file_edits import ApplyFileEdits


class HarborApplyFileEdits(ApplyFileEdits):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path")
        edits = arguments.get("edits") or []
        backend = self._ctx.harbor_backend
        total = 0
        for edit in edits:
            search = edit.get("search", "")
            replace = edit.get("replace", "")
            replace_all = str(edit.get("occurrence") or "first") == "all"
            total += backend.edit(path, search, replace, replace_all=replace_all)
        return json.dumps(
            {"tool": "fs_apply_file_edits", "ok": True, "path": path,
             "edits_applied": len(edits), "replacements": total},
            ensure_ascii=False,
        )
