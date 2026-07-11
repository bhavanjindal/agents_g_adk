# Data Engineering Agent

A self-correcting, multi-agent data pipeline with two **human-in-the-loop** checkpoints.
It plans, loads, transforms, stores, and validates data — pausing for human approval
at the two moments where a mistake is most expensive to fix.

---

## Architecture

```
SequentialAgent  (DataEngineeringOrchestrator)  ← root_agent
│
│  Phase 1 completes fully before Phase 2 begins.
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
│         Human approves → call exit_loop() → move to Phase 2.
│         Human gives feedback → record it, do NOT exit_loop → Planner revises.
│
└── LoopAgent  (ExecutionLoop, max 5 rounds)
    │
    │  Executes the approved plan, retrying if quality checks fail.
    │
    ├── SequentialAgent  (DataPipeline)
    │   │
    │   ├── DataLoader
    │   │     Writes chunked ingestion code per the approved plan.
    │   │     Fixes the specific issue noted by the Validator on retries.
    │   │
    │   ├── DataTransformer
    │   │     Writes vectorised Polars transformation code.
    │   │     Applies plan's transform steps in order: filter → cast → nulls → enrich → agg.
    │   │
    │   ├── WriteApprover  [tools: request_input]          ← HITL checkpoint 2
    │   │     Presents write summary (destination, rows, format, partition key).
    │   │     Calls request_input() — human replies 'yes', 'staging', or 'abort'.
    │   │     Records decision as "WRITE DECISION: <choice>" for the Storer to read.
    │   │
    │   └── DataStorer
    │         Reads WRITE DECISION:
    │         'production' → writes to plan's destination (temp → validate → swap).
    │         'staging'    → writes to ./staging/<name> for human review.
    │         'abort'      → writes nothing; Validator will fail this iteration.
    │
    └── DataQualityValidator  [tools: exit_loop]
          Checks row counts, nulls, schema, value ranges, duplicate PKs.
          All pass + production write → call exit_loop() → done.
          Any fail → describe the issue → loop retries automatically.
```

### Why two separate LoopAgents?

A single loop would mix two concerns: plan correctness (should we do this at all?)
and execution quality (did we do it right?). Separating them means:

- The plan is signed off by a human **before** any data is touched
- Execution retries only fix code/data issues, never re-open the plan
- The human never sees a "plan approval" prompt mid-retry of a quality failure

### Why HITL at these two points?

| Checkpoint | Cost of getting it wrong without HITL |
|---|---|
| After planning | Low — just a plan on paper. Catching a wrong source or missing step here is free. |
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

--- Phase 2: Execution ---

DataLoader:      [writes ingestion code, logs 84,201 rows loaded]
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

---

## Models

All agents use `gemini-flash-latest`. Upgrade individual agents to `gemini-pro-latest`
if you need deeper reasoning — most likely candidates are `DataPipelinePlanner`
(complex schema inference) or `DataTransformer` (intricate multi-step logic).
