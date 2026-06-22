"""Harbor variant of run_python_code — runs code in the container, parity envelope.

Reuses the base RunPythonCode output shaping (default timeout, max_output_chars
truncation, _result envelope, validation) so the model sees the advertised behavior;
only the physical execution routes through HarborBackend (the task container).
"""
import shlex

from tools.exec.run_python_code import RunPythonCode

# Base RunPythonCode uses literal defaults (no self.timeout/self.cut_n attrs).
_DEFAULT_TIMEOUT = 10
_DEFAULT_MAX_OUTPUT_CHARS = 4000
_DELIM = "GC_HARBOR_PY_EOF"


class HarborRunPythonCode(RunPythonCode):
    def __call__(self, **arguments) -> str:
        backend = self._ctx.harbor_backend
        code = arguments.get("code")
        cwd = arguments.get("cwd")
        timeout_seconds = arguments.get("timeout_seconds")
        max_output_chars = arguments.get("max_output_chars")
        env_vars = arguments.get("env_vars")

        def _err(error):
            return self._result(
                ok=False, return_code=None, timed_out=False, truncated=False,
                stdout="", stderr="", error=error,
            )

        if not isinstance(code, str):
            return _err({"type": "invalid_argument", "field": "code"})
        if not code.strip():
            return _err({"type": "invalid_argument", "field": "code", "message": "Code is empty"})

        if timeout_seconds is None:
            timeout_seconds = _DEFAULT_TIMEOUT
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            return _err({"type": "invalid_argument", "field": "timeout_seconds"})

        if max_output_chars is None:
            max_output_chars = _DEFAULT_MAX_OUTPUT_CHARS
        if not isinstance(max_output_chars, int) or max_output_chars <= 0:
            return _err({"type": "invalid_argument", "field": "max_output_chars"})

        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            return _err({"type": "invalid_argument", "field": "cwd"})
        if cwd is not None and not backend.is_dir(cwd):
            # ponytail: base populates allowed_write_paths from the HOST security context;
            # in the container there is no such context, so the hint list is omitted.
            return _err({
                "type": "cwd_invalid",
                "message": f"Working directory is not an existing directory: {cwd}",
                "cwd": cwd,
                "allowed_write_paths": [],
                "hint": (
                    "Pass cwd pointing to an existing directory. If allowed_write_paths is present above, "
                    "use one of those paths — they are the writable locations allowed for this agent run."
                ),
            })

        # env_vars validation (mirrors base) + forwarding via inline env prefix.
        env_pairs = []
        if env_vars is not None:
            if not isinstance(env_vars, list):
                return _err({
                    "type": "invalid_argument", "field": "env_vars",
                    "message": "env_vars must be a list of key/value pairs: [{'name': 'KEY', 'value': 'VALUE'}]",
                })
            for idx, item in enumerate(env_vars):
                if not isinstance(item, dict):
                    return _err({
                        "type": "invalid_argument", "field": f"env_vars[{idx}]",
                        "message": "Each env_vars item must be an object: {'name': 'KEY', 'value': 'VALUE'}",
                    })
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    return _err({"type": "invalid_argument", "field": f"env_vars[{idx}].name"})
                value = item.get("value")
                if not isinstance(value, str):
                    return _err({"type": "invalid_argument", "field": f"env_vars[{idx}].value"})
                env_pairs.append((name.strip(), value))

        # Feed code via a quoted heredoc so the body is taken literally.
        command = f"python3 - <<'{_DELIM}'\n{code}\n{_DELIM}"
        if env_pairs:
            prefix = "env " + " ".join(f"{n}={shlex.quote(v)}" for n, v in env_pairs)
            command = f"{prefix} {command}"

        res = backend.exec(command, cwd=cwd, timeout=timeout_seconds)
        stdout, st = self._truncate_text((res.stdout or "").rstrip(), max_output_chars)
        stderr, se = self._truncate_text((res.stderr or "").rstrip(), max_output_chars)
        ok = res.return_code == 0
        error = None if ok else {"type": "non_zero_exit", "message": f"Process exited with status {res.return_code}"}
        # ponytail: timed_out stays False — HarborBackend.exec doesn't surface a container
        # timeout signal yet (it raises/returns without distinguishing timeout from error).
        return self._result(
            ok=ok, return_code=res.return_code, timed_out=False, truncated=st or se,
            stdout=stdout, stderr=stderr, error=error,
        )
