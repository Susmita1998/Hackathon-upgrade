"""Real MCP transport: the API spawns the three MCP servers as stdio subprocesses and calls them with the official MCP client.

Enable with MCP_TRANSPORT=stdio (default: in-process JSON-RPC gateway). The state-store subprocess and the API share one SQLite file
(STATE_DB), standing in for the shared PostgreSQL of the blueprint.
"""
from __future__ import annotations
import functools, json, os, sys
from contextlib import AsyncExitStack
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .models import ErrorCode, ToolError

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {"hybrid-rag": ["mcp_servers/rag_server.py"], "graphify-vault": ["mcp_servers/graphify_server.py"],
           "state-store": ["mcp_servers/state_store_server.py", "--stdio"]}


def guarded(fn):
    """Server side: turn ToolError / validation errors into a structured result so the client can re-raise the right error code."""
    @functools.wraps(fn)
    def inner(*a, **k):
        try:
            return fn(*a, **k)
        except ToolError as e:
            return {"__error__": e.to_dict()}
        except Exception as e:                                       # pydantic ValidationError etc.
            return {"__error__": {"code": int(ErrorCode.INVALID_PARAMS), "name": "INVALID_PARAMS", "message": str(e)[:300], "data": None}}
    return inner


class RealMcpClients:
    def __init__(self, state_db: str):
        self.state_db, self.sessions, self.stack = state_db, {}, AsyncExitStack()

    async def __aenter__(self):
        env = {**os.environ, "STATE_DB": self.state_db, "LLM_MODE": "off", "PYTHONUNBUFFERED": "1"}
        env.pop("OPENROUTER_API_KEY", None)                             # MCP servers never need the LLM key
        try:
            for name, argv in SCRIPTS.items():
                params = StdioServerParameters(command=sys.executable, args=[str(ROOT / argv[0]), *argv[1:]], env=env, cwd=str(ROOT))
                r, w = await self.stack.enter_async_context(stdio_client(params))
                sess = await self.stack.enter_async_context(ClientSession(r, w))
                await sess.initialize()
                self.sessions[name] = sess
        except BaseException:
            await self.stack.aclose(); raise
        return self

    async def __aexit__(self, *exc):
        await self.stack.aclose()

    async def call(self, server: str, tool: str, tenant: str, args: dict):
        res = await self.sessions[server].call_tool(tool, {"tenant_id": tenant, **args})
        text = res.content[0].text if res.content else "{}"
        if getattr(res, "is_error", getattr(res, "isError", False)):
            raise ToolError(ErrorCode.INVALID_PARAMS, text[:300])
        data = json.loads(text)
        if isinstance(data, dict) and "__error__" in data:
            e = data["__error__"]
            raise ToolError(ErrorCode(e["code"]) if e["code"] in {int(x) for x in ErrorCode} else ErrorCode.INVALID_PARAMS, e["message"], e.get("data"))
        return data
