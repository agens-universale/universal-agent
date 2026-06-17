"""Harbor variant of fs_write_file: writes through the injected HarborBackend."""
import json
from tools.fs.write_to_file_tool import WriteToFile


class HarborWriteToFile(WriteToFile):
    def __call__(self, **arguments) -> str:
        path = arguments.get("path")
        content = arguments.get("content", "")
        self._ctx.harbor_backend.write(path, content)
        return json.dumps(
            {"tool": "fs_write_file", "ok": True, "path": path, "bytes": len(content.encode("utf-8"))},
            ensure_ascii=False,
        )
