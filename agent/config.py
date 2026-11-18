"""Single source of truth for thresholds and budgets (design.md sections 1.2-1.7)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT_DIR = ROOT / "knowledge_vault"
GRAPH_PATH = ROOT / "graph.json"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

# RAG (Gate 3)
BM25_K1, BM25_B, BM25_TOP_K = 1.25, 0.75, 50
DENSE_TOP_K, HNSW_M, HNSW_EF_SEARCH = 50, 32, 64
CHUNK_TOKENS, CHUNK_OVERLAP = 512, 64
RRF_K, RRF_POOL = 60, 40
RERANK_K, RERANK_THRESHOLD, RERANK_MAX_LEN = 4, 0.42, 512
STRICT_RERANK_THRESHOLD = 0.55          # hallucination-surge runbook

# Loop (Gate 4)
import os as _o
MAX_ITERATIONS, LOOP_BUDGET_S = 5, 0.450
STEP_TIMEOUT_S = float(_o.environ.get("STEP_TIMEOUT_S", "0.120"))     # blueprint: 120 ms; env override for loaded CI/test machines
MAX_OBSERVATION_TOKENS = 180
HOT_MEMORY_TURNS = 3
BREAKER_MAX_FAILS = 2

# Reliability (Gate 9)
POOLS = {"rag": 64, "db": 128, "llm": 256}
MCP_TIMEOUT_S = float(_o.environ.get("MCP_TIMEOUT_S", "0.150"))        # blueprint: 150 ms JSON-RPC timeout
ABSTAIN_BUDGET_MS = 90

# Cache (Gate 11 runbook)
CACHE_TTL_S, CACHE_TTL_JITTER_S = 300, 45
SEMANTIC_CACHE_COSINE = 0.92

# Governance
MIN_FAITHFULNESS = 0.92
TIER_RANK = {"TIER_1_READ": 1, "TIER_2_COMPUTE": 2, "TIER_3_MUTATE": 3}
ABSTENTION_MESSAGE = ("I can't answer that from the approved knowledge base, so I won't guess. "
                      "Please rephrase with a specific policy, SKU or LOT ID, or contact the policy owner.")

# Pricing assumptions used ONLY for KPI-29 (illustrative, not a quote). The blueprint serves the LLM with vLLM/TensorRT-LLM,
# so the primary basis is a self-hosted small model; the hosted-API figure is kept for comparison in the report.
PRICE_PER_1K_INPUT_TOKENS_USD = 0.0003      # $0.30 / M tokens (self-hosted small model, GPU cost amortised)
PRICE_PER_1K_OUTPUT_TOKENS_USD = 0.0009     # $0.90 / M tokens
HOSTED_API_PRICE_IN_PER_1K, HOSTED_API_PRICE_OUT_PER_1K = 0.003, 0.015   # $3 / $15 per M, for comparison

# ---- Live LLM (OpenRouter) -------------------------------------------------------------
import os as _os
LLM_MODE = _os.environ.get("LLM_MODE", "auto")           # off | openrouter | auto (live if OPENROUTER_API_KEY is set)
OR_BUDGET_USD = float(_os.environ.get("OR_BUDGET_USD", "3.00"))   # hard per-process spend cap; beyond it every step falls back to local logic
OR_TIMEOUTS_S = {"triage": 8.0, "plan": 12.0, "generate": 30.0, "verify": 15.0, "direct": 10.0, "judge": 30.0}
LOOP_BUDGET_LLM_S = 40.0                                  # remote LLM calls cannot fit the 450 ms local budget (see design.md ADR-06)
# The workspace guardrail only permits these Anthropic models (probed 2026-11-18). Override with OR_MODELS_<ROLE>=a,b,c
_H5, _H45, _S5 = "anthropic/claude-haiku-5.5", "anthropic/claude-haiku-4.5", "anthropic/claude-sonnet-5.5"
MODEL_CHAINS = {
    "triage": [_H45, _H5], "plan": [_H5, _H45], "direct": [_H5, _H45],
    "generate": [_H5, _H45], "generate_strong": [_S5, _H45, _H5],     # strong path: Tier-3 risk, high complexity, multi-hop
    "verify": [_H45, _H5],                                            # independent of the generator whenever possible
    "judge": [_S5, _H45],
}
for _role in list(MODEL_CHAINS):
    _v = _os.environ.get(f"OR_MODELS_{_role.upper()}")
    if _v:
        MODEL_CHAINS[_role] = [x.strip() for x in _v.split(",") if x.strip()]
MANDATORY_REASONING_MODELS = {"anthropic/claude-sonnet-5.5"}
LLM_RETRIES, LLM_COOLDOWN_S, LLM_COOLDOWN_AFTER = 1, 30.0, 3

# Subagent tool scopes (design.md 1.3): enforced by the harness
RETRIEVAL_TOOLS = {"hybrid_retrieve", "resolve_subgraph", "lookup_sku", "lookup_lot", "get_session_facts"}       # read-only
EXECUTION_TOOLS = {"lookup_sku", "lookup_lot", "get_session_facts", "upsert_fact", "submit_form", "transition_workflow"}   # no RAG / web
