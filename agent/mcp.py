"""MCP layer: JSON-RPC 2.0 servers + gateway with bulkheads, timeouts and fail-fast degradation.

The same server objects back (a) the in-process gateway used by the API (low overhead, still JSON-RPC
serialised on the wire format) and (b) real FastMCP stdio servers in mcp_servers/*.py.
"""
from __future__ import annotations
import asyncio, itertools, json, os, time
from . import config as C
from .models import (ErrorCode, GetFactsIn, HybridRetrieveIn, LookupLotIn, LookupSkuIn, ResolveSubgraphIn, ToolError, UpsertFactIn)
from .rag import RagIndex
from .security import InjectionShield
from .state import StateStore
from .graphify import Graphify


class McpServer:
    name = "server"; pool = "rag"

    def __init__(self):
        self.tools: dict = {}
        self.resources: dict = {}
        self.healthy = True

    def handle(self, req: dict) -> dict:
        """Handle one JSON-RPC 2.0 request dict, return response dict."""
        rid, method, params = req.get("id"), req.get("method"), req.get("params", {})
        try:
            if not self.healthy:
                raise ToolError(ErrorCode.MCP_TIMEOUT, f"{self.name} unavailable")
            if method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": sorted(self.tools)}
            elif method == "tools/call":
                tenant = params.get("_meta", {}).get("tenant_id")
                if not tenant:
                    raise ToolError(ErrorCode.TENANT_MISMATCH, "tenant_id missing in _meta")
                fn = self.tools.get(params["name"])
                if not fn:
                    raise ToolError(ErrorCode.INVALID_PARAMS, f"unknown tool {params['name']}")
                result = {"content": fn(tenant, params.get("arguments", {})), "isError": False}
            elif method == "resources/read":
                result = {"contents": self.resources[params["uri"].split("#")[0].rsplit("/", 1)[0]](params["uri"])}
            else:
                raise ToolError(ErrorCode.INVALID_PARAMS, f"unknown method {method}")
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except ToolError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": int(e.code), "message": e.message, "data": e.data}}
        except Exception as e:                                         # unexpected
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"internal error: {e}"}}


class RagServer(McpServer):
    name, pool = "hybrid-rag", "rag"

    def __init__(self, index: RagIndex, shield: InjectionShield):
        super().__init__()
        self.index, self.shield = index, shield
        self.quarantine_enabled = True                    # indirect-injection chunk quarantine (tests switch it off to probe the LLM prompt hardening)
        self.strict = False                               # runbook: raise cross-encoder cutoff 0.42 -> 0.55
        self.tools["hybrid_retrieve"] = self.hybrid_retrieve
        self.resources["rag://chunks"] = self.read_chunk

    def hybrid_retrieve(self, tenant, args):
        a = HybridRetrieveIn(**args)
        thr = max(a.threshold, C.STRICT_RERANK_THRESHOLD) if self.strict else a.threshold
        quarantined = []

        def ok(ch):
            if self.quarantine_enabled and self.shield.scan_chunk(ch.text).blocked:
                quarantined.append(ch.chunk_id)
                return False
            return True

        chunks, res = self.index.retrieve(a.query, a.top_k_sparse, a.top_k_dense, a.rrf_k, a.rerank_k, thr, chunk_filter=ok)
        return {"chunks": [c.model_dump() for c in chunks], "abstain": not chunks,
                "confidence": chunks[0].score if chunks else 0.0, "stage_ms": res.ms, "quarantined": quarantined,
                "stages": {"bm25": res.bm25[:10], "dense": res.dense[:10], "fused": res.fused[:10], "rerank": res.reranked}}

    def read_chunk(self, uri):
        cid = uri.split("rag://chunks/")[1]
        for c in self.index.chunks:
            if c.chunk_id == cid:
                return {"uri": uri, "text": c.text}
        raise ToolError(ErrorCode.NOT_FOUND, uri)


class StateServer(McpServer):
    name, pool = "state-store", "db"

    def __init__(self, store: StateStore):
        super().__init__()
        self.s = store
        locked = lambda f: (lambda tenant, args: self._locked(f, tenant, args))
        self.tools.update({
            "lookup_sku": locked(lambda t, a: self.s.lookup_sku(t, LookupSkuIn(**a).sku)),
            "lookup_lot": locked(lambda t, a: self.s.lookup_lot(t, LookupLotIn(**a).lot_id)),
            "get_session_facts": locked(lambda t, a: self.s.get_facts(t, GetFactsIn(**a).session_id)),
            "upsert_fact": locked(lambda t, a: (lambda m: self.s.upsert_fact(t, m.session_id, m.key, m.value))(UpsertFactIn(**a))),
            "request_approval": locked(lambda t, a: {"approval_id": self.s.request_approval(t, a["tool"], a["args"], a["requested_by"])}),
            "append_audit": locked(lambda t, a: self.s.audit(t, a["event"], a.get("payload", {})) or {"ok": True}),
        })

    def _locked(self, f, tenant, args):
        self.s.seed_tenant(tenant)
        with self.s.lock:
            return f(tenant, args)


class GraphServer(McpServer):
    name, pool = "graphify-vault", "rag"

    def __init__(self, g: Graphify):
        super().__init__()
        self.g = g
        self.tools["resolve_subgraph"] = lambda tenant, args: (lambda a: self.g.subgraph(a.anchor, a.depth, a.max_tokens))(ResolveSubgraphIn(**args))
        self.resources["vault://notes"] = lambda uri: {"uri": uri, "text": self.g.notes[uri.split("/")[-1]]["body"]}


class McpGateway:
    def __init__(self, servers: list[McpServer], telemetry, verify_manifests: bool = True):
        self.servers = {s.name: s for s in servers}
        self.sem = {k: asyncio.Semaphore(v) for k, v in C.POOLS.items()}   # bulkheads
        self.degraded_until: dict[str, float] = {}
        self.t = telemetry
        self._ids = itertools.count(1)
        self.last_payloads: list[dict] = []
        self.default_timeout = C.MCP_TIMEOUT_S
        self.manifest_status: dict[str, dict] = {}
        self.real = None                      # RealMcpClients when MCP_TRANSPORT=stdio
        if verify_manifests:
            self.verify_manifests()

    def verify_manifests(self) -> dict:
        """Gate 13: signed manifests. A server that does not verify is marked unhealthy and is never called (fail closed)."""
        from .manifest import verify_server
        for n, s in self.servers.items():
            ok, reason = verify_server(s)
            self.manifest_status[n] = {"verified": ok, "reason": reason}
            s.healthy = ok
            if not ok:
                self.t.incr("mcp.manifest_rejected")
        return self.manifest_status

    def status(self):
        now = time.time()
        return [{"server": n, "pool": s.pool, "healthy": s.healthy and self.degraded_until.get(n, 0) < now, "tools": sorted(s.tools),
                 "manifest": self.manifest_status.get(n, {"verified": None, "reason": "not checked"}), "transport": "stdio" if self.real else "in-process"}
                for n, s in self.servers.items()]

    async def call_tool(self, server: str, tool: str, args: dict, tenant: str, trace=None, timeout: float | None = None):
        timeout = self.default_timeout if timeout is None else timeout
        srv = self.servers[server]
        if self.degraded_until.get(server, 0) > time.time():             # tripped bulkhead: fail fast
            raise ToolError(ErrorCode.MCP_TIMEOUT, f"{server} degraded (bulkhead open)")
        t0 = time.perf_counter()
        if self.real is not None:                                       # real MCP client -> stdio subprocess server
            try:
                content = await asyncio.wait_for(self.real.call(server, tool, tenant, args), max(timeout, 1.0))   # IPC + process: allow 1 s
            except asyncio.TimeoutError:
                self.t.incr("mcp.timeouts"); self.degraded_until[server] = time.time() + 2.0
                raise ToolError(ErrorCode.MCP_TIMEOUT, f"{server}.{tool} exceeded the real-transport timeout")
            ms = (time.perf_counter() - t0) * 1e3
            self.t.observe("mcp.real_ms", ms)
            if trace is not None:
                trace.add(f"mcp.{server}.{tool}", ms, transport="stdio")
            return content
        req = {"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
               "params": {"name": tool, "arguments": args, "_meta": {"tenant_id": tenant}}}
        wire = json.dumps(req)
        t1 = time.perf_counter()
        try:
            async with self.sem[srv.pool]:
                raw = await asyncio.wait_for(asyncio.to_thread(lambda: json.dumps(srv.handle(json.loads(wire)))), timeout)
        except asyncio.TimeoutError:
            self.t.incr("mcp.timeouts")
            self.degraded_until[server] = time.time() + 2.0                # trip bulkhead for 2 s
            raise ToolError(ErrorCode.MCP_TIMEOUT, f"{server}.{tool} exceeded {int(timeout*1000)} ms")
        t2 = time.perf_counter()
        resp = json.loads(raw)
        t3 = time.perf_counter()
        self.t.observe("mcp.serde_ms", ((t1 - t0) + (t3 - t2)) * 1e3)      # KPI-30: serialisation overhead
        self.last_payloads = (self.last_payloads + [{"request": req, "response": resp}])[-20:]
        if trace is not None:
            trace.add(f"mcp.{server}.{tool}", (t3 - t0) * 1e3)
        if "error" in resp:
            e = resp["error"]
            raise ToolError(ErrorCode(e["code"]) if e["code"] in set(int(x) for x in ErrorCode) else ErrorCode.INVALID_PARAMS, e["message"], e.get("data"))
        return resp["result"]["content"]
