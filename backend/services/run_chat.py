"""Bounded Chat Completions SSE to schema-validated NDJSON blocks."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import tiktoken
from dashscope import get_tokenizer
from pydantic import ValidationError

from backend.models.model_settings import ConnectionVersion, ModelProfile
from backend.repositories.runs import fail
from backend.run_schemas import ResultBlock
from backend.services.model_probe import Attempt, ModelProbe, WireUsage


class Chat:
    def __init__(self, probe: ModelProbe):
        self.probe = probe
        self.request_id: str | None = None
        self.output_started = False
        self.retry_after: float | None = None

    async def input_tokens(self, profile: ModelProfile, messages: list[dict[str, str]]) -> int:
        encoded = json.dumps(messages, ensure_ascii=False)
        tokenizer = (
            await asyncio.to_thread(get_tokenizer, profile.model_name)
            if profile.provider_protocol == "dashscope-chat"
            else await asyncio.to_thread(
                tiktoken.get_encoding, profile.tokenizer_id or "o200k_base"
            )
        )
        tokens = len(tokenizer.encode(encoded))
        # Tokenizer counts plus a conservative framing bound. Actual provider usage settles it.
        return max(tokens + 32 * len(messages) + 64, len(encoded.encode()) + 64)

    async def generate(
        self,
        profile: ModelProfile,
        version: ConnectionVersion,
        messages: list[dict[str, str]],
        output: int,
        usage: Attempt,
        emit: Callable[[str], Awaitable[None]],
    ) -> list[ResultBlock]:
        self.request_id, self.output_started, self.retry_after = None, False, None
        if version.base_url not in self.probe.settings.model_base_urls.get(
            version.provider_protocol, []
        ):
            raise fail("MODEL_URL_DENIED")
        await self.probe.destination(version.base_url)
        payload: dict[str, Any] = {
            "model": profile.model_name,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
            "max_completion_tokens": output,
        }
        if profile.provider_protocol == "dashscope-chat":
            payload.pop("max_completion_tokens")
            payload.update(max_tokens=output, enable_thinking=False)
        blocks: list[ResultBlock] = []
        wire = b""
        candidate = ""
        total = 0
        done = finished = False

        async def decode(line: str) -> None:
            if not line.strip():
                return
            try:
                block = ResultBlock.model_validate_json(line)
            except (ValueError, ValidationError):
                raise fail("RESULT_INVALID") from None
            blocks.append(block)
            if len(blocks) > 64:
                raise fail("RESULT_INVALID")
            await emit(block.text + "\n")

        try:
            async with httpx.AsyncClient(
                transport=self.probe.transport, trust_env=False, follow_redirects=False, timeout=30
            ) as client:
                async with client.stream(
                    "POST",
                    version.base_url + "/chat/completions",
                    headers={"Authorization": f"Bearer {version.credential_value}"},
                    json=payload,
                ) as response:
                    self.request_id = response.headers.get("x-request-id")
                    if response.status_code != 200:
                        retry = response.headers.get("retry-after", "")
                        if retry.isdigit():
                            self.retry_after = min(float(retry), 30)
                        raise fail(
                            "MODEL_AUTH_FAILED"
                            if response.status_code in {401, 403}
                            else "MODEL_RATE_LIMITED"
                            if response.status_code == 429
                            else "MODEL_REQUEST_INVALID"
                            if 400 <= response.status_code < 500
                            else "MODEL_UNAVAILABLE",
                            503,
                        )
                    async for data in response.aiter_bytes():
                        wire += data
                        total += len(data)
                        if len(wire) > 65536 or total > 2 * 1024 * 1024:
                            raise fail("RESULT_INVALID")
                        while b"\n" in wire:
                            line, wire = wire.split(b"\n", 1)
                            if not line.startswith(b"data:"):
                                continue
                            content = line[5:].strip()
                            if content == b"[DONE]":
                                done = True
                                continue
                            chunk = json.loads(content)
                            self.request_id = chunk.get("id", self.request_id)
                            if chunk.get("usage") is not None:
                                actual = WireUsage.model_validate(chunk["usage"])
                                usage.input_tokens, usage.output_tokens, usage.token_source = (
                                    actual.prompt_tokens,
                                    actual.completion_tokens,
                                    "actual",
                                )
                                if (
                                    actual.prompt_tokens + actual.completion_tokens > usage.reserved
                                    or actual.completion_tokens > output
                                ):
                                    raise fail("MODEL_BUDGET_INVALID")
                            for choice in chunk.get("choices", []):
                                if choice.get("index") != 0:
                                    raise fail("RESULT_INVALID")
                                if choice.get("finish_reason"):
                                    if choice["finish_reason"] != "stop":
                                        raise fail("RESULT_INVALID")
                                    finished = True
                                delta = choice.get("delta", {})
                                self.output_started |= bool(delta.get("content"))
                                if delta.get("tool_calls") or delta.get("refusal"):
                                    raise fail("RESULT_INVALID")
                                candidate += delta.get("content") or ""
                                if len(candidate.encode()) > 65536:
                                    raise fail("RESULT_INVALID")
                                while "\n" in candidate:
                                    result_line, candidate = candidate.split("\n", 1)
                                    await decode(result_line)
                    if not done or not finished:
                        raise fail("MODEL_STREAM_INTERRUPTED")
                    await decode(candidate)
                    if not blocks:
                        raise fail("RESULT_INVALID")
                    return blocks
        except (httpx.HTTPError, OSError):
            raise fail(
                "MODEL_STREAM_INTERRUPTED" if self.output_started else "MODEL_UNAVAILABLE", 503
            ) from None
        except (ValueError, ValidationError, TypeError, AttributeError, KeyError):
            raise fail("MODEL_RESPONSE_INVALID") from None
