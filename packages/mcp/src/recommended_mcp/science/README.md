# science — science-mcp

Unified MCP server for scientific literature search across OpenAlex,
CrossRef, PubMed/PMC, arXiv, Semantic Scholar, DBLP, CORE, OpenCitations,
bioRxiv/medRxiv. Exposes 7 tools.

Source — in `external/science-mcp/`. Installed by `install.py` via
`pip install ./external/science-mcp` (PEP 517 build).

## Requirements

- Python 3.10+.
- Active venv of the agent before running `install.py`.

## Install

From the repo root:

```bash
python recommended_mcp/install.py
```

For dev (live-edit `external/science-mcp/`):

```bash
python recommended_mcp/install.py --editable
```

After install, the `science-mcp` entry-point lives on `$PATH` of the venv.

## Run the daemon

science-mcp runs as a long-lived process so in-memory throttling persists
across `gc-cli` calls. Default: HTTP on `127.0.0.1:8765`, endpoint path
`/mcp`.

```bash
# Recommended: supervised start (idempotent, Ctrl+C stops the daemon)
python recommended_mcp/run.py

# Manual foreground
science-mcp --transport http
```

Stdio mode (one-off subprocess spawned by the agent):

```bash
science-mcp --transport stdio
```

## Connect to agent

Copy `agent_config.snippet.yml` into the `mcp-servers:` section of your
`config.yml` agent file.

## Configuration via env

The science-mcp daemon reads env from its own shell (the agent does NOT
forward env in HTTP mode — the daemon already runs with its own env). Set
these before launching `science-mcp`:

| Variable | Default | Purpose |
|---|---|---|
| `SCIENCE_MCP_MAILTO` | `team@company.example` | OpenAlex polite-pool + CrossRef UA |
| `SCIENCE_MCP_HOST` | `127.0.0.1` | Daemon bind host |
| `SCIENCE_MCP_PORT` | `8765` | Daemon bind port |
| `NCBI_API_KEY` | unset | Raises NCBI rate limit 3/s → 10/s |
| `SEMANTIC_SCHOLAR_API_KEY` | unset | Required for usable S2 |
| `CORE_API_KEY` | unset | More stable CORE access |
| `SCIENCE_MCP_CACHE_DIR` | `~/.cache/science-mcp` | Disk JSON cache |
| `SCIENCE_MCP_CACHE` | `1` | Set to `0` to disable cache |
| `SCIENCE_MCP_HTTP_TIMEOUT` | `15` | Per-request timeout, seconds |
| `SCIENCE_MCP_QUEUE_TIMEOUT` | `120` | Max wait in throttler queue, seconds |
