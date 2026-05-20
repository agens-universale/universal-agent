# science-mcp

MCP server for unified scientific literature search. Wraps 10 public APIs
(OpenAlex, CrossRef, PubMed/PMC, arXiv, Semantic Scholar, DBLP, CORE,
OpenCitations, bioRxiv/medRxiv) behind 7 normalized tools.

## Install

From the parent repo (`background-agent/`):

```bash
python recommended_mcp/install.py
```

Or directly:

```bash
pip install /Users/gkave/SberAI/background-agent/external/science-mcp
```

After install, the `science-mcp` entry-point is on `$PATH` of the venv.

## Run the daemon

science-mcp runs as a long-lived process so in-memory throttling persists
across agent calls. Default: HTTP on `127.0.0.1:8765`, endpoint path `/mcp`.

```bash
# Foreground (default transport is http)
science-mcp --transport http

# Custom bind
science-mcp --transport http --host 0.0.0.0 --port 9000

# Background
nohup science-mcp --transport http > /tmp/science-mcp.log 2>&1 &
```

Stdio mode is kept as a legacy fallback for one-shot debugging (process
spawned per agent call, no shared throttling):

```bash
science-mcp --transport stdio
```

## Configure (env)

| Variable | Default | Purpose |
|---|---|---|
| `SCIENCE_MCP_MAILTO` | `team@company.example` | OpenAlex polite-pool + CrossRef UA |
| `SCIENCE_MCP_HOST` | `127.0.0.1` | Daemon bind host |
| `SCIENCE_MCP_PORT` | `8765` | Daemon bind port |
| `NCBI_API_KEY` | unset | Raises NCBI rate limit 3/s → 10/s |
| `SEMANTIC_SCHOLAR_API_KEY` | unset | Required for usable S2 (no-key = instant 429) |
| `CORE_API_KEY` | unset | More stable CORE access |
| `SCIENCE_MCP_CACHE_DIR` | `~/.cache/science-mcp` | Disk JSON cache |
| `SCIENCE_MCP_CACHE` | `1` | `0` disables disk cache |
| `SCIENCE_MCP_HTTP_TIMEOUT` | `15` | Default per-request timeout, seconds |
| `SCIENCE_MCP_QUEUE_TIMEOUT` | `120` | Max wait in throttler queue, seconds |

## Connect to agent

Copy `recommended_mcp/science/agent_config.snippet.yml` (in the parent repo)
into the `mcp-servers:` section of your agent's `config.yml`. Default
connection: `transport: stream_http`, `url: http://127.0.0.1:8765/mcp`.

## Documentation

Implementation plan and design: `.docs/superpowers/plans/2026-05-19-science-mcp-implementation.md`.

## Architecture

Layered: utilities → adapters → tools → server. See
`.docs/superpowers/plans/2026-05-19-science-mcp-implementation.md` for the
full implementation plan.

## Operational constraints (from live probes 2026-05-18)

- Semantic Scholar without an API key returns immediate 429 — adapter raises `unauthorized`.
- CrossRef `filter=member:246` includes CSHL journals, not only preprints — `cshl_preprints` adapter secondary-filters by DOI prefix `10.1101/YYYY.MM.DD.NNNNNN`.
- PMC `efetch` returns full-text XML up to 236KB — `ncbi_fetch` defaults to `summary`; `fetch` mode capped at 50KB.
- OpenCitations `citations` returns ~5MB for popular DOIs — disk cache TTL 24h is mandatory.
- arXiv enforces 1 req / 3s; `preprint_search` is isolated from `literature_search`.
- CORE endpoint requires trailing slash: `/v3/search/works/?q=...`.
- OpenCitations endpoint moved: `https://api.opencitations.net/index/v1/...`.
- `stream_stdio` transport in `gc-cli-pub` whitelists inherited env to `HOME/LOGNAME/PATH/SHELL/TERM/USER` — list all `SCIENCE_MCP_*` and provider keys explicitly under `env:` in agent config. HTTP transport bypasses this — daemon inherits its own shell env.
