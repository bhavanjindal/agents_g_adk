# Data Engineering Agent

A self-correcting, multi-agent data pipeline with three **human-in-the-loop** checkpoints.
It plans, loads, transforms, stores, and validates data — pausing for human approval
at the moments where a mistake is most expensive to fix.

`DataLoader`, `DataTransformer`, and `DataStorer` are capable of actually executing
their generated code (via `UnsafeLocalCodeExecutor`) instead of just printing it —
but only if the human opts in at the `ExecutionModeChooser` checkpoint. See
[Real code execution](#real-code-execution) below before choosing 'execute'.

---

## Real code execution

Right after plan approval, `ExecutionModeChooser` asks the human once:

> "How should I handle the pipeline code? Type 'execute' to actually run it on
> this machine ... or 'print' to only show you the generated scripts without
> running anything."

That answer is recorded as `EXECUTION MODE: execute` or `EXECUTION MODE: print_only`
and read by `DataLoader`, `DataTransformer`, and `DataStorer` on every turn:

- **`execute`** → the agent puts its code in a \`\`\`python fenced block.
  `code_executor=UnsafeLocalCodeExecutor()` on each of these three agents makes
  ADK auto-detect that fence and run it with a plain `exec()` in a local
  subprocess — no sandbox, no network isolation. That's what lets these agents
  reach a local service like the MongoDB from `docker_compose.yaml` at
  `localhost:27017`, but it also means LLM-generated code has full access to
  whatever the local process can reach (filesystem, network, credentials in env
  vars). Fine for this learning repo on your own machine; do not choose 'execute'
  against a shared host or with production credentials without adding a real
  sandbox (`ContainerCodeExecutor` / a remote executor).
- **`print_only`** → the same code goes in a \`\`\`text fence instead, which ADK's
  delimiter matching ignores, so nothing runs. Nothing is read from or written to
  disk/Mongo; the agents describe the code as a preview only.

When `execute` is chosen, each execution is a **fresh, non-stateful subprocess** —
nothing in memory survives between agent turns. So instead of passing dataframes
in-process, the pipeline hands off through fixed staging files:

- `DataLoader` writes → `./output/_stage_loaded.parquet`
- `DataTransformer` reads that, writes → `./output/_stage_transformed.parquet`
- `DataStorer` reads that and writes the final destination (production/staging per
  the human's WRITE DECISION)

In `print_only` mode, `DataQualityValidator` skips its checks entirely (there's
nothing real to validate) and exits the loop after one pass.

`PlanReviewer`, `ExecutionModeChooser`, `WriteApprover`, and `DataQualityValidator`
never run pipeline code themselves — they only gate the loop
(`request_input` / `exit_loop`).

---

## Architecture

```
SequentialAgent  (DataEngineeringOrchestrator)  ← root_agent
│
│  Each phase completes fully before the next begins.
│
├── LoopAgent  (PlanApprovalLoop, max 3 rounds)
│   │
│   │  Refines the plan until the human approves it.
│   │
│   ├── DataPipelinePlanner
│   │     Reads the task (and any prior human feedback) → produces a structured plan:
│   │     source, schema, transformations, destination, quality checks, risks.
│   │
│   └── PlanReviewer  [tools: request_input, exit_loop]   ← HITL checkpoint 1
│         Formats the plan for the human and calls request_input().
│         Human approves → call exit_loop() → move to Phase 1.5.
│         Human gives feedback → record it, do NOT exit_loop → Planner revises.
│
├── ExecutionModeChooser  [tools: request_input]          ← HITL checkpoint 2
│     Asks once: 'execute' (run for real) or 'print' (preview only, no execution).
│     Records "EXECUTION MODE: execute" or "EXECUTION MODE: print_only" for the
│     pipeline agents below to read on every turn.
│
└── LoopAgent  (ExecutionLoop, max 5 rounds)
    │
    │  Executes the approved plan, retrying if quality checks fail.
    │
    ├── SequentialAgent  (DataPipeline)
    │   │
    │   ├── DataLoader  [code_executor: UnsafeLocalCodeExecutor]
    │   │     execute mode    → ```python fence, RUNS for real, stages
    │   │                        ./output/_stage_loaded.parquet.
    │   │     print_only mode → ```text fence, shown only, nothing runs.
    │   │     Fixes the specific issue noted by the Validator on retries.
    │   │
    │   ├── DataTransformer  [code_executor: UnsafeLocalCodeExecutor]
    │   │     execute mode    → ```python fence, RUNS for real, stages
    │   │                        ./output/_stage_transformed.parquet.
    │   │     print_only mode → ```text fence, shown only, nothing runs.
    │   │
    │   ├── WriteApprover  [tools: request_input]          ← HITL checkpoint 3
    │   │     Presents write summary (destination, rows, format, partition key).
    │   │     Calls request_input() — human replies 'yes', 'staging', or 'abort'.
    │   │     Records decision as "WRITE DECISION: <choice>" for the Storer to read.
    │   │
    │   └── DataStorer  [code_executor: UnsafeLocalCodeExecutor]
    │         Reads WRITE DECISION, then in execute mode RUNS the write:
    │         'production' → writes to plan's destination (temp → validate → swap).
    │         'staging'    → writes to ./staging/<name> for human review.
    │         'abort'      → writes nothing; Validator will fail this iteration.
    │         In print_only mode: shows the write code, writes nothing.
    │
    └── DataQualityValidator  [tools: exit_loop]
          print_only mode → nothing to validate; exit_loop() immediately.
          execute mode    → checks row counts, nulls, schema, value ranges, dup PKs.
          All pass + production write → call exit_loop() → done.
          Any fail → describe the issue → loop retries automatically.
```

### Why two separate LoopAgents?

A single loop would mix two concerns: plan correctness (should we do this at all?)
and execution quality (did we do it right?). Separating them means:

- The plan is signed off by a human **before** any data is touched
- Execution retries only fix code/data issues, never re-open the plan
- The human never sees a "plan approval" prompt mid-retry of a quality failure

### Why HITL at these points?

| Checkpoint | Cost of getting it wrong without HITL |
|---|---|
| After planning | Low — just a plan on paper. Catching a wrong source or missing step here is free. |
| Execute vs. print-only | High — without asking first, an agent with a code executor would silently run LLM-generated code against real local services (filesystem, Mongo) the moment it decided to. |
| Before production write | High — overwriting a production table or file is hard to reverse, especially at scale. |

Interrupting between Load → Transform → Store would add friction with no benefit
(those steps follow the already-approved plan mechanically).

---

## How to run

```bash
# Terminal REPL (shows each agent's turn inline)
uv run adk run data_engineering_agent

# Browser UI (recommended — shows each agent separately, HITL prompts appear in UI)
uv run adk web data_engineering_agent
```

With no env vars set, this uses whatever `.env` says (see `.env.example` at the
repo root — `AGENT_MODEL_PROVIDER` defaults to `gemini` if unset there too).
To run a single invocation on a specific provider without editing `.env`,
prefix the command instead — see [Models](#models) below for the full
per-provider variable list:

```bash
# One-off Gemini run
AGENT_MODEL_PROVIDER=gemini uv run adk run data_engineering_agent

# One-off Anthropic run
AGENT_MODEL_PROVIDER=anthropic ANTHROPIC_MODEL=claude-sonnet-5 \
  uv run adk run data_engineering_agent

# One-off Ollama run — model MUST support tool calling, see the Ollama note below
AGENT_MODEL_PROVIDER=ollama OLLAMA_MODEL=qwen2.5:14b \
  uv run adk run data_engineering_agent
```

---

## Example session

```
User:  Load sales.csv, remove duplicate order_ids, cast order_date to date,
       store as Parquet partitioned by order_date.

--- Phase 1: Plan Approval ---

DataPipelinePlanner:  [produces plan]

PlanReviewer:  Here's the pipeline plan:
  • Source: sales.csv (CSV, local filesystem)
  • Schema: order_id (str), order_date (str→date), amount (float), customer_id (str)
  • Transformations: dedup on order_id, cast order_date to Date
  • Destination: ./output/sales.parquet, partitioned by order_date
  • Quality checks: row count ≥ 1, no nulls in order_id, no duplicate order_ids
  Does this look correct? Type 'approve' or describe what should be changed.

User:  approve

PlanReviewer:  Plan approved. → calls exit_loop()

--- Phase 1.5: Execution Mode ---

ExecutionModeChooser:  How should I handle the pipeline code? Type 'execute' to
  actually run it on this machine, or 'print' to only show you the generated
  scripts without running anything.

User:  execute

ExecutionModeChooser:  EXECUTION MODE: execute

--- Phase 2: Execution ---

DataLoader:      [writes AND runs ingestion code, logs 84,201 rows loaded]
DataTransformer: [writes dedup + cast code, logs 83,950 rows after dedup]

WriteApprover:   Write summary:
  • Destination: ./output/sales.parquet
  • Format: Parquet (snappy)
  • Rows: 83,950
  • Partitioned by: order_date
  • Will overwrite existing: no (new path)
  Confirm production write? Type 'yes', 'staging', or 'abort'.

User:  yes

WriteApprover:  WRITE DECISION: production
DataStorer:     [writes to ./output/sales.parquet, logs 83,950 rows, 12.4 MB]

DataQualityValidator:  All checks passed:
  ✓ 83,950 rows (expected ≥ 1)
  ✓ No nulls in order_id
  ✓ No duplicate order_ids
  Pipeline complete. → calls exit_loop()
```

---

## Key design decisions

| Decision | Rationale |
|---|---|
| Polars lazy API by default | Lazy evaluation + columnar format handles larger-than-RAM datasets; predicate pushdown minimises I/O |
| WriteApprover uses 'staging' option | Gives the human a safe middle ground — review the data before committing to production |
| Storer writes temp → validate → swap | Prevents partial writes from corrupting the live dataset if the job is interrupted mid-write |
| Validator controls ExecutionLoop exit | Quality checks are data-driven from the plan, not hardcoded — works for any pipeline |
| Plan loop max 3, execution loop max 5 | Plan rarely needs more than 2 revisions; execution may need more retries for data quality edge cases |
| Execute/print-only chosen once, outside the retry loop | It's a session-wide setting, not a per-attempt one — asking again on every retry would be noise |
| print_only uses a ```text fence, not a flag | ADK's code executor triggers purely off fence language; switching fences is a reliable way to gate real execution without extra tool-call plumbing |

---

## Models

All agents share one `MODEL` (`agent.py`), resolved once at import time by
`_resolve_model()` from environment variables — switch every agent in the
pipeline between Gemini, Anthropic (on-demand), and a local Ollama model
without touching code:

```bash
# Gemini (default) — reads GOOGLE_API_KEY from .env
AGENT_MODEL_PROVIDER=gemini            # or just omit it
GEMINI_MODEL=gemini-flash-latest       # optional override, e.g. gemini-pro-latest

# Anthropic (Claude), called on-demand — reads ANTHROPIC_API_KEY from .env
AGENT_MODEL_PROVIDER=anthropic
ANTHROPIC_MODEL=claude-sonnet-5        # or claude-opus-4-8 (harder reasoning),
                                        # claude-haiku-4-5 (cheap/fast)

# Local Ollama — no API key, no network egress; requires `ollama serve` running
AGENT_MODEL_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:14b               # must support TOOL CALLING — see note below
OLLAMA_API_BASE=http://localhost:11434 # optional, this is the default
```

Set these in `.env` (see `.env.example` at the repo root — it's auto-loaded) or
export them before running `adk run` / `adk web`. Whichever provider is active
in `.env` is what a plain `uv run adk run data_engineering_agent` uses; prefix
the command with the vars above instead for a one-off run on a different
provider (see [How to run](#how-to-run)).

The Ollama path routes through [LiteLLM](https://docs.litellm.ai/)
(`google.adk.models.lite_llm.LiteLlm`); the Anthropic path uses ADK's native
`google.adk.models.anthropic_llm.AnthropicLlm` wrapper (backed by the official
`anthropic` Python SDK) — both are already project dependencies. It resolves
credentials the standard SDK way (`ANTHROPIC_API_KEY` env var); no other auth
wiring is needed. (ADK also exports a `Claude` class from the same module —
don't use that one here, it's hardcoded to `AsyncAnthropicVertex` for
Claude-on-Vertex-AI and demands `GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION`
instead of an API key.)

### ⚠️ Ollama models must support tool calling

Every HITL checkpoint in this agent (`PlanReviewer`, `ExecutionModeChooser`,
`WriteApprover`, `DataQualityValidator`) works by the model calling a function
tool (`request_input` / `exit_loop`) — this is not optional plumbing, it's how
the agent pauses for human input and ends loops at all. A model with no
function-calling support will error outright: `llama3`, `mistral`, `llama2`,
and `falcon` on Ollama all failed with `does not support tools` when tested
against this agent.

Check any local model before pointing this agent at it:

```bash
curl -s http://localhost:11434/api/show -d '{"name":"MODEL_NAME"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('capabilities'))"
# Must include 'tools' in the list, e.g. ['completion', 'tools']
```

Confirmed working: `qwen2.5:14b` (`ollama pull qwen2.5:14b`, ~9GB). If you want
something smaller, `qwen2.5:3b` and `llama3.2:3b` are from tool-calling-capable
model families, but verify with the command above after pulling — capability
support isn't guaranteed to hold at every size within a family.

Notes:
- This repo pins `litellm<1.90`. Newer litellm releases ship an optional Rust
  extension with no prebuilt wheel for some platforms (this repo hit that
  building from source on macOS/arm64) — the pin avoids the broken build.
- Model quality varies a lot across providers. Gemini and Claude are both
  strong at the reasoning-heavy steps (`DataPipelinePlanner`,
  `DataTransformer`); a local model is generally the weakest of the three
  there and is where the HITL checkpoints matter most.
- Claude billing is per-token and provider-side (no local server) — treat
  `AGENT_MODEL_PROVIDER=anthropic` as an on-demand switch for when you want
  Claude's reasoning quality for a run, not a default left on unattended.
- All agents currently share the same model tier (no per-agent overrides). If
  you want to mix providers — e.g. Gemini for planning and Claude for
  execution — split `MODEL` into per-agent variables and wire them into the
  relevant `Agent(...)` calls the same way.
