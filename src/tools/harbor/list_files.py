"""Harbor variant of fs_list_files: lists via shell through HarborBackend."""
import json
import shlex
from tools.fs.list_files import ListFiles


class HarborListFiles(ListFiles):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path") or "."
        glob = arguments.get("glob")
        backend = self._ctx.harbor_backend
        if glob:
            cmd = f"cd {shlex.quote(path)} && ls -1d {glob} 2>/dev/null"
        else:
            cmd = f"ls -la {shlex.quote(path)}"
        res = backend.exec(cmd)
        return json.dumps(
            {"tool": "fs_list_files", "ok": res.return_code == 0, "path": path, "listing": res.stdout},
            ensure_ascii=False,
        )
