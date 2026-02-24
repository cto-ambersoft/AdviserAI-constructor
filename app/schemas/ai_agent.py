from pydantic import BaseModel, Field


class AIAgentPrompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=10_000)
    strategy_name: str | None = Field(default=None, max_length=120)


class AIAgentResponse(BaseModel):
    result: str
