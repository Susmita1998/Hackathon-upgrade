"""FastAPI surface: /api/v1/agent/query plus HITL, plugin, breaker, MCP and metrics endpoints."""
from __future__ import annotations
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from .models import ErrorCode, QueryRequest, ToolError
from .service import AgentService


class Decision(BaseModel):
    approve: bool
    reviewer: str


def create_app(service: AgentService | None = None) -> FastAPI:
    transport = os.environ.get("MCP_TRANSPORT", "inproc").lower()          # inproc | stdio (real MCP subprocess servers)
    if service is None:
        db = os.environ.get("STATE_DB") or (str(Path(__file__).resolve().parent.parent / "state.db") if transport == "stdio" else ":memory:")
        os.environ["STATE_DB"] = db if db != ":memory:" else ""
        svc = AgentService(state_path=db)
    else:
        svc = service

    @asynccontextmanager
    async def lifespan(app):
        if transport == "stdio":
            from .mcp_real import RealMcpClients
            bad = [n for n, st in svc.gw.manifest_status.items() if not st["verified"]]
            if bad:
                raise RuntimeError(f"refusing to spawn MCP servers with unverified manifests: {bad}")
            async with RealMcpClients(svc.state_path) as real:
                svc.gw.real = real
                yield
            svc.gw.real = None
        else:
            yield

    app = FastAPI(title="The Unreliable Assistant (enterprise grade)", version="1.0", lifespan=lifespan)
    app.state.svc = svc

    @app.get("/health")
    async def health():
        return {"status": "ok", "llm": {"live": svc.live, "provider": "openrouter" if svc.live else "none (local stubs)", "spent_usd": round(svc.client.spent, 4) if svc.client else 0.0,
                "budget_usd": svc.client.budget if svc.client else 0.0}, "mcp_transport": "stdio" if svc.gw.real else "in-process", "mcp": svc.gw.status()}

    @app.post("/api/v1/agent/query")
    async def query(req: QueryRequest, request: Request, x_tenant_id: str | None = Header(None), x_role: str = Header("operator"),
                    traceparent: str | None = Header(None)):
        if x_tenant_id != req.tenant_id:                       # tenant header must match body (cross-tenant guard)
            svc.t.incr("tenant.mismatch")
            return JSONResponse(status_code=403, content={"error": ToolError(ErrorCode.TENANT_MISMATCH, "X-Tenant-ID does not match tenant_id").to_dict()})
        resp = await svc.handle(req, role=x_role, traceparent=traceparent)
        return JSONResponse(content=resp.model_dump(), headers={"traceparent": f"00-{resp.trace_id}-0000000000000001-01"})

    @app.get("/api/v1/hitl/pending")
    async def pending(tenant_id: str, x_tenant_id: str | None = Header(None)):
        if x_tenant_id != tenant_id:
            raise HTTPException(403, "tenant mismatch")
        return svc.state.list_approvals(tenant_id, "PENDING_APPROVAL")

    @app.post("/api/v1/hitl/{approval_id}/decision")
    async def decision(approval_id: str, d: Decision, tenant_id: str, x_tenant_id: str | None = Header(None), x_role: str = Header("operator")):
        if x_tenant_id != tenant_id:
            raise HTTPException(403, "tenant mismatch")
        if x_role != "reviewer":
            raise HTTPException(403, "only an authorised reviewer may decide")
        try:
            return svc.decide(tenant_id, approval_id, d.approve, d.reviewer)
        except ToolError as e:
            raise HTTPException(409 if e.code == ErrorCode.DUPLICATE_REQUEST else 403 if e.code == ErrorCode.RBAC_DENIED else 404, e.message)

    @app.get("/api/v1/plugins")
    async def plugins():
        return svc.plugins.status()

    @app.post("/api/v1/plugins/{name}/toggle")
    async def toggle(name: str, enabled: bool):
        svc.plugins.toggle(name, enabled)
        return svc.plugins.status()

    @app.post("/api/v1/breaker/reset")
    async def reset():
        svc.breaker.reset()
        return {"open": svc.breaker.open_keys}

    @app.get("/api/v1/mcp/verify")
    async def mcp_verify():
        return svc.gw.verify_manifests()

    @app.get("/api/v1/mcp/status")
    async def mcp_status():
        return {"servers": svc.gw.status(), "last_payloads": svc.gw.last_payloads[-5:]}

    @app.get("/api/v1/metrics")
    async def metrics():
        return svc.t.summary() | {"cache_hit_ratio": svc.cache.hit_ratio}

    @app.get("/api/v1/audit/verify")
    async def audit_verify(tenant_id: str):
        return {"tenant_id": tenant_id, "chain_valid": svc.state.verify_chain(tenant_id)}

    return app


app = create_app()
