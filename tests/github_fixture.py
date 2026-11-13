"""Synthetic MCP JSON-RPC server fixture, NOT captured GitHub production evidence."""

import json
from typing import Any

import httpx

from backend.services.github_mcp import REMOTE_TOOLS


class GitHubFixture:
    def __init__(self) -> None:
        self.mode = "ok"
        self.requests: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.githubcopilot.com/mcp/"
        assert request.headers["x-mcp-readonly"] == "true"
        assert request.headers["x-mcp-tools"] == ",".join(REMOTE_TOOLS)
        if request.headers.get("authorization", "").endswith("synthetic-invalid"):
            return httpx.Response(401)
        if self.mode in {"auth", "scope", "rate", "redirect"}:
            return httpx.Response(
                {"auth": 401, "scope": 403, "rate": 429, "redirect": 307}[self.mode],
                headers={"location": "https://attacker.invalid"},
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        if request.method == "GET":
            return httpx.Response(405)
        data = json.loads(request.content)
        self.requests.append(data)
        method = data["method"]
        if "id" not in data:
            return httpx.Response(202)
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-d13", "version": "1"},
            }
        elif method == "tools/list":
            tools = []
            for name in REMOTE_TOOLS:
                props = (
                    {
                        "query": {"type": "string"},
                        "page": {"type": "number"},
                        "perPage": {"type": "number"},
                    }
                    if name.startswith("search_")
                    else {
                        "method": {"type": "string", "enum": ["get", "get_comments"]},
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "issue_number" if name == "issue_read" else "pullNumber": {
                            "type": "number"
                        },
                    }
                )
                if self.mode == "schema":
                    props["extra"] = {"type": "string"}
                required = ["query"] if name.startswith("search_") else list(props)
                tools.append(
                    {
                        "name": name,
                        "inputSchema": {
                            "type": "object",
                            "properties": props,
                            "required": required,
                        },
                        "annotations": {"readOnlyHint": self.mode != "write"},
                    }
                )
            tools.append(
                {
                    "name": "create_issue",
                    "inputSchema": {"type": "object"},
                    "annotations": {"readOnlyHint": False},
                }
            )
            result = {"tools": tools}
        elif method == "tools/call":
            name = data["params"]["name"]
            args = data["params"]["arguments"]
            value: dict[str, Any]
            if name == "search_repositories":
                full = args["query"].removeprefix("repo:")
                value = {
                    "items": []
                    if self.mode == "empty"
                    else [
                        {"id": 123, "full_name": "other/repo" if self.mode == "different" else full}
                    ]
                }
            elif name.startswith("search_"):
                value = {
                    "items": self.items,
                    "total_count": len(self.items),
                    "incomplete_results": False,
                }
            else:
                assert args["method"] == "get"
                value = self.items[0]
            result = {
                "content": [{"type": "text", "text": json.dumps(value)}],
                "isError": self.mode == "call_error",
            }
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": data["id"], "result": result})
