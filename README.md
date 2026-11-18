# RCAgent

RCAgent investigates operational questions with read-only MCP tools. A primary Agent plans the investigation, queries directly when appropriate, delegates scoped work to Workers, and writes a concise answer from archived evidence. This Baseline includes PRD44 direct queries, programmatic fact handoff, proactive closeout and transport retry management.

## Setup

Use Python 3.13 and Poetry from this repository root.

```sh
poetry install
cp .env.example .env
```

Set `RCAGENT_LLM_API_KEY`, `RCAGENT_LLM_BASE_URL` and `RCAGENT_LLM_MODEL` in `.env`. Select `RCAGENT_MCP_PROFILE=native`, `business` or `integrated`. Configure the corresponding `RCAGENT_NATIVE_MCP_URL` and/or `RCAGENT_BUSINESS_MCP_URL` and required tokens for your independently deployed MCP services.

Prepare a JSON file with an `incident_text` question, including the object and time window to investigate, then run.

```sh
poetry run python -m local_testing.run_investigation var/runs/example task.json
```

The output directory must be new. The LangGraph entry is `graph.py:parent_graph`. The default end-to-end budget is 300 seconds. Worker, model and tool concurrency defaults are 5, 1 and 8. Actual latency depends on the configured model and MCP services.

## Verification and evidence

```sh
poetry run python -m unittest local_testing.tests.test_prd44_fetch_handoff local_testing.tests.test_prd42_short_dispatch
```

Runs save `events.jsonl`, `sources.json`, raw tool responses and `report.md`. Set `RCAGENT_NATIVE_TRACE_REQUESTS=true` to save public model request snapshots. Program checks record references, scopes and execution receipts. Evaluate business conclusions against the actual evidence.

Local secrets, generated data and migration backups are excluded by `.gitignore`. Historical development notes are in `docs/historical-development-notes.md`. Historical experiment scripts require their own datasets and external services. Optional `legacy_stdio` replay requires an explicitly installed external server at `RCAGENT_LEGACY_MCP_SERVER`.

## Provenance

RCAgent evolved from SRE-agent. [Original attribution and citation metadata](docs/upstream/README.md) are retained. This repository uses an independent Git history. Installation and default product execution use this repository and configured MCP endpoints.
