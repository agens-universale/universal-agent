"""Harbor variant of exec_command: runs through the injected HarborBackend."""
import json
from tools.exec.execute_command import ExecuteCommand


class HarborExecuteCommand(ExecuteCommand):
    def __call__(self, **arguments) -> str:
        command = arguments.get("command")
        cwd = arguments.get("cwd")
        res = self._ctx.harbor_backend.exec(command, cwd=cwd)
        return json.dumps(
            {
                "tool": "exec_command",
                "ok": res.return_code == 0,
                "return_code": res.return_code,
                "stdout": res.stdout,
                "stderr": res.stderr,
            },
            ensure_ascii=False,
        )
