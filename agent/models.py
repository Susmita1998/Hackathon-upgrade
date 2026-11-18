"""Pydantic v2 contracts and error codes (design.md 1.2)."""
from __future__ import annotations
from enum import IntEnum
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

TENANT_RE = r"^tenant_[A-Za-z0-9_-]{1,64}$"
SKU_RE = r"^SKU-\d{3,}$"
LOT_RE = r"^LOT-\d{5}$"
Tier = Literal["TIER_1_READ", "TIER_2_COMPUTE", "TIER_3_MUTATE"]
Route = Literal["DIRECT_ANSWER", "HYBRID_RAG", "TRANSACTION_EXEC", "REJECT_OOS"]


class ErrorCode(IntEnum):
    INVALID_PARAMS = -32602
    RBAC_DENIED = -32001
    HITL_PENDING = -32002
    BREAKER_OPEN = -32003
    MCP_TIMEOUT = -32004
    DUPLICATE_REQUEST = -32005
    TENANT_MISMATCH = -32006
    POLICY_BLOCKED = -32007
    NOT_FOUND = -32008


class ToolError(Exception):
    def __init__(self, code: ErrorCode, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def to_dict(self):
        return {"code": int(self.code), "name": self.code.name, "message": self.message, "data": self.data}


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


# ---- tool inputs ----
class HybridRetrieveIn(Strict):
    query: str = Field(min_length=1, max_length=2000)
    top_k_sparse: int = Field(50, ge=1, le=50)
    top_k_dense: int = Field(50, ge=1, le=50)
    rrf_k: int = Field(60, ge=1, le=200)
    rerank_k: int = Field(4, ge=1, le=10)
    threshold: float = Field(0.42, ge=0.0, le=1.0)

class LookupSkuIn(Strict):
    sku: str = Field(pattern=SKU_RE)

class LookupLotIn(Strict):
    lot_id: str = Field(pattern=LOT_RE)

class SubmitFormIn(Strict):
    form: str = Field(min_length=1, max_length=80)
    sku: str | None = Field(None, pattern=SKU_RE)
    lot_id: str | None = Field(None, pattern=LOT_RE)
    note: str = Field("", max_length=500)
    idempotency_key: str

class TransitionIn(Strict):
    lot_id: str = Field(pattern=LOT_RE)
    to_status: Literal["released", "quarantined", "recalled", "inspected"]
    idempotency_key: str

class UpsertFactIn(Strict):
    session_id: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=400)

class GetFactsIn(Strict):
    session_id: str = Field(min_length=1, max_length=64)

class ResolveSubgraphIn(Strict):
    anchor: str = Field(min_length=1, max_length=200)
    depth: int = Field(2, ge=1, le=2)
    max_tokens: int = Field(320, ge=20, le=400)


class Chunk(BaseModel):
    chunk_id: str
    chunk_hash: str
    text: str
    score: float = 0.0
    source_uri: str = ""


# ---- API ----
class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(pattern=TENANT_RE)
    user_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(None, max_length=64)


class QueryResponse(BaseModel):
    tenant_id: str
    user_id: str
    session_id: str
    route: str
    status: Literal["OK", "ABSTAINED", "BLOCKED", "PENDING_APPROVAL", "HALTED", "REJECTED", "ERROR"]
    answer: str
    citations: list[str] = []
    steps: int = 0
    trace_id: str = ""
    latency_ms: float = 0.0
    cache: Literal["hit", "miss", "bypass"] = "miss"
    faithfulness: float | None = None
    pending_approval_id: str | None = None
    error: dict | None = None
    spans: list[dict] = []
    models: list[dict] = []          # one entry per real LLM call: role, model, ms, tokens, cost, fallbacks
    llm_cost_usd: float = 0.0
    evidence: list[dict] = []        # the exact context the LLM saw (only when the service runs with return_evidence, e.g. RAGAS scoring)
