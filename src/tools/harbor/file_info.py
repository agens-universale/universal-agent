"""Harbor variant of fs_file_info: stats via shell through HarborBackend."""
import json
import shlex
from tools.fs.file_info import FileInfo


class HarborFileInfo(FileInfo):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path")
        res = self._ctx.harbor_backend.exec(f"stat {shlex.quote(path)} 2>/dev/null")
        return json.dumps(
            {"tool": "fs_file_info", "ok": res.return_code == 0, "path": path, "info": res.stdout},
            ensure_ascii=False,
        )
