"""Harbor variant of fs_search_and_replace: edits via HarborBackend."""
import json
from tools.fs.search_and_replace import SearchAndReplace


class HarborSearchAndReplace(SearchAndReplace):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path")
        search = arguments.get("search", "")
        replace = arguments.get("replace", "")
        replace_all = str(arguments.get("occurrence") or "first") == "all"
        count = self._ctx.harbor_backend.edit(path, search, replace, replace_all=replace_all)
        return json.dumps(
            {"tool": "fs_search_and_replace", "ok": count > 0, "path": path, "replacements": count},
            ensure_ascii=False,
        )
