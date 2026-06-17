"""Harbor variant of fs_search_files: greps via shell through HarborBackend."""
import json
import shlex
from tools.fs.search_files import SearchFiles


class HarborSearchFiles(SearchFiles):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path") or "."
        regex = arguments.get("regex", "")
        file_pattern = arguments.get("file_pattern")
        include = f"--include={shlex.quote(file_pattern)} " if file_pattern else ""
        cmd = f"grep -rnE {include}-e {shlex.quote(regex)} {shlex.quote(path)} 2>/dev/null"
        res = self._ctx.harbor_backend.exec(cmd)
        return json.dumps(
            {"tool": "fs_search_files", "ok": res.return_code in (0, 1), "path": path,
             "matches": res.stdout},
            ensure_ascii=False,
        )
