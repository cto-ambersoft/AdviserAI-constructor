from fastapi import APIRouter, WebSocket

from app.schemas.ai_agent import AIAgentPrompt, AIAgentResponse
from app.services.ai.client import AIService

router = APIRouter()
ai_service = AIService()


@router.post("/analyze", response_model=AIAgentResponse, summary="Analyze strategy prompt")
async def analyze_with_ai(payload: AIAgentPrompt) -> AIAgentResponse:
    result = ai_service.analyze_prompt(payload.prompt)
    return AIAgentResponse(result=result)


@router.websocket("/ws/prices")
async def stream_prices(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"event": "connected", "message": "price stream placeholder"})
    await websocket.close()
