"""MCP server `mcp-state-store`: streamable-http on :8081/mcp (python mcp_servers/state_store_server.py) or stdio (--stdio).

Read tools only plus request_approval/append_audit. Mutations are NOT exposed: they execute solely through the HITL approval path.
"""
import os, sys
import _common  # noqa: F401
from mcp.server.mcpserver import MCPServer as FastMCP
from agent.mcp import StateServer
from agent.mcp_real import guarded
from agent.state import StateStore

core = StateServer(StateStore(os.environ.get("STATE_DB", "state.db")))
mcp = FastMCP("state-store")


def _call(_name, _tenant, **kw):
    return core.tools[_name](_tenant, kw)


@mcp.tool()
@guarded
def lookup_sku(tenant_id: str, sku: str) -> dict: return _call("lookup_sku", tenant_id, sku=sku)
@mcp.tool()
@guarded
def lookup_lot(tenant_id: str, lot_id: str) -> dict: return _call("lookup_lot", tenant_id, lot_id=lot_id)
@mcp.tool()
@guarded
def get_session_facts(tenant_id: str, session_id: str) -> dict: return _call("get_session_facts", tenant_id, session_id=session_id)
@mcp.tool()
@guarded
def upsert_fact(tenant_id: str, session_id: str, key: str, value: str) -> dict: return _call("upsert_fact", tenant_id, session_id=session_id, key=key, value=value)
@mcp.tool()
@guarded
def request_approval(tenant_id: str, tool: str, args: dict, requested_by: str) -> dict: return _call("request_approval", tenant_id, tool=tool, args=args, requested_by=requested_by)
@mcp.tool()
@guarded
def append_audit(tenant_id: str, event: str, payload: dict) -> dict: return _call("append_audit", tenant_id, event=event, payload=payload)


if __name__ == "__main__":
    mcp.run(transport="stdio" if "--stdio" in sys.argv else "streamable-http", host="127.0.0.1", port=8081)
