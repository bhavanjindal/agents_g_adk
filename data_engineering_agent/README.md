# Data Engineering Agent

A self-correcting, multi-agent data pipeline built with Google ADK. It plans, loads,
transforms, stores, and validates data — retrying automatically when quality checks fail.

---

## Architecture

```
LoopAgent  (DataEngineeringOrchestrator)
│
│  Runs the inner pipeline + validator on each iteration.
│  Exits when the Validator calls exit_loop() or after 5 iterations.
│
├── SequentialAgent  (DataPipeline)
│   │
│   │  Each stage sees the full conversation history of all prior stages.
│   │
│   ├── DataPipelinePlanner
│   │     Reads the user task → produces a structured plan:
│   │     source, schema, transformations, destination, quality checks, risks.
│   │
│   ├── DataLoader
│   │     Reads the plan → writes chunked ingestion code.
│   │     Defaults: Polars for files/APIs, SQLAlchemy 2.x for relational DBs.
│   │
│   ├── DataTransformer
│   │     Reads the plan + loader output → writes vectorised transformation code.
│   │     Uses Polars lazy API. Applies filter → cast → null-handling → enrich → agg.
│   │
│   └── DataStorer
│         Reads the plan + transformer output → writes idempotent storage code.
│         Defaults: Parquet (snappy), partitioned by access pattern, temp-then-swap.
│
└── DataQualityValidator  (has exit_loop tool)
      Runs after each full pipeline iteration.
      Checks row counts, nulls, schema, value ranges, duplicate keys.
      → All pass: calls exit_loop() — pipeline complete.
      → Any fail: describes failures so the next iteration can fix them.
```

### Why LoopAgent + SequentialAgent?

Data engineering work is inherently iterative — schema mismatches, null edge cases,
and encoding issues are rarely caught on the first pass. The loop lets the system
self-correct instead of failing hard. The inner SequentialAgent enforces the correct
stage order (you cannot transform before loading).

> Note: `LoopAgent` and `SequentialAgent` are deprecated in google-adk 2.x in favour
> of the graph-based `Workflow` API. They remain functional in 2.4.0 and are used here
> because `Workflow` cannot yet be used as an `LlmAgent` sub-agent.

---

## How to run

```bash
# Terminal REPL
uv run adk run data_engineering_agent

# Browser UI (recommended — shows each agent's output separately)
uv run adk web data_engineering_agent
```

---

## Example prompts

**Basic ETL**
```
Load sales_data.csv, remove duplicate order_ids, cast order_date to date,
and store the result as Parquet partitioned by order_date.
```

**API ingestion**
```
Fetch all pages from the /api/v1/events endpoint (requires Bearer token in
Authorization header), keep only events where status = 'completed',
and upsert into the postgres events table on event_id.
```

**Database-to-warehouse**
```
Extract all rows from the mysql orders table where updated_at > yesterday,
join with the customers table on customer_id, and load into BigQuery
dataset=analytics, table=orders_enriched, partitioned by order_date.
```

**Troubleshooting**
```
My Spark job reading from GCS is OOMing on the executor. The dataset is
500 GB Parquet and I'm doing a join on user_id. How should I fix this?
```

---

## Key design decisions

| Decision | Rationale |
|---|---|
| Polars as default (not pandas) | Lazy evaluation, columnar, faster on modern hardware, better for large-than-RAM datasets via streaming |
| Storer writes to temp path first | Prevents partial writes corrupting the live dataset if the job is interrupted |
| Validator controls loop exit | Quality checks are data-driven — the validator reads the Planner's checks, not a hardcoded list |
| max_iterations = 5 | Prevents infinite loops on persistent data quality issues; 5 is enough for most real-world self-correction cycles |
| Each agent sees full conversation history | Downstream agents (Transformer, Storer) have the full context of what the Planner and Loader decided — no need to re-specify |

---

## Model

All agents use `gemini-flash-latest` — sufficient for code generation and reasoning
tasks at this complexity level. Upgrade to `gemini-pro-latest` if you need deeper
multi-hop reasoning (e.g. very complex SQL generation or large schema inference).
