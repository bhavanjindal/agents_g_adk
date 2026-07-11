# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This is a learning repo for exploring various types of autonomous agents using Google ADK. Each agent subdirectory is a self-contained example — favor clarity and explicitness over abstraction when adding new ones.

## Package management

Use `uv` for all dependency operations — never `pip` directly.

```bash
uv sync                  # install/update all dependencies
uv add <package>         # add a new dependency
uv run <script>          # run a script in the venv
```

## Running agents

ADK agents are run via the `adk` CLI (installed with `google-adk`):

```bash
uv run adk run hello_agent        # run an agent in the terminal
uv run adk web hello_agent        # launch the ADK web UI for the agent
```

## Environment setup

Copy `.env.example` to `.env` and fill in your Google API key:

```
GOOGLE_API_KEY=your_key_here
GOOGLE_GENAI_USE_VERTEXAI=FALSE
```

## Model selection

Match the model to the task to avoid overspending:

Use `*-latest` aliases per Google docs to avoid pinning to outdated versions:

| Model | Use when |
|---|---|
| `gemini-flash-lite-latest` | Simple classification, routing, yes/no decisions |
| `gemini-flash-latest` | General-purpose tasks, Q&A, tool use, most agents |
| `gemini-pro-latest` | Complex agentic workflows, deep reasoning, long-context |

**Rule of thumb:** default to `gemini-flash-latest`. Only step up to `gemini-pro-latest` for deep multi-step reasoning or very long context.

## Architecture

Each agent lives in its own subdirectory (e.g. `hello_agent/`) and must contain an `agent.py` that exposes a `root_agent` — an instance of `google.adk.agents.Agent`. The ADK CLI discovers agents by looking for this `root_agent` variable.

```
hello_agent/
  agent.py   # defines root_agent = Agent(name=..., model=..., description=..., instruction=...)
```

The `model` field uses Gemini model IDs (e.g. `gemini-2.0-flash`). The `instruction` field is the system prompt. Add new agents by creating a new directory following the same pattern.
