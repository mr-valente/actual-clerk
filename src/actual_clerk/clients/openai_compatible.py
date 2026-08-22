"""A minimal OpenAI-compatible chat client for a local model server.

Clerk asks the model exactly one kind of question -- classify this transaction
against these existing categories -- so the client only needs bounded retries,
structured output, and a graceful path for servers that do not implement the
`json_schema` response format.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from actual_clerk.config import Settings

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class ModelError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.openai_base_url.rstrip("/")
        self.model = settings.model
        self.api_key = settings.secret_value("openai_api_key")
        self.max_output_tokens = settings.model_max_output_tokens
        self.max_retries = settings.model_max_retries
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds), headers=headers
        )

    async def close(self) -> None:
        await self.client.aclose()

    @property
    def completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    async def _post(
        self, payload: dict[str, Any], *, allow_format_fallback: bool = False
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        format_fallback_stage = 0
        attempt = 0
        while attempt <= self.max_retries:
            try:
                response = await self.client.post(self.completions_url, json=payload)
                if response.status_code < 400:
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ValueError("response body must be a JSON object")
                    return body
                # Many local servers accept only the looser `json_object` mode,
                # and a few accept neither. Step down before giving up.
                if (
                    allow_format_fallback
                    and response.status_code in {400, 404, 422}
                    and format_fallback_stage < 2
                ):
                    format_fallback_stage += 1
                    payload = dict(payload)
                    if format_fallback_stage == 1:
                        payload["response_format"] = {"type": "json_object"}
                    else:
                        payload.pop("response_format", None)
                    continue
                retryable = response.status_code in {408, 425, 429, 500, 502, 503, 504}
                message = response.text[:1000]
                if not retryable or attempt >= self.max_retries:
                    raise ModelError(
                        f"Model returned {response.status_code}: {message}", retryable=retryable
                    )
                retry_after = response.headers.get("retry-after")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else min(12, 0.75 * (2**attempt))
                )
                await asyncio.sleep(delay)
                attempt += 1
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise ModelError(f"Model request failed: {exc}", retryable=True) from exc
                await asyncio.sleep(min(12, 0.75 * (2**attempt)))
                attempt += 1
            except (ValueError, json.JSONDecodeError) as exc:
                if attempt >= self.max_retries:
                    raise ModelError(
                        f"Model returned invalid response JSON: {exc}", retryable=True
                    ) from exc
                await asyncio.sleep(min(4, 0.5 * (2**attempt)))
                attempt += 1
        raise ModelError(f"Model request failed: {last_error}", retryable=True)

    @staticmethod
    def _content(body: dict[str, Any]) -> str:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(
                "Model response did not contain choices[0].message.content", retryable=True
            ) from exc
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        if not isinstance(content, str) or not content.strip():
            raise ModelError("Model returned empty content", retryable=True)
        return content.strip()

    @staticmethod
    def _finish_reason(body: dict[str, Any]) -> str | None:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None
        reason = choices[0].get("finish_reason")
        return str(reason) if reason is not None else None

    async def structured(
        self, *, name: str, schema: dict[str, Any], system: str, user: str
    ) -> dict[str, Any]:
        schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        instruction = (
            "Return exactly one JSON object matching the following JSON Schema. "
            "Use the property names exactly as written and include no additional properties.\n"
            f"JSON Schema: {schema_json}"
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "messages": [
                {"role": "system", "content": f"{system}\n\n{instruction}"},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            },
        }
        body = await self._post(payload, allow_format_fallback=True)
        if self._finish_reason(body) in {"length", "max_tokens"}:
            raise ModelError("Structured output reached the configured token limit")
        return parse_structured(self._content(body))

    async def test_connection(self) -> dict[str, Any]:
        body = await self._post(
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            }
        )
        return {"ok": True, "model": self.model, "response": self._content(body)[:80]}


def parse_structured(content: str) -> dict[str, Any]:
    """Recover the JSON object from a response a small model decorated."""
    cleaned = _CODE_FENCE.sub("", _THINK_BLOCK.sub("", content)).strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ModelError("Model did not return a JSON object", retryable=True) from None
        try:
            result = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelError(
                f"Model did not return valid structured JSON: {exc}", retryable=True
            ) from exc
    if not isinstance(result, dict):
        raise ModelError("Structured model response must be a JSON object", retryable=True)
    return result
