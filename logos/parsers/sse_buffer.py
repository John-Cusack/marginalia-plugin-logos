"""SSE stream buffering for Logos study assistant responses."""

from __future__ import annotations

import json
from typing import Any

import httpx


async def buffer_sse(response: httpx.Response) -> str:
    """Buffer an SSE stream into a single concatenated string."""
    parts: list[str] = []
    buffer = ""

    async for chunk in response.aiter_text():
        buffer += chunk
        lines = buffer.split("\n")
        buffer = lines.pop()

        for line in lines:
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    parts.append(data)

    if buffer.startswith("data:"):
        data = buffer[5:].strip()
        if data and data != "[DONE]":
            parts.append(data)

    return "".join(parts)


async def buffer_sse_json(response: httpx.Response) -> list[Any]:
    """Buffer SSE and parse each data line as JSON."""
    results: list[Any] = []
    buffer = ""

    async for chunk in response.aiter_text():
        buffer += chunk
        lines = buffer.split("\n")
        buffer = lines.pop()

        for line in lines:
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    try:
                        results.append(json.loads(data))
                    except json.JSONDecodeError:
                        pass

    return results
