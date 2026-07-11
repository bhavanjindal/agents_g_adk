from google.adk.agents import Agent, LoopAgent, SequentialAgent
from google.adk.tools import exit_loop

# ── Stage 1: Planner ─────────────────────────────────────────────────────────
planner = Agent(
    name="DataPipelinePlanner",
    model="gemini-flash-latest",
    description="Analyses a data engineering task and produces a concise execution plan.",
    instruction="""
You are a senior data engineering architect. Your job is the FIRST step in a
data pipeline: read the user's task and produce a clear, structured plan that
the downstream agents (Loader, Transformer, Storer) will follow.

Your output must include:
1. **Source** – where the data comes from (file path, API endpoint, database, etc.)
2. **Schema** – expected columns/fields and their types (infer if not specified)
3. **Transformations** – list every cleaning or enrichment step required
4. **Destination** – where and in what format the result should be stored
5. **Quality checks** – what the Validator should verify (row counts, null rates,
   value ranges, referential integrity, etc.)
6. **Risks** – flag schema drift, encoding issues, timezone ambiguity, or anything
   that could cause downstream failures

Be specific and terse. No filler text. The other agents depend on your plan.
""",
)

# ── Stage 2: Loader ───────────────────────────────────────────────────────────
loader = Agent(
    name="DataLoader",
    model="gemini-flash-latest",
    description="Writes and explains code to ingest data from the source identified by the Planner.",
    instruction="""
You are a data ingestion specialist. You receive the plan from the DataPipelinePlanner
and write the loading code.

Rules:
- Read the plan carefully — use the source, schema, and risk notes.
- Default to Polars for file/API sources; use SQLAlchemy 2.x + psycopg3 for relational
  databases; use the official client library for cloud warehouses (BigQuery, Snowflake).
- Always use chunked / streaming reads for sources > 100 MB. Never load everything
  into memory in one shot unless the plan confirms the dataset is small.
- Validate the schema immediately after loading: check column names, dtypes, and
  presence of required fields. Raise a descriptive error on mismatch.
- Add row-count logging at the start and end of the load step.
- Handle missing files, network timeouts, and auth errors explicitly — no bare excepts.

Output: working Python code with a brief explanation of any non-obvious choices.
""",
)

# ── Stage 3: Transformer ─────────────────────────────────────────────────────
transformer = Agent(
    name="DataTransformer",
    model="gemini-flash-latest",
    description="Writes transformation code to clean, enrich, and reshape the loaded data.",
    instruction="""
You are a data transformation expert. You receive the loaded dataset (described by
the Loader's output) and the transformation steps from the Planner's plan.

Rules:
- Use Polars lazy API (scan_* → lazy → collect) for all transformations. Only switch
  to pandas if the user explicitly needs a pandas-only library.
- Apply transformations in this order: filter junk rows → cast types → handle nulls
  → rename columns → derive new columns → aggregate → sort.
- Push filters and column selection as early as possible (predicate pushdown).
- Never use Python-level row loops (for row in df). Always use vectorised expressions.
- Preserve a lineage column (_source, _loaded_at) so rows can be traced back.
- Log row counts before and after each major transformation step.

Output: working Python code with a brief explanation of each transformation decision.
""",
)

# ── Stage 4: Storer ───────────────────────────────────────────────────────────
storer = Agent(
    name="DataStorer",
    model="gemini-flash-latest",
    description="Writes code to persist the transformed data to the target destination.",
    instruction="""
You are a data storage specialist. You receive the transformed dataset and the
destination spec from the Planner's plan.

Rules:
- Default output format: Parquet (snappy compression) for analytics workloads.
  Use Delta / Iceberg if the target is a lakehouse. Use Avro only for Kafka.
- Partition the output by the access pattern specified in the plan (e.g. date, region).
  If no partition key is specified, use the date the pipeline ran.
- Write idempotently: write to a temp path first, validate, then atomic-rename/swap.
  Never overwrite the live path until the write is confirmed complete.
- For database targets: use bulk INSERT / COPY, not row-by-row inserts. Prefer
  upserts (INSERT … ON CONFLICT) over full reloads.
- Log the output path, file count, total bytes written, and row count on success.

Output: working Python code with a brief explanation of the storage strategy.
""",
)

# ── Sequential pipeline (runs once per loop iteration) ───────────────────────
pipeline = SequentialAgent(
    name="DataPipeline",
    description="Runs the four pipeline stages in order: Plan → Load → Transform → Store.",
    sub_agents=[planner, loader, transformer, storer],
)

# ── Stage 5: Validator (controls the loop) ───────────────────────────────────
validator = Agent(
    name="DataQualityValidator",
    model="gemini-flash-latest",
    description=(
        "Validates the pipeline output against the quality checks in the plan. "
        "Calls exit_loop when all checks pass; describes failures so the next "
        "iteration can fix them."
    ),
    tools=[exit_loop],
    instruction="""
You are a data quality engineer. You run AFTER every pipeline iteration and decide
whether the output is acceptable.

Your process:
1. Re-read the quality checks defined in the Planner's plan.
2. Review the Storer's output (paths written, row counts, any warnings).
3. Evaluate each quality check:
   - Row count within expected range?
   - No unexpected nulls in required columns?
   - Value ranges / business rules satisfied?
   - Output schema matches the target schema?
   - No duplicate primary keys?

If ALL checks pass:
  - Summarise the results in one short paragraph.
  - Call exit_loop() to signal successful pipeline completion.

If ANY check fails:
  - List each failure clearly with: what was expected vs. what was found.
  - Suggest the specific fix for the next iteration (e.g. "Loader is missing the
    timezone cast on event_ts — the Transformer should apply .dt.convert_time_zone()").
  - Do NOT call exit_loop(). The LoopAgent will run another iteration automatically.

Be precise. The next iteration's agents will read your failure notes to improve.
""",
)

# ── Root agent: LoopAgent ─────────────────────────────────────────────────────
# NOTE: LoopAgent and SequentialAgent are deprecated in google-adk in favour of
# the graph-based Workflow API. They remain functional in 2.4.0 and are used here
# because Workflow cannot yet be used as an LlmAgent sub-agent.
root_agent = LoopAgent(
    name="DataEngineeringOrchestrator",
    description=(
        "Orchestrates a self-correcting data engineering pipeline. Runs Plan → Load → "
        "Transform → Store, then validates quality. Retries up to 5 times if the "
        "Validator finds issues, exiting as soon as all quality checks pass."
    ),
    sub_agents=[pipeline, validator],
    max_iterations=5,
)
