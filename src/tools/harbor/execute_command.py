"""Harbor variant of exec_command — full envelope/truncation/timeout parity with base.

Runs the command in the task container via HarborBackend instead of a host subprocess;
all output shaping (default timeout, max_output_chars truncation, _result envelope)
reuses the base ExecuteCommand behavior so the model sees the advertised fields.
"""
import shlex

from tools.exec.execute_command import ExecuteCommand


class HarborExecuteCommand(ExecuteCommand):
    timeout = 300

    def __call__(self, **arguments) -> str:
        backend = self._ctx.harbor_backend
        command = arguments.get("command")
        cwd = arguments.get("cwd")
        timeout_seconds = arguments.get("timeout_seconds")
        max_output_chars = arguments.get("max_output_chars")
        env_vars = arguments.get("env_vars")

        if not isinstance(command, str):
            return self._result(
                ok=False, command=str(command), cwd=cwd or "", exit_code=None,
                timed_out=False, truncated=False, stdout="", stderr="",
                error={"type": "invalid_argument", "field": "command"},
            )
        if not command.strip():
            return self._result(
                ok=False, command=command, cwd=cwd or "", exit_code=None,
                timed_out=False, truncated=False, stdout="", stderr="",
                error={"type": "invalid_argument", "field": "command", "message": "Empty command"},
            )

        if timeout_seconds is None:
            timeout_seconds = self.timeout
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            return self._result(
                ok=False, command=command, cwd=cwd or "", exit_code=None,
                timed_out=False, truncated=False, stdout="", stderr="",
                error={"type": "invalid_argument", "field": "timeout_seconds"},
            )

        if max_output_chars is None:
            max_output_chars = self.cut_n
        if not isinstance(max_output_chars, int) or max_output_chars <= 0:
            return self._result(
                ok=False, command=command, cwd=cwd or "", exit_code=None,
                timed_out=False, truncated=False, stdout="", stderr="",
                error={"type": "invalid_argument", "field": "max_output_chars"},
            )

        if cwd is not None and not backend.is_dir(cwd):
            return self._result(
                ok=False, command=command, cwd=cwd, exit_code=None,
                timed_out=False, truncated=False, stdout="", stderr="",
                error={"type": "invalid_argument", "field": "cwd", "message": "Not a directory"},
            )

        # env_vars validation and forwarding
        if env_vars is not None:
            if not isinstance(env_vars, list):
                return self._result(
                    ok=False, command=command, cwd=cwd or "", exit_code=None,
                    timed_out=False, truncated=False, stdout="", stderr="",
                    error={
                        "type": "invalid_argument",
                        "field": "env_vars",
                        "message": "env_vars must be a list of key/value pairs: [{'name': 'KEY', 'value': 'VALUE'}]",
                    },
                )
            env_pairs: list[tuple[str, str]] = []
            for idx, item in enumerate(env_vars):
                if not isinstance(item, dict):
                    return self._result(
                        ok=False, command=command, cwd=cwd or "", exit_code=None,
                        timed_out=False, truncated=False, stdout="", stderr="",
                        error={
                            "type": "invalid_argument",
                            "field": f"env_vars[{idx}]",
                            "message": "Each env_vars item must be an object: {'name': 'KEY', 'value': 'VALUE'}",
                        },
                    )
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    return self._result(
                        ok=False, command=command, cwd=cwd or "", exit_code=None,
                        timed_out=False, truncated=False, stdout="", stderr="",
                        error={"type": "invalid_argument", "field": f"env_vars[{idx}].name"},
                    )
                value = item.get("value")
                if not isinstance(value, str):
                    return self._result(
                        ok=False, command=command, cwd=cwd or "", exit_code=None,
                        timed_out=False, truncated=False, stdout="", stderr="",
                        error={
                            "type": "invalid_argument",
                            "field": f"env_vars[{idx}].value",
                            "message": "env_vars value must be a string",
                        },
                    )
                env_pairs.append((name.strip(), value))

            if env_pairs:
                # Build an inline env prefix: env KEY1=val1 KEY2=val2 <command>
                # shlex.quote on values prevents injection from special chars
                prefix = "env " + " ".join(
                    f"{n}={shlex.quote(v)}" for n, v in env_pairs
                )
                command = f"{prefix} {command}"

        res = backend.exec(command, cwd=cwd, timeout=timeout_seconds)
        stdout, st = self._truncate_text((res.stdout or "").rstrip(), max_output_chars)
        stderr, se = self._truncate_text((res.stderr or "").rstrip(), max_output_chars)
        ok = res.return_code == 0
        error = None if ok else {"type": "non_zero_exit", "message": f"Command exited with status {res.return_code}"}
        # ponytail: timed_out is always False because HarborBackend.exec does not currently
        # surface a timeout signal from the container runtime — the backend raises or returns
        # normally without distinguishing a timeout from other errors.
        return self._result(
            ok=ok, command=command, cwd=cwd or "", exit_code=res.return_code,
            timed_out=False, truncated=st or se, stdout=stdout, stderr=stderr, error=error,
        )
