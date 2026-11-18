"""MCP server `mcp-graphify-vault` (stdio): resolves an anchor note and returns a depth<=2 wikilink subgraph (<=400 tokens)."""
import _common  # noqa: F401
from mcp.server.mcpserver import MCPServer as FastMCP
from agent.graphify import Graphify
from agent.mcp import GraphServer
from agent.mcp_real import guarded

core = GraphServer(Graphify())
mcp = FastMCP("graphify-vault")


@mcp.tool()
@guarded
def resolve_subgraph(tenant_id: str, anchor: str, depth: int = 2, max_tokens: int = 320) -> dict:
    """Minimal connected subgraph around an anchor node (note id or free text)."""
    return core.tools["resolve_subgraph"](tenant_id, {"anchor": anchor, "depth": depth, "max_tokens": max_tokens})


@mcp.resource("vault://notes/{note_id}")
def note(note_id: str) -> str:
    return core.g.notes[note_id]["body"]


if __name__ == "__main__":
    mcp.run(transport="stdio")
