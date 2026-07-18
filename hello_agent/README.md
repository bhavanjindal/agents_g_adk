# Hello Agent

A minimal single-agent example — the starting point for learning Google ADK.
Responds to any question in a friendly but funny tone.

---

## Architecture

```
Agent  (SuperHuman)
  A single LlmAgent with no tools or sub-agents.
  Demonstrates the simplest possible ADK agent structure.
```

---

## How to run

Requires a local Ollama server (`ollama serve`) with the model pulled — this
agent always routes through Ollama, unlike `data_engineering_agent` which can
switch providers via `AGENT_MODEL_PROVIDER`.

```bash
ollama pull qwen3.6:latest   # one-time, ~23GB; override via OLLAMA_MODEL
uv run adk run hello_agent
uv run adk web hello_agent
```

Set `OLLAMA_MODEL` / `OLLAMA_API_BASE` in `.env` (see `.env.example` at the
repo root) to point at a different local model or host.

---

## Example prompts

```
What is the meaning of life?
Explain quantum computing to me.
Why is the sky blue?
```

---

## Key design decisions

This agent is intentionally minimal — its purpose is to show the bare minimum
required to define and run an ADK agent: a directory, an `agent.py`, and a
`root_agent` variable. Everything else in the repo builds on this pattern.
