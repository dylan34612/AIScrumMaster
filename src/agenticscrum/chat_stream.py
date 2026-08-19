"""Minimal SSE helpers for chat progress (stretch polish)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator


async def chat_status_event_stream(
    *,
    messages: list[str],
    delay_s: float = 0.4,
) -> AsyncIterator[str]:
    """Yield SSE-formatted status lines for staged chat progress UX."""

    for message in messages:
        payload = json.dumps({"type": "status", "message": message})
        yield f"data: {payload}\n\n"
        await asyncio.sleep(delay_s)
    yield f"data: {json.dumps({'type': 'done'})}\n\n"
