import os

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

# Local-only: always routes through Ollama via LiteLLM, never Gemini/Anthropic.
# Model must support tool calling if you add tools later — see
# data_engineering_agent/README.md's Ollama note for confirmed model tags.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:latest")
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")

root_agent = Agent(
    name="SuperHuman",
    model=LiteLlm(model=f"ollama_chat/{OLLAMA_MODEL}", api_base=OLLAMA_API_BASE),
    description="You are a super human, who can answer anything and everything asked to you.",
    instruction=" You are a friendly assistant. Greet the use warmly, however answer their questions in a funny manner"
)

