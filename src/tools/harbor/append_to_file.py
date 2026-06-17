"""Harbor variant of fs_append_file: appends via shell through HarborBackend."""
import json
import shlex
from tools.fs.append_to_file import AppendToFile


class HarborAppendToFile(AppendToFile):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path")
        content = arguments.get("content", "")
        separator = arguments.get("separator") or ""
        payload = separator + content
        backend = self._ctx.harbor_backend
        backend.exec(f"mkdir -p $(dirname {shlex.quote(path)})")
        res = backend.exec(f"printf '%s' {shlex.quote(payload)} >> {shlex.quote(path)}")
        return json.dumps(
            {"tool": "fs_append_file", "ok": res.return_code == 0, "path": path,
             "appended_bytes": len(payload.encode("utf-8"))},
            ensure_ascii=False,
        )
