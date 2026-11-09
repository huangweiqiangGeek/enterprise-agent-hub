"""Bounded protocol probes. Provider content and errors never become public diagnostics."""

import asyncio
import ipaddress
import json
import math
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import tiktoken
from dashscope import get_tokenizer
from pydantic import BaseModel, Field, ValidationError

from backend.config import Settings
from backend.model_schemas import ModelConfigWrite, ModelKind, TestResult, TestUsage
from backend.models.identity import now
from backend.services.errors import AppError

PROTOCOLS = {
    "chat": ("openai-chat", "dashscope-chat"),
    "embedding": ("openai-embeddings",),
    "rerank": ("cohere-rerank-v2", "dashscope-rerank"),
}
CHAT_TOKENIZERS = {
    "openai-chat": {"o200k_base", "cl100k_base"},
    "dashscope-chat": {"dashscope-qwen-1.26.7"},
}
OK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class WireUsage(BaseModel):
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(default=0, ge=0, strict=True)


class ProbeFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


@dataclass
class Attempt:
    number: int
    id: UUID = field(default_factory=uuid4)
    reserved: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    token_source: Literal["actual", "reserved", "unavailable"] = "unavailable"
    billed_units: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0
    status: str = "pending"

    def dto(self) -> TestUsage:
        return TestUsage(
            attempt=self.number,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            reserved_tokens=self.reserved,
            token_source=self.token_source,
            billed_units=self.billed_units,
            status=self.status,
        )


@dataclass
class ProbeOutcome:
    result: TestResult
    dimension: int | None = None
    tokenizer: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)


class ModelProbe:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        # Tests inject a transport; no HTTP field can bypass destination rules.
        self.transport = transport

    def validate(self, kind: ModelKind, value: ModelConfigWrite) -> str | None:
        if value.provider_protocol not in PROTOCOLS[kind]:
            raise AppError(422, "MODEL_PROTOCOL_INVALID", "此模型类型不支持所选协议。")
        if value.base_url not in self.settings.model_base_urls.get(value.provider_protocol, []):
            raise AppError(422, "MODEL_URL_DENIED", "该地址未获部署管理员允许。")
        if kind != "embedding" and value.embedding_dimension is not None:
            raise AppError(422, "MODEL_DIMENSION_INVALID", "只有 Embedding 可设置向量维度。")
        tokenizer = self.settings.chat_tokenizers.get(value.model_name) if kind == "chat" else None
        if kind == "chat" and value.model_name not in self.chat_models(value.provider_protocol):
            raise AppError(
                422, "MODEL_TOKENIZER_UNSUPPORTED", "此聊天模型尚未配置受支持的 tokenizer。"
            )
        return tokenizer

    def chat_models(self, protocol: str) -> list[str]:
        return [
            model
            for model, tokenizer in self.settings.chat_tokenizers.items()
            if tokenizer in CHAT_TOKENIZERS.get(protocol, set())
            and (protocol != "dashscope-chat" or model.startswith("qwen"))
        ]

    async def destination(self, url: str) -> None:
        if self.transport is not None:
            return
        host = urlsplit(url).hostname
        if not host:
            raise ProbeFailure("MODEL_URL_DENIED")
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ProbeFailure("MODEL_URL_DENIED")

    @staticmethod
    def usage(value: Any, attempt: Attempt) -> None:
        if value is None:
            return
        usage = WireUsage.model_validate(value)
        attempt.input_tokens = usage.prompt_tokens
        attempt.output_tokens = usage.completion_tokens
        attempt.token_source = "actual"
        if attempt.reserved and usage.prompt_tokens + usage.completion_tokens > attempt.reserved:
            raise ProbeFailure("MODEL_BUDGET_INVALID")
        if usage.completion_tokens > 64:
            raise ProbeFailure("MODEL_OUTPUT_LIMIT_UNSUPPORTED")

    async def request(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: dict[str, Any],
        attempt: Attempt,
        *,
        stream: bool = False,
        tool_stop: bool = False,
    ) -> Any:
        async with client.stream("POST", path, json=payload) as response:
            if response.status_code in {401, 403}:
                raise ProbeFailure("MODEL_AUTH_FAILED")
            if response.status_code == 429:
                raise ProbeFailure("MODEL_RATE_LIMITED")
            if response.status_code != 200:
                raise ProbeFailure("MODEL_PROVIDER_ERROR")
            if stream:
                name, arguments, finished, done = "", "", False, False
                buffer = b""
                total = 0
                async for block in response.aiter_bytes():
                    total += len(block)
                    buffer += block
                    if total > 65536 or len(buffer) > 65536:
                        raise ProbeFailure("MODEL_RESPONSE_INVALID")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            done = True
                            continue
                        chunk = json.loads(data)
                        self.usage(chunk.get("usage"), attempt)
                        for choice in chunk.get("choices", []):
                            if choice.get("index") != 0:
                                raise ProbeFailure("MODEL_RESPONSE_INVALID")
                            # DashScope forced function calls finish with stop; still require
                            # the exact streamed function/arguments and final DONE below.
                            if choice.get("finish_reason") == "tool_calls" or (
                                tool_stop and choice.get("finish_reason") == "stop"
                            ):
                                finished = True
                            delta = choice.get("delta", {})
                            if delta.get("refusal"):
                                raise ProbeFailure("MODEL_CAPABILITY_UNSUPPORTED")
                            for call in delta.get("tool_calls", []):
                                if call.get("index") != 0:
                                    raise ProbeFailure("MODEL_RESPONSE_INVALID")
                                function = call.get("function", {})
                                name += function.get("name") or ""
                                arguments += function.get("arguments") or ""
                if (
                    not done
                    or not finished
                    or name != "eah_probe"
                    or not self.valid_ok(json.loads(arguments))
                ):
                    raise ProbeFailure("MODEL_CAPABILITY_UNSUPPORTED")
                return None
            body = bytearray()
            async for block in response.aiter_bytes():
                body.extend(block)
                if len(body) > 2 * 1024 * 1024:
                    raise ProbeFailure("MODEL_RESPONSE_INVALID")
            return json.loads(body)

    async def test(
        self,
        kind: ModelKind,
        value: ModelConfigWrite,
        record: Callable[[Attempt], Awaitable[None]],
    ) -> ProbeOutcome:
        tokenizer = self.validate(kind, value)
        started = monotonic()
        attempts: list[Attempt] = []
        dimension: int | None = None
        capabilities: dict[str, bool] = {}
        error: str | None = None
        try:
            async with asyncio.timeout(30):
                await self.destination(value.base_url)
                if tokenizer:
                    # The official SDK bundles Qwen's vocabulary; no arbitrary model code loads.
                    encoding = (
                        await asyncio.to_thread(get_tokenizer, value.model_name)
                        if value.provider_protocol == "dashscope-chat"
                        else await asyncio.to_thread(tiktoken.get_encoding, tokenizer)
                    )
                    if encoding.decode(encoding.encode("研发 model probe")) != "研发 model probe":
                        raise ProbeFailure("MODEL_TOKENIZER_UNSUPPORTED")
                credential = value.credential.get_secret_value() if value.credential else ""
                async with httpx.AsyncClient(
                    base_url=value.base_url + "/",
                    headers={"Authorization": f"Bearer {credential}"},
                    timeout=httpx.Timeout(25, connect=10),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    calls = 2 if kind == "chat" else 1
                    for index in range(calls):
                        payload: dict[str, Any] = {"model": value.model_name}
                        if kind == "chat":
                            payload.update(
                                {
                                    "messages": [
                                        {
                                            "role": "user",
                                            "content": 'Return {"ok":true}.',
                                        }
                                    ],
                                    "max_completion_tokens": 64,
                                }
                            )
                            if value.provider_protocol == "dashscope-chat":
                                # qwen-plus requires max_tokens and non-thinking mode.
                                payload.pop("max_completion_tokens")
                                payload.update({"max_tokens": 64, "enable_thinking": False})
                            if index == 0:
                                payload.update(
                                    {
                                        "stream": True,
                                        "stream_options": {"include_usage": True},
                                        "tools": [
                                            {
                                                "type": "function",
                                                "function": {
                                                    "name": "eah_probe",
                                                    "parameters": OK_SCHEMA,
                                                    "strict": True,
                                                },
                                            }
                                        ],
                                        "tool_choice": {
                                            "type": "function",
                                            "function": {"name": "eah_probe"},
                                        },
                                        "parallel_tool_calls": False,
                                    }
                                )
                            else:
                                payload["response_format"] = {
                                    "type": "json_schema",
                                    "json_schema": {
                                        "name": "eah_probe",
                                        "schema": OK_SCHEMA,
                                        "strict": True,
                                    },
                                }
                        elif kind == "embedding":
                            payload.update({"input": ["model probe"], "encoding_format": "float"})
                            if value.embedding_dimension is not None:
                                payload["dimensions"] = value.embedding_dimension
                        else:
                            payload.update(
                                {
                                    "query": "model probe",
                                    "documents": ["model probe", "unrelated"],
                                    "top_n": 2,
                                }
                            )
                            if value.provider_protocol == "dashscope-rerank":
                                payload = {
                                    "model": value.model_name,
                                    "input": {
                                        "query": payload["query"],
                                        "documents": payload["documents"],
                                    },
                                    "parameters": {"top_n": 2, "return_documents": False},
                                }
                        # Fixed, tiny probe input: UTF-8 byte length plus framing/output headroom
                        # is a conservative reservation, never labelled actual provider usage.
                        reserved = len(json.dumps(payload).encode()) + 512 if kind == "chat" else 0
                        attempt = Attempt(
                            index + 1,
                            reserved=reserved,
                            token_source="reserved" if reserved else "unavailable",
                        )
                        attempts.append(attempt)
                        await record(attempt)
                        call_started = monotonic()
                        try:
                            path = {
                                "chat": "chat/completions",
                                "embedding": "embeddings",
                                "rerank": "rerank",
                            }[kind]
                            if value.provider_protocol == "dashscope-rerank":
                                path = "services/rerank/text-rerank/text-rerank"
                            data = await self.request(
                                client,
                                path,
                                payload,
                                attempt,
                                stream=kind == "chat" and index == 0,
                                tool_stop=value.provider_protocol == "dashscope-chat",
                            )
                            if kind == "chat" and index == 1:
                                self.usage(data.get("usage"), attempt)
                                choice = data["choices"][0]
                                if (
                                    choice.get("finish_reason") != "stop"
                                    or choice["message"].get("refusal")
                                    or not self.valid_ok(json.loads(choice["message"]["content"]))
                                ):
                                    raise ProbeFailure("MODEL_CAPABILITY_UNSUPPORTED")
                            elif kind == "embedding":
                                self.usage(data.get("usage"), attempt)
                                vectors = data["data"]
                                vector = vectors[0]["embedding"]
                                if (
                                    len(vectors) != 1
                                    or vectors[0]["index"] != 0
                                    or not isinstance(vector, list)
                                    or not 1 <= len(vector) <= 65536
                                ):
                                    raise ProbeFailure("MODEL_RESPONSE_INVALID")
                                if any(
                                    type(n) not in {int, float} or not math.isfinite(n)
                                    for n in vector
                                ):
                                    raise ProbeFailure("MODEL_RESPONSE_INVALID")
                                dimension = len(vector)
                                if (
                                    value.embedding_dimension is not None
                                    and dimension != value.embedding_dimension
                                ):
                                    raise ProbeFailure("MODEL_DIMENSION_MISMATCH")
                            elif kind == "rerank":
                                dashscope = value.provider_protocol == "dashscope-rerank"
                                results = (
                                    data["output"]["results"] if dashscope else data["results"]
                                )
                                if len(results) != 2 or {row["index"] for row in results} != {0, 1}:
                                    raise ProbeFailure("MODEL_RESPONSE_INVALID")
                                if any(
                                    type(row["index"]) is not int
                                    or type(row["relevance_score"]) not in {int, float}
                                    or not math.isfinite(row["relevance_score"])
                                    or not 0 <= row["relevance_score"] <= 1
                                    for row in results
                                ):
                                    raise ProbeFailure("MODEL_RESPONSE_INVALID")
                                if results[0]["relevance_score"] < results[1]["relevance_score"]:
                                    raise ProbeFailure("MODEL_RESPONSE_INVALID")
                                units = (
                                    data.get("usage", {})
                                    if dashscope
                                    else data.get("meta", {}).get("billed_units", {})
                                )
                                # Provider rerank billing is distinct from chat input/output usage.
                                allowed_units = (
                                    {"total_tokens"}
                                    if dashscope
                                    else {"search_units", "input_tokens"}
                                )
                                if any(
                                    type(v) is not int or v < 0
                                    for k, v in units.items()
                                    if k in allowed_units
                                ):
                                    raise ProbeFailure("MODEL_RESPONSE_INVALID")
                                attempt.billed_units = {
                                    k: v for k, v in units.items() if k in allowed_units
                                }
                            attempt.status = "succeeded"
                        finally:
                            if attempt.status == "pending":
                                attempt.status = "failed"
                            attempt.duration_ms = int((monotonic() - call_started) * 1000)
                            await asyncio.shield(record(attempt))
                capabilities = (
                    {
                        "stream": True,
                        "toolCalls": True,
                        "structuredOutput": True,
                        "outputLimit": True,
                        "tokenizer": True,
                    }
                    if kind == "chat"
                    else {"dimensionsVerified": True}
                    if kind == "embedding"
                    else {"rankingVerified": True}
                )
        except ProbeFailure as exc:
            error = exc.code
        except (TimeoutError, httpx.TimeoutException):
            error = "MODEL_TIMEOUT"
        except (httpx.HTTPError, OSError):
            error = "MODEL_UNAVAILABLE"
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ValidationError):
            error = "MODEL_RESPONSE_INVALID"
        return ProbeOutcome(
            TestResult(
                ok=error is None,
                tested_at=now(),
                provider=value.provider_protocol,
                model_name=value.model_name,
                latency_ms=int((monotonic() - started) * 1000),
                usage=[a.dto() for a in attempts],
                error_code=error,
                message="能力测试通过。"
                if error is None
                else "测试未通过，当前生效配置未被替换。请检查地址、凭据和模型能力。",
            ),
            dimension=dimension,
            tokenizer=tokenizer,
            capabilities=capabilities,
        )

    @staticmethod
    def valid_ok(value: Any) -> bool:
        return isinstance(value, dict) and set(value) == {"ok"} and value["ok"] is True
