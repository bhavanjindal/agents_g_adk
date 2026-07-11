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

```bash
uv run adk run hello_agent
uv run adk web hello_agent
```

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
