"""MCP server `mcp-hybrid-rag` (stdio): hybrid_retrieve tool + rag://chunks/{id} resource. 3-stage RAG in agent/rag.py."""
import _common  # noqa: F401
from mcp.server.mcpserver import MCPServer as FastMCP
from agent import config as C
from agent.mcp import RagServer
from agent.rag import RagIndex, load_vault
from agent.mcp_real import guarded
from agent.security import InjectionShield

core = RagServer(RagIndex(load_vault(C.VAULT_DIR)), InjectionShield())
mcp = FastMCP("hybrid-rag")


@mcp.tool()
@guarded
def hybrid_retrieve(tenant_id: str, query: str, top_k_sparse: int = 50, top_k_dense: int = 50, rrf_k: int = 60,
                    rerank_k: int = 4, threshold: float = 0.42) -> dict:
    """BM25 + bi-encoder -> RRF -> cross-encoder. Returns <=4 verified chunks, or abstain=true below threshold."""
    return core.hybrid_retrieve(tenant_id, dict(query=query, top_k_sparse=top_k_sparse, top_k_dense=top_k_dense,
                                                rrf_k=rrf_k, rerank_k=rerank_k, threshold=threshold))


@mcp.resource("rag://chunks/{chunk_id}")
def chunk(chunk_id: str) -> str:
    return core.read_chunk(f"rag://chunks/{chunk_id}")["text"]


if __name__ == "__main__":
    mcp.run(transport="stdio")
