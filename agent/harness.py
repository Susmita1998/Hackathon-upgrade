"""ToolExecutionHarness (Gate 4): strict validation -> deterministic auto-repair -> RBAC -> UUIDv4 idempotency -> breaker -> dispatch."""
from __future__ import annotations
import re, uuid
from dataclasses import dataclass, field
from pydantic import ValidationError
from . import config as C
from .models import (ErrorCode, GetFactsIn, HybridRetrieveIn, LookupLotIn, LookupSkuIn, ResolveSubgraphIn, SubmitFormIn,
                     ToolError, TransitionIn, UpsertFactIn)
from .plugins import args_hash


@dataclass
class ToolSpec:
    name: str; server: str; model: type; tier: str; mutating: bool = False


TOOLS = {t.name: t for t in [
    ToolSpec("hybrid_retrieve", "hybrid-rag", HybridRetrieveIn, "TIER_1_READ"),
    ToolSpec("resolve_subgraph", "graphify-vault", ResolveSubgraphIn, "TIER_1_READ"),
    ToolSpec("lookup_sku", "state-store", LookupSkuIn, "TIER_1_READ"),
    ToolSpec("lookup_lot", "state-store", LookupLotIn, "TIER_1_READ"),
    ToolSpec("get_session_facts", "state-store", GetFactsIn, "TIER_1_READ"),
    ToolSpec("upsert_fact", "state-store", UpsertFactIn, "TIER_2_COMPUTE"),
    ToolSpec("submit_form", "state-store", SubmitFormIn, "TIER_3_MUTATE", True),
    ToolSpec("transition_workflow", "state-store", TransitionIn, "TIER_3_MUTATE", True),
]}

_SKU = re.compile(r"^\s*sku[\s_\-#:]*0*(\d{1,6})\s*$", re.I)
_LOT = re.compile(r"^\s*lot[\s_\-#:]*0*(\d{1,5})\s*$", re.I)


def repair_value(key: str, value, annotation_hint: str | None = None):
    """Deterministic, side-effect-free repairs. Returns (new_value, changed)."""
    if isinstance(value, str):
        v = value.strip()
        if key in ("sku",) and (m := _SKU.match(v)):
            return f"SKU-{int(m.group(1)):03d}", True
        if key in ("lot_id",) and (m := _LOT.match(v)):
            return f"LOT-{int(m.group(1)):05d}", True
        if key == "to_status":
            return v.lower(), v.lower() != value
        if key in ("top_k_sparse", "top_k_dense", "rrf_k", "rerank_k", "depth", "max_tokens") and re.fullmatch(r"\d+", v):
            return int(v), True
        if key == "threshold" and re.fullmatch(r"\d*\.?\d+", v):
            return float(v), True
        return v, v != value
    if key == "threshold" and isinstance(value, int) and not isinstance(value, bool):
        return float(value), True
    return value, False


def repair_args(args: dict) -> tuple[dict, bool]:
    out, changed = {}, False
    for k, v in args.items():
        nv, ch = repair_value(k, v)
        out[k], changed = nv, changed or (nv != v)
    return out, changed


def is_uuid4(s) -> bool:
    try:
        return uuid.UUID(str(s)).version == 4
    except Exception:
        return False


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: dict | None = None
    error: dict | None = None
    repaired: bool = False
    args: dict = field(default_factory=dict)


class ToolExecutionHarness:
    def __init__(self, gateway, plugins, state, telemetry):
        self.gw, self.plugins, self.state, self.t = gateway, plugins, state, telemetry
        self._idem: dict[str, str] = {}

    async def call(self, ctx, tool: str, raw_args: dict) -> ToolResult:
        spec = TOOLS.get(tool)
        if not spec:
            return ToolResult(False, tool, error=ToolError(ErrorCode.INVALID_PARAMS, f"unknown tool {tool}").to_dict())
        allowed = ctx.meta.get("allowed_tools")
        if allowed is not None and tool not in allowed:                      # subagent isolation (design.md 1.3)
            self.t.incr("harness.scope_denied")
            return ToolResult(False, tool, error=ToolError(ErrorCode.RBAC_DENIED, f"{tool} is outside this subagent's tool scope").to_dict())
        args = dict(raw_args)
        repaired = False
        # (1) strict validation
        self.t.incr("harness.calls")
        probe = {k: v for k, v in args.items()}
        if spec.mutating:
            probe.setdefault("idempotency_key", "pending")
        try:
            spec.model(**probe)
            self.t.incr("harness.first_pass_ok")
        except ValidationError:
            # (2) deterministic auto-repair
            self.t.incr("harness.repair_attempts")
            args, repaired = repair_args(args)
            probe = dict(args)
            if spec.mutating:
                probe.setdefault("idempotency_key", "pending")
            try:
                spec.model(**probe)
                self.t.incr("harness.repair_ok")
            except ValidationError as e:
                self.t.incr("harness.invalid")
                err = ToolError(ErrorCode.INVALID_PARAMS, "arguments failed validation after auto-repair",
                                {"errors": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()][:5]})
                self._result(ctx, tool, args, False)
                return ToolResult(False, tool, error=err.to_dict(), repaired=repaired, args=args)
        # (3) RBAC tier
        if C.TIER_RANK[spec.tier] > C.TIER_RANK[ctx.max_tier]:
            self.t.incr("harness.rbac_denied")
            return ToolResult(False, tool, error=ToolError(ErrorCode.RBAC_DENIED, f"{spec.tier} not permitted for role {ctx.role}").to_dict(), args=args)
        # (4) UUIDv4 idempotency for mutations (mint deterministically per logical action so retries reuse it)
        h = args_hash(tool, {k: v for k, v in args.items() if k != "idempotency_key"})
        if spec.mutating:
            key = args.get("idempotency_key")
            if key is not None and not is_uuid4(key):
                return ToolResult(False, tool, error=ToolError(ErrorCode.INVALID_PARAMS, "idempotency_key must be a UUIDv4").to_dict(), args=args)
            if key is None:
                args["idempotency_key"] = self._idem.setdefault(f"{ctx.tenant_id}:{ctx.session_id}:{h}", str(uuid.uuid4()))
        # (5) breaker
        try:
            self.plugins.on_dispatch(ctx, tool, h)
        except ToolError as e:
            self.t.incr("harness.breaker_open")
            return ToolResult(False, tool, error=e.to_dict(), repaired=repaired, args=args)
        # (6) Tier 3 -> HITL freeze: never executed here
        try:
            if spec.mutating:
                data = await self.gw.call_tool("state-store", "request_approval",
                                               {"tool": tool, "args": args, "requested_by": ctx.user_id}, ctx.tenant_id, ctx.trace)
                self.t.incr("hitl.requested")
                err = ToolError(ErrorCode.HITL_PENDING, "Tier 3 action frozen pending human approval", {"approval_id": data["approval_id"]})
                return ToolResult(False, tool, error=err.to_dict(), repaired=repaired, args=args)
            data = await self.gw.call_tool(spec.server, tool, args, ctx.tenant_id, ctx.trace)
            if tool == "hybrid_retrieve" and isinstance(data, dict):             # blueprint span names for the 3-stage pipeline
                for stage, span in (("bm25", "mcp.rag.bm25"), ("dense", "mcp.rag.biencoder"), ("rrf", "mcp.rag.rrf"), ("rerank", "mcp.rag.rerank")):
                    if stage in data.get("stage_ms", {}):
                        ctx.trace.add(span, data["stage_ms"][stage])
            self._result(ctx, tool, args, True)
            return ToolResult(True, tool, data=data, repaired=repaired, args=args)
        except ToolError as e:
            self._result(ctx, tool, args, False)
            return ToolResult(False, tool, error=e.to_dict(), repaired=repaired, args=args)

    def _result(self, ctx, tool, args, ok):
        self.plugins.on_result(ctx, tool, args_hash(tool, {k: v for k, v in args.items() if k != "idempotency_key"}), ok)
