class AIService:
    def analyze_prompt(self, prompt: str) -> str:
        # Keep AI integration isolated from FastAPI endpoints.
        return f"Stub AI response for prompt length={len(prompt)}"
