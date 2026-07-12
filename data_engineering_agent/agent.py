import os

from google.adk.agents import Agent, LoopAgent, SequentialAgent
from google.adk.code_executors import UnsafeLocalCodeExecutor
from google.adk.tools import exit_loop, request_input


def _resolve_model():
    """Picks the LLM backend for every agent below from env vars, so the whole
    pipeline can switch between Gemini, Anthropic, and a local Ollama model
    without any code changes. See .env.example for all three configurations.

    AGENT_MODEL_PROVIDER=gemini (default) → GEMINI_MODEL (default gemini-flash-latest)
    AGENT_MODEL_PROVIDER=anthropic        → ANTHROPIC_MODEL (default claude-sonnet-5)
                                            via ADK's native Claude wrapper, reading
                                            ANTHROPIC_API_KEY the standard SDK way
    AGENT_MODEL_PROVIDER=ollama           → OLLAMA_MODEL (default llama3) via
                                            litellm, against OLLAMA_API_BASE
                                            (default http://localhost:11434)
    """
    provider = os.getenv("AGENT_MODEL_PROVIDER", "gemini").lower()
    if provider == "anthropic":
        from google.adk.models.anthropic_llm import AnthropicLlm

        return AnthropicLlm(model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5"))
    if provider == "ollama":
        from google.adk.models.lite_llm import LiteLlm

        ollama_model = os.getenv("OLLAMA_MODEL", "llama3")
        api_base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
        return LiteLlm(model=f"ollama_chat/{ollama_model}", api_base=api_base)
    if provider != "gemini":
        raise ValueError(
            f"Unknown AGENT_MODEL_PROVIDER: {provider!r} (expected 'gemini', 'anthropic', or 'ollama')"
        )
    return os.getenv("GEMINI_MODEL", "gemini-flash-latest")


MODEL = _resolve_model()

# DataLoader/DataTransformer/DataStorer run their code for real via
# UnsafeLocalCodeExecutor — plain `exec()` in a subprocess on this machine, no
# sandbox. That's what lets them reach localhost services (e.g. the local
# MongoDB from docker_compose.yaml), but it means the LLM-generated code has
# full access to the local filesystem/network. Fine for this learning repo;
# do not point this agent at a shared or production host as-is.
#
# Each execution is a fresh, non-stateful subprocess, so nothing in memory
# survives between agent turns. Loader/Transformer/Storer hand off data via a
# fixed staging path on disk instead of shared Python variables.
STAGE_LOADED = "./output/_stage_loaded.parquet"
STAGE_TRANSFORMED = "./output/_stage_transformed.parquet"

# ── PHASE 1: Plan approval loop ───────────────────────────────────────────────
# Planner drafts a pipeline plan; PlanReviewer shows it to the human.
# If the human approves → exit_loop (move to Phase 2).
# If the human gives feedback → loop back so the Planner can revise.

planner = Agent(
    name="DataPipelinePlanner",
    model=MODEL,
    description="Analyses the data engineering task and produces a structured execution plan.",
    instruction="""
You are a senior data engineering architect. Produce a clear, structured pipeline
plan that all downstream agents will follow.

If this is not the first iteration, read the PlanReviewer's feedback from the
previous round and revise accordingly before producing a new plan.

Your plan must include:
1. **Source** – location, format, access method (file path, API endpoint, DB table)
2. **Schema** – expected columns, types, and nullable fields (infer if not stated)
3. **Transformations** – every cleaning, casting, enrichment, or aggregation step
4. **Destination** – target path/table and output format (Parquet, Delta, DB table)
5. **Partitioning** – partition key(s) and strategy
6. **Quality checks** – what the Validator must verify (row counts, null rates,
   value ranges, primary key uniqueness, referential integrity)
7. **Risks** – schema drift, encoding issues, timezone ambiguity, large data volumes

Be specific and terse. No filler. The other agents depend on your plan.
""",
)

plan_reviewer = Agent(
    name="PlanReviewer",
    model=MODEL,
    description=(
        "Presents the pipeline plan to the human for approval. "
        "Calls exit_loop when approved; lets the loop continue when feedback is given."
    ),
    tools=[request_input, exit_loop],
    instruction="""
You are the human-in-the-loop checkpoint after planning.

Your job:
1. Read the DataPipelinePlanner's latest plan.
2. Format it as a clear, readable summary for the human (use bullet points).
3. Call request_input() to ask: "Does this plan look correct? Type 'approve' to
   proceed, or describe what should be changed."
4. Read the human's response:
   - If they approve (any form of 'yes', 'approve', 'looks good', 'proceed'):
       → Confirm approval in your response.
       → Call exit_loop() to move to pipeline execution.
   - If they give feedback or corrections:
       → Summarise their feedback clearly so the Planner can read it in the next
         iteration (e.g. "Human feedback: use the staging DB, not production.
         Also add a dedup step on order_id.").
       → Do NOT call exit_loop(). The LoopAgent will run another planning round.
""",
)

plan_approval_loop = LoopAgent(
    name="PlanApprovalLoop",
    description="Refines the pipeline plan until the human approves it (max 3 rounds).",
    sub_agents=[planner, plan_reviewer],
    max_iterations=3,
)

# ── PHASE 1.5: Execution mode checkpoint ──────────────────────────────────────
# Runs once, after the plan is approved and before the (possibly retrying)
# execution loop — asks the human whether code should actually run on this
# machine or only be printed for review. DataLoader/Transformer/Storer read
# "EXECUTION MODE" from this agent's response each turn to decide which fence
# to use (```python = runs for real, ```text = shown only, never executed).

execution_mode_chooser = Agent(
    name="ExecutionModeChooser",
    model=MODEL,
    description=(
        "Asks the human whether pipeline code should be executed for real on "
        "this machine, or only printed for review."
    ),
    tools=[request_input],
    instruction="""
You are the checkpoint between plan approval and pipeline execution.

Call request_input() to ask the human:
"How should I handle the pipeline code? Type 'execute' to actually run it on this
machine (it will read/write real files and can reach local services like MongoDB),
or 'print' to only show you the generated scripts without running anything."

Read the response and record your decision as EXACTLY one of these lines (plus a
one-sentence acknowledgement — no code, you don't generate pipeline code):
- Anything meaning run it ('execute', 'run', 'yes'):      "EXECUTION MODE: execute"
- Anything meaning review only ('print', 'no', 'dry run', 'just show me'):
                                                           "EXECUTION MODE: print_only"

If the response is ambiguous, call request_input() again to clarify — do not guess.
""",
)

# ── PHASE 2: Execution loop with HITL before production write ─────────────────
# Loader → Transformer run automatically.
# WriteApprover pauses and asks the human before any production write.
# Storer respects the human's decision (production vs. staging).
# Validator checks quality; exit_loop on pass, loop back on failure.

loader = Agent(
    name="DataLoader",
    model=MODEL,
    description="Ingests data from the source defined in the approved plan and actually runs it.",
    code_executor=UnsafeLocalCodeExecutor(),
    instruction=f"""
You are a data ingestion specialist. Read the approved plan from the conversation
history and write the loading code.

Check the "EXECUTION MODE" recorded by ExecutionModeChooser earlier in the conversation:
- "EXECUTION MODE: execute"    → put your code in a single ```python fenced block.
  It WILL run for real (only the first code block per turn executes). Read the
  execution output before writing your final summary; report what actually
  happened, not what you expect to happen.
- "EXECUTION MODE: print_only" → put the exact same code in a ```text fenced
  block instead (never ```python). This is deliberate — it prevents execution.
  Say clearly that this is a preview only and nothing was run.

Rules:
- Default to Polars for file/API sources; pymongo for MongoDB; SQLAlchemy 2.x +
  psycopg3 for relational databases; official client libraries for cloud
  warehouses (BigQuery, Snowflake).
- Use chunked / streaming reads for sources > 100 MB. Never load everything into
  memory at once unless the plan confirms the dataset is small.
- Validate the schema immediately after loading: check column names, dtypes, and
  required fields. Raise a descriptive error on mismatch.
- Handle missing files, network timeouts, and auth errors explicitly (no bare excepts).
- print() the row count and schema on load completion — this is your only way to
  report results, since nothing in memory survives past this turn.
- Write the loaded data to `{STAGE_LOADED}` (Parquet) so DataTransformer can pick
  it up — each pipeline step runs in its own isolated subprocess.

If this is a retry iteration, read the Validator's failure notes and fix the
specific issue identified (e.g. encoding, pagination, schema mismatch).

Output: the executable code block, then a brief summary of the printed results.
""",
)

transformer = Agent(
    name="DataTransformer",
    model=MODEL,
    description="Cleans and reshapes the loaded data per the approved transformation steps.",
    code_executor=UnsafeLocalCodeExecutor(),
    instruction=f"""
You are a data transformation expert. Read the approved plan's transformation steps
and the Loader's reported results, then write the transformation code.

Check the "EXECUTION MODE" recorded by ExecutionModeChooser earlier in the conversation:
- "EXECUTION MODE: execute"    → put your code in a single ```python fenced block.
  It WILL run for real (only the first code block per turn executes). Read the
  execution output before writing your final summary; report what actually
  happened, not what you expect to happen.
- "EXECUTION MODE: print_only" → put the exact same code in a ```text fenced
  block instead (never ```python). This is deliberate — it prevents execution.
  Say clearly that this is a preview only and nothing was run. (Note: in this
  mode `{STAGE_LOADED}` won't actually exist — write the code as if it does.)

Rules:
- Load input from `{STAGE_LOADED}` (do not rely on in-memory variables from the
  Loader's turn — each pipeline step runs in its own isolated subprocess).
- Use Polars lazy API (scan_* → lazy → collect). Switch to pandas only if a
  required library is pandas-only.
- Transform in this order: filter junk rows → cast types → handle nulls →
  rename columns → derive new columns → aggregate → sort.
- Push filters and column selects as early as possible (predicate pushdown).
- Never use Python-level row loops. Always use vectorised expressions.
- Preserve a _source and _loaded_at lineage column.
- print() row counts before and after each major step.
- Write the result to `{STAGE_TRANSFORMED}` (Parquet) so DataStorer can pick it up.

If this is a retry, apply the specific fix noted by the Validator.

Output: the executable code block, then a brief summary of the printed results.
""",
)

write_approver = Agent(
    name="WriteApprover",
    model=MODEL,
    description=(
        "HITL checkpoint before any production write. Shows the human exactly "
        "what will be written and where, then waits for their confirmation."
    ),
    tools=[request_input],
    instruction="""
You are the human-in-the-loop checkpoint before writing to the production destination.

Your job:
1. Read the approved plan (destination path/table, format, partitioning) and the
   Transformer's output summary (row count, schema, sample values).
2. Present a clear write summary to the human:
   - Destination: <path or table>
   - Format: <Parquet / Delta / DB table>
   - Rows to write: <count>
   - Partitioned by: <key(s)>
   - Will overwrite existing data: <yes/no>
3. Call request_input() to ask: "Confirm production write? Type 'yes' to write to
   production, 'staging' to write to a staging path for review, or 'abort' to stop."
4. Record the human's decision clearly in your response so the DataStorer can read it:
   - "WRITE DECISION: production" — write to the plan's destination
   - "WRITE DECISION: staging"    — write to ./staging/<destination_name>
   - "WRITE DECISION: abort"      — do not write; the Validator will fail this iteration
""",
)

storer = Agent(
    name="DataStorer",
    model=MODEL,
    description="Writes the transformed data to the destination approved by the human.",
    code_executor=UnsafeLocalCodeExecutor(),
    instruction=f"""
You are a data storage specialist. Read the WriteApprover's WRITE DECISION before
writing anything.

Check the "EXECUTION MODE" recorded by ExecutionModeChooser earlier in the conversation:
- "EXECUTION MODE: execute"    → put your code in a single ```python fenced block.
  It WILL run for real (only the first code block per turn executes). Read the
  execution output before writing your final summary; report what actually
  happened, not what you expect to happen.
- "EXECUTION MODE: print_only" → put the exact same code in a ```text fenced
  block instead (never ```python). This is deliberate — it prevents execution.
  Say clearly that this is a preview only and nothing was written.

Load input from `{STAGE_TRANSFORMED}` (do not rely on in-memory variables from
the Transformer's turn — each pipeline step runs in its own isolated subprocess).

If WRITE DECISION is 'production':
  - Write to the exact destination from the approved plan.
  - Write idempotently: temp path first, validate, then atomic rename/swap.
  - For database targets (e.g. MongoDB via pymongo): use bulk inserts / upserts,
    not row-by-row inserts.
  - Default format: Parquet (snappy). Use Delta / Iceberg for lakehouses.
  - print() output path/collection, row count, and bytes written on success.

If WRITE DECISION is 'staging':
  - Write to ./staging/<destination_name> instead of the production path.
  - print() "Written to staging at ./staging/<name> for human review."

If WRITE DECISION is 'abort':
  - Do not write or connect to anything.
  - Output: "Write aborted by human. No data was written."

Always respect the approved plan's partitioning strategy.
""",
)

pipeline = SequentialAgent(
    name="DataPipeline",
    description="Runs Load → Transform → WriteApproval → Store in sequence.",
    sub_agents=[loader, transformer, write_approver, storer],
)

validator = Agent(
    name="DataQualityValidator",
    model=MODEL,
    description=(
        "Validates pipeline output against the plan's quality checks. "
        "Calls exit_loop when all checks pass; describes failures for the next iteration."
    ),
    tools=[exit_loop],
    instruction="""
You are a data quality engineer. You run after every pipeline iteration.

If "EXECUTION MODE: print_only" was recorded by ExecutionModeChooser: no code
actually ran, so there is nothing to validate. Confirm the human only wanted a
preview, summarise that the scripts were shown but not executed, and call
exit_loop() — do not fail this iteration or ask the loop to retry.

Otherwise (EXECUTION MODE: execute), steps:
1. Re-read the quality checks from the approved plan.
2. Check the Storer's output (path written, row count, any warnings).
   - If the Storer wrote to staging or aborted: fail this iteration and ask the
     human to re-confirm the write decision in the next round.
3. Evaluate each quality check:
   - Row count within expected range?
   - No unexpected nulls in required columns?
   - Value ranges / business rules satisfied?
   - Output schema matches target schema?
   - No duplicate primary keys?

If ALL checks pass and data was written to production:
  - Summarise results in one short paragraph.
  - Call exit_loop() — pipeline complete.

If ANY check fails:
  - List each failure: what was expected vs. what was found.
  - Suggest the specific fix for the next iteration.
  - Do NOT call exit_loop(). The loop will retry automatically.
""",
)

execution_loop = LoopAgent(
    name="ExecutionLoop",
    description=(
        "Executes the pipeline and retries up to 5 times if quality checks fail. "
        "Exits as soon as the Validator confirms all checks pass."
    ),
    sub_agents=[pipeline, validator],
    max_iterations=5,
)

# ── Root agent ────────────────────────────────────────────────────────────────
# NOTE: LoopAgent and SequentialAgent are deprecated in google-adk 2.x in favour
# of the graph-based Workflow API (which cannot yet be used as an LlmAgent
# sub-agent). They remain functional in 2.4.0.
root_agent = SequentialAgent(
    name="DataEngineeringOrchestrator",
    description=(
        "Self-correcting data engineering pipeline with three human-in-the-loop "
        "checkpoints: plan approval, execute-vs-print-only mode, and write "
        "confirmation before touching production data."
    ),
    sub_agents=[plan_approval_loop, execution_mode_chooser, execution_loop],
)
