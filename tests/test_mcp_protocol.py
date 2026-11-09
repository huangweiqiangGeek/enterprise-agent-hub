import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ToolAnnotations


@pytest.mark.asyncio
async def test_streamable_http_initialize_list_and_read_tool() -> None:
    server = FastMCP("D01 local fixture", stateless_http=True, json_response=True)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def read_probe() -> str:
        return "fixture-only"

    app = server.streamable_http_app()

    async with (
        server.session_manager.run(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=5) as client,
    ):
        async with streamable_http_client("http://localhost:8000/mcp", http_client=client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listing = await session.list_tools()
                assert initialized.protocolVersion
                assert [tool.name for tool in listing.tools] == ["read_probe"]
                result = await session.call_tool("read_probe", {})
                assert result.isError is False
                assert isinstance(result.content[0], TextContent)
                assert result.content[0].text == "fixture-only"
