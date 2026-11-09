"""真实协议探针；无账号时退出 2（UNVERIFIED），不使用 stub 补绿。"""

import argparse
import asyncio
import json
import os
from typing import Literal

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import AsyncOpenAI

from backend.runtime import run_async


async def probe_models(
    client: AsyncOpenAI, chat_model: str, embedding_model: str, *, dashscope: bool = False
) -> dict[str, int]:
    stream = await client.chat.completions.create(
        model=chat_model,
        messages=[{"role": "user", "content": "Reply with OK."}],
        stream=True,
        stream_options={"include_usage": True},
        **(
            {"max_tokens": 16, "extra_body": {"enable_thinking": False}}
            if dashscope
            else {"max_completion_tokens": 16}
        ),
    )
    text = ""
    tokens = 0
    async with stream:
        async for chunk in stream:
            if chunk.choices:
                text += chunk.choices[0].delta.content or ""
            if chunk.usage:
                tokens = chunk.usage.total_tokens
    if not text or not tokens:
        raise ValueError("Chat stream must include text and usage")
    result = await client.embeddings.create(
        model=embedding_model, input=["D01 compatibility probe"]
    )
    if len(result.data) != 1 or not result.data[0].embedding:
        raise ValueError("Embedding response must contain one non-empty vector")
    return {"chatTokens": tokens, "embeddingDimensions": len(result.data[0].embedding)}


async def probe_github_mcp(token: str) -> dict[str, int | str]:
    # The endpoint and read-only mode are fixed; this CLI never accepts arbitrary MCP URLs.
    headers = {
        "Authorization": f"Bearer {token}",
        "X-MCP-Readonly": "true",
        "X-MCP-Toolsets": "issues,pull_requests",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20, connect=15)) as client:
        async with streamable_http_client(
            "https://api.githubcopilot.com/mcp/", http_client=client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listing = await session.list_tools()
                if not listing.tools:
                    raise ValueError("GitHub MCP returned no tools")
                for tool in listing.tools:
                    if not tool.annotations or tool.annotations.readOnlyHint is not True:
                        raise ValueError("A listed tool is not explicitly read-only")
                return {"protocol": initialized.protocolVersion, "listedTools": len(listing.tools)}


def emit(status: Literal["PASS", "FAIL", "UNVERIFIED"], message: str, **details: object) -> None:
    print(json.dumps({"status": status, "message": message, **details}, ensure_ascii=False))


async def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=["models", "mcp"])
    args = parser.parse_args()
    names = (
        [
            "EAH_PROBE_API_KEY",
            "EAH_PROBE_BASE_URL",
            "EAH_PROBE_CHAT_MODEL",
            "EAH_PROBE_EMBEDDING_MODEL",
        ]
        if args.target == "models"
        else ["EAH_PROBE_GITHUB_TOKEN"]
    )
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        emit("UNVERIFIED", "缺少本任务真实服务配置", missing=missing)
        return 2
    try:
        result: object
        async with asyncio.timeout(60):
            if args.target == "models":
                async with AsyncOpenAI(
                    api_key=os.environ["EAH_PROBE_API_KEY"],
                    base_url=os.environ["EAH_PROBE_BASE_URL"],
                    timeout=20,
                    max_retries=0,
                ) as client:
                    result = await probe_models(
                        client,
                        os.environ["EAH_PROBE_CHAT_MODEL"],
                        os.environ["EAH_PROBE_EMBEDDING_MODEL"],
                        dashscope=os.getenv("EAH_PROBE_CHAT_PROTOCOL") == "dashscope-chat",
                    )
            else:
                result = await probe_github_mcp(os.environ["EAH_PROBE_GITHUB_TOKEN"])
        emit("PASS", "真实协议探针完成；不等同于 D03/D13 业务验收", result=result)
        return 0
    except Exception as error:
        emit("FAIL", "真实协议探针失败", errorType=type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_async(main()))
