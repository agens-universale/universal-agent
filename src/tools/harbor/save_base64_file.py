"""Harbor variant of fs_save_base64_file: decodes into the container via shell."""
import json
import shlex
from tools.fs.save_base64_file import SaveBase64File


class HarborSaveBase64File(SaveBase64File):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path")
        data = arguments.get("data", "")
        backend = self._ctx.harbor_backend
        backend.exec(f"mkdir -p $(dirname {shlex.quote(path)})")
        res = backend.exec(f"printf '%s' {shlex.quote(data)} | base64 -d > {shlex.quote(path)}")
        return json.dumps(
            {"tool": "fs_save_base64_file", "ok": res.return_code == 0, "path": path},
            ensure_ascii=False,
        )
