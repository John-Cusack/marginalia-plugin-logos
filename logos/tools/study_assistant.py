"""logos.study_assistant — AI study assistant (SSE stream)."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client
from logos.parsers.sse_buffer import buffer_sse


@tool(
    id="logos.study_assistant",
    description="Ask the Logos AI Study Assistant a question. Uses the Logos knowledge base and your library to provide scholarly answers.",
    input_schema={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Your question for the study assistant.",
            },
            "conversation_id": {
                "type": "string",
                "description": "Optional conversation ID to continue a previous conversation.",
            },
        },
        "required": ["message"],
    },
)
async def handler(message: str, conversation_id: str | None = None, **kwargs) -> str:
    body: dict = {
        "message": message,
        "settings": {"assistant": "default"},
    }
    if conversation_id:
        body["conversationId"] = conversation_id

    response = await logos_client.post_stream(
        "/api/assistant-api/converseStream", body
    )
    return await buffer_sse(response)
