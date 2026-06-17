"""Harbor variant of fs_read_file: reads through the injected HarborBackend."""
import json
from tools.fs.read_file import ReadFile


class HarborReadFile(ReadFile):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path")
        backend = self._ctx.harbor_backend
        try:
            content = backend.read(path)
        except FileNotFoundError:
            return json.dumps(
                {"tool": "fs_read_file", "ok": False, "error": f"File not found: {path}"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"tool": "fs_read_file", "ok": True, "path": path, "content": content},
            ensure_ascii=False,
        )
