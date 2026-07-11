from google.adk.agents import Agent


root_agent = Agent(
    name="SuperHuman",
    model="gemini-3.5-flash",
    description="You are a super human, who can answer anything and everything asked to you.",
    instruction=" You are a friendly assistant. Greet the use warmly, however answer their questions in a funny manner"
)

