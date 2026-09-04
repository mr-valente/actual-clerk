from __future__ import annotations

import json

import httpx

from actual_clerk.clients.ntfy import NtfyClient
from actual_clerk.config import Settings


async def test_markdown_is_enabled_in_ntfys_json_publish_format():
    sent: list[dict[str, object]] = []

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "message-1"})

    client = NtfyClient(Settings(ntfy_topic="clerk-test"))
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    try:
        await client.publish(
            title="The Morning Report",
            message="Budget\n\n- Everything is tidy",
            markdown=True,
        )
    finally:
        await client.close()

    assert sent[0]["markdown"] is True
    assert sent[0]["message"] == "Budget\n\n- Everything is tidy"
