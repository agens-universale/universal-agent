# Harbor adapters

Adapters that run gc-cli under the [Harbor](https://www.harborframework.com) eval harness.

## `gc_cli_external.py` — External agent

Runs gc-cli as a Harbor **External** agent: the agent loop runs in-process on the host,
and gc-cli's filesystem/shell tools operate inside the task container through Harbor's
`BaseEnvironment` (the `use_harbor_tools` pack + `HarborBackend`). Nothing is installed
into the task container.

### Setup (once)

gc-cli must be importable in the harbor runner environment. Install it there:

```bash
uv pip install --python "$(dirname "$(readlink -f "$(command -v harbor)")")/python" -e .
```

(or any env where the `harbor` CLI runs).

### Run

Run from **this `harbor/` directory** so `adapters.gc_cli_external` resolves from cwd:

```bash
cd harbor
export GC_CLI_BACKEND_URL=http://127.0.0.1:8014/v1/chat/completions   # host-side backend
harbor run \
  --path <task-dir> \
  --agent-import-path adapters.gc_cli_external:GcCliExternal \
  --model openai/MiniMaxAI/MiniMax-M2.7 \
  --jobs-dir results/run
```

> Import path is `adapters.gc_cli_external`, **not** `harbor.adapters...` — the top-level
> name `harbor` is the framework package and must not be shadowed. Running from inside this
> `harbor/` dir keeps `adapters` on the path while the framework imports from its venv.

### Notes

- **Backend URL is host-side** (`127.0.0.1`), not `host.docker.internal` — the loop runs on
  the host, not in the container.
- **Model id keeps the vendor path** (`MiniMaxAI/MiniMax-M2.7`); only the `openai/` provider
  prefix is stripped.
- The harbor tool pack lives in `src/tools/harbor/` (backend + 11 tool shims); the toggle is
  `use_harbor_tools` in `build_tools_data`.
