"""Harbor variant of exec_python: runs python in the container via HarborBackend."""
import json
from tools.exec.run_python_code import RunPythonCode

_DELIM = "GC_HARBOR_PY_EOF"


class HarborRunPythonCode(RunPythonCode):
    def __call__(self, **arguments) -> str:
        code = arguments.get("code", "")
        cwd = arguments.get("cwd")
        timeout = arguments.get("timeout_seconds")
        # Feed code via a quoted heredoc so the body is taken literally.
        cmd = f"python3 - <<'{_DELIM}'\n{code}\n{_DELIM}"
        res = self._ctx.harbor_backend.exec(cmd, cwd=cwd, timeout=timeout)
        return json.dumps(
            {"tool": "exec_python", "ok": res.return_code == 0, "return_code": res.return_code,
             "stdout": res.stdout, "stderr": res.stderr},
            ensure_ascii=False,
        )
