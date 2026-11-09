"""Bailian wire contracts, not a substitute for separately invoked real-provider probes."""

import json
from typing import Any

import httpx
import pytest
from model_fixture import ProviderFixture

from backend.config import Settings
from backend.model_schemas import ModelConfigWrite
from backend.services.errors import AppError
from backend.services.model_probe import Attempt, ModelProbe


def candidate(protocol: str, model: str) -> ModelConfigWrite:
    return ModelConfigWrite.model_validate(
        {
            "providerProtocol": protocol,
            "baseUrl": Settings().model_base_urls[protocol][0],
            "modelName": model,
            "credential": "fixture-only-secret",
            "expectedRevision": 0,
        }
    )


async def record(_attempt: Attempt) -> None:
    return None


@pytest.mark.asyncio
async def test_qwen_tools_schema_tokenizer_and_output_parameters() -> None:
    fixture = ProviderFixture()

    async def respond(request: httpx.Request) -> httpx.Response:
        response = await fixture.handle(request)
        # Real DashScope forced tool streams use stop, unlike the OpenAI fixture.
        return httpx.Response(
            response.status_code,
            content=response.content.replace(
                b'"finish_reason": "tool_calls"', b'"finish_reason": "stop"'
            ),
            headers=response.headers,
        )

    probe = ModelProbe(Settings(), httpx.MockTransport(respond))
    outcome = await probe.test("chat", candidate("dashscope-chat", "qwen-plus"), record)
    assert outcome.result.ok
    assert outcome.tokenizer == "dashscope-qwen-1.26.7"
    assert outcome.capabilities["tokenizer"]
    assert len(fixture.requests) == 2
    for request in fixture.requests:
        assert request["max_tokens"] == 64 and request["enable_thinking"] is False
        assert "max_completion_tokens" not in request
    assert fixture.requests[0]["tools"][0]["function"]["strict"]
    assert fixture.requests[1]["response_format"]["type"] == "json_schema"
    fixture.mode = "truncated"
    failed = await probe.test("chat", candidate("dashscope-chat", "qwen-plus"), record)
    assert not failed.result.ok
    assert failed.result.usage[0].status == "failed"


@pytest.mark.parametrize("mode", ["ok", "index", "score", "order", "billing", "auth"])
@pytest.mark.asyncio
async def test_dashscope_rerank_shape_billing_and_failures(mode: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/services/rerank/text-rerank/text-rerank"
        assert json.loads(request.content) == {
            "model": "gte-rerank-v2",
            "input": {"query": "model probe", "documents": ["model probe", "unrelated"]},
            "parameters": {"top_n": 2, "return_documents": False},
        }
        if mode == "auth":
            return httpx.Response(401, json={"message": "fixture-only-secret"})
        results: list[dict[str, Any]] = [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.1},
        ]
        if mode == "index":
            results[1]["index"] = 0
        if mode == "score":
            results[0]["relevance_score"] = 2
        if mode == "order":
            results.reverse()
        return httpx.Response(
            200,
            json={
                "output": {"results": results},
                "usage": {"total_tokens": -1 if mode == "billing" else 19},
            },
        )

    outcome = await ModelProbe(Settings(), httpx.MockTransport(respond)).test(
        "rerank", candidate("dashscope-rerank", "gte-rerank-v2"), record
    )
    assert outcome.result.ok == (mode == "ok")
    assert "fixture-only-secret" not in outcome.result.model_dump_json()
    if mode == "ok":
        assert outcome.result.usage[0].billed_units == {"total_tokens": 19}
        assert outcome.result.usage[0].input_tokens is None
    else:
        assert outcome.result.usage[0].status == "failed"


def test_protocol_and_tokenizer_cannot_be_mixed() -> None:
    probe = ModelProbe(Settings())
    with pytest.raises(AppError, match="tokenizer"):
        probe.validate("chat", candidate("openai-chat", "qwen-plus"))
    with pytest.raises(AppError):
        probe.validate("chat", candidate("dashscope-rerank", "qwen-plus"))
    assert probe.chat_models("dashscope-chat") == ["qwen-plus"]


@pytest.mark.asyncio
async def test_private_dns_remains_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(2, 1, 6, "", ("198.18.2.14", 443))]),
    )
    outcome = await ModelProbe(Settings()).test(
        "chat", candidate("dashscope-chat", "qwen-plus"), record
    )
    assert outcome.result.error_code == "MODEL_URL_DENIED"
    assert outcome.result.usage == []
