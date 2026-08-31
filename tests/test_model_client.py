from __future__ import annotations

import json

import httpx
import pytest

from actual_clerk.clients.openai_compatible import (
    ModelError,
    OpenAICompatibleClient,
    parse_structured,
)
from actual_clerk.schemas import CATEGORY_CHOICE_SCHEMA, CategoryChoice

ANSWER = {"category_number": 2, "confidence": 0.9, "reason": "Coffee shop", "suggested_new_category": ""}


def completion(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def build(settings, handler):
    client = OpenAICompatibleClient(settings)
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def ask(client):
    return await client.structured(
        name="category_choice", schema=CATEGORY_CHOICE_SCHEMA, system="s", user="u"
    )


def test_the_completions_url_is_derived_once(settings):
    settings.openai_base_url = "http://model:11434/v1"
    assert OpenAICompatibleClient(settings).completions_url == "http://model:11434/v1/chat/completions"
    settings.openai_base_url = "http://model:11434/v1/chat/completions"
    assert OpenAICompatibleClient(settings).completions_url == "http://model:11434/v1/chat/completions"


def test_structured_output_survives_small_model_decoration():
    assert parse_structured('{"a": 1}') == {"a": 1}
    assert parse_structured('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_structured("<think>hmm</think> here: {\"a\": 3}") == {"a": 3}
    assert parse_structured('Sure! {"a": 4} hope that helps') == {"a": 4}
    assert parse_structured('[{"a": 5}]') == {"a": 5}
    assert parse_structured('"{\\"a\\": 6}"') == {"a": 6}


def test_output_that_is_not_an_object_is_refused():
    with pytest.raises(ModelError):
        parse_structured("[1, 2, 3]")
    with pytest.raises(ModelError):
        parse_structured('[{"a": 1}, {"a": 2}]')
    with pytest.raises(ModelError):
        parse_structured("no json here at all")
    with pytest.raises(ModelError):
        parse_structured('{"unclosed": ')


async def test_a_json_schema_request_is_sent_first(settings):
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=completion(json.dumps(ANSWER)))

    client = build(settings, handler)
    try:
        assert await ask(client) == ANSWER
    finally:
        await client.close()
    assert seen["payload"]["response_format"]["type"] == "json_schema"
    assert seen["payload"]["temperature"] == 0
    assert "JSON Schema" in seen["payload"]["messages"][0]["content"]


async def test_a_server_without_json_schema_support_steps_down(settings):
    """llama.cpp and friends accept json_object but not json_schema."""
    attempts = []

    def handler(request):
        payload = json.loads(request.content)
        attempts.append((payload.get("response_format") or {}).get("type"))
        if len(attempts) == 1:
            return httpx.Response(400, text="unsupported response_format")
        return httpx.Response(200, json=completion(json.dumps(ANSWER)))

    client = build(settings, handler)
    try:
        assert await ask(client) == ANSWER
    finally:
        await client.close()
    assert attempts == ["json_schema", "json_object"]


async def test_a_server_with_no_structured_mode_at_all_still_works(settings):
    attempts = []

    def handler(request):
        payload = json.loads(request.content)
        attempts.append((payload.get("response_format") or {}).get("type"))
        if len(attempts) < 3:
            return httpx.Response(422, text="nope")
        return httpx.Response(200, json=completion(json.dumps(ANSWER)))

    client = build(settings, handler)
    try:
        assert await ask(client) == ANSWER
    finally:
        await client.close()
    assert attempts == ["json_schema", "json_object", None]


async def test_a_transient_failure_is_retried(settings):
    settings.model_max_retries = 2
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="warming up")
        return httpx.Response(200, json=completion(json.dumps(ANSWER)))

    client = build(settings, handler)
    try:
        assert await ask(client) == ANSWER
    finally:
        await client.close()
    assert len(calls) == 3


async def test_malformed_structured_content_is_retried(settings):
    settings.model_max_retries = 1
    calls = []

    def handler(request):
        calls.append(1)
        content = "null" if len(calls) == 1 else json.dumps(ANSWER)
        return httpx.Response(200, json=completion(content))

    client = build(settings, handler)
    try:
        assert await ask(client) == ANSWER
    finally:
        await client.close()
    assert len(calls) == 2


async def test_malformed_structured_content_retries_are_bounded(settings):
    settings.model_max_retries = 1
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=completion("null"))

    client = build(settings, handler)
    try:
        with pytest.raises(ModelError, match="must be a JSON object"):
            await ask(client)
    finally:
        await client.close()
    assert len(calls) == 2


async def test_retries_are_bounded(settings):
    settings.model_max_retries = 1
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="still down")

    client = build(settings, handler)
    try:
        with pytest.raises(ModelError) as excinfo:
            await ask(client)
    finally:
        await client.close()
    assert excinfo.value.retryable is True
    assert len(calls) == 2


async def test_a_permanent_error_is_not_retried(settings):
    settings.model_max_retries = 3
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, text="bad key")

    client = build(settings, handler)
    try:
        with pytest.raises(ModelError) as excinfo:
            await ask(client)
    finally:
        await client.close()
    assert excinfo.value.retryable is False
    assert len(calls) == 1


async def test_a_truncated_answer_is_refused_rather_than_half_read(settings):
    client = build(
        settings,
        lambda request: httpx.Response(200, json=completion('{"category_num', "length")),
    )
    try:
        with pytest.raises(ModelError, match="token limit"):
            await ask(client)
    finally:
        await client.close()


async def test_an_empty_answer_is_an_error(settings):
    client = build(settings, lambda request: httpx.Response(200, json=completion("   ")))
    try:
        with pytest.raises(ModelError, match="empty"):
            await ask(client)
    finally:
        await client.close()


async def test_a_response_missing_its_choices_is_an_error(settings):
    client = build(settings, lambda request: httpx.Response(200, json={"nope": True}))
    try:
        with pytest.raises(ModelError, match="choices"):
            await ask(client)
    finally:
        await client.close()


async def test_content_returned_as_parts_is_joined(settings):
    body = {"choices": [{"message": {"content": [{"text": '{"a"'}, {"text": ": 1}"}]}}]}
    client = build(settings, lambda request: httpx.Response(200, json=body))
    try:
        assert await ask(client) == {"a": 1}
    finally:
        await client.close()


async def test_the_connection_test_reports_the_model_that_answered(settings):
    client = build(settings, lambda request: httpx.Response(200, json=completion("OK")))
    try:
        result = await client.test_connection()
    finally:
        await client.close()
    assert result == {"ok": True, "model": "test-model", "response": "OK"}


# ------------------------------------------------------------ output schema


def test_the_choice_schema_rejects_shapes_that_would_mislead():
    assert CategoryChoice.model_validate(ANSWER).category_number == 2
    assert CategoryChoice.model_validate({**ANSWER, "suggested_new_category": None}).suggested_new_category == ""
    assert CategoryChoice.model_validate({**ANSWER, "reason": None}).reason == ""
    # Confidence expressed as a percentage is read as a ratio.
    assert CategoryChoice.model_validate({**ANSWER, "confidence": 85}).confidence == pytest.approx(0.85)
    assert CategoryChoice.model_validate({**ANSWER, "confidence": "nonsense"}).confidence == 0.0
    assert CategoryChoice.model_validate({**ANSWER, "confidence": 1000}).confidence == 1.0
    with pytest.raises(ValueError):
        CategoryChoice.model_validate({**ANSWER, "category_number": -1})
    with pytest.raises(ValueError):
        CategoryChoice.model_validate({**ANSWER, "unexpected": True})


def test_the_schema_asks_for_every_field_it_needs():
    assert CATEGORY_CHOICE_SCHEMA["additionalProperties"] is False
    assert set(CATEGORY_CHOICE_SCHEMA["required"]) == set(CATEGORY_CHOICE_SCHEMA["properties"])
