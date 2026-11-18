"""AgentService: wires ingress -> plugins -> cache -> triage -> bounded loop -> verification -> egress (design.md 1.3)."""
from __future__ import annotations
import asyncio, hashlib, os, time, uuid
from . import config as C
from .cache import HotMemory, SemanticCache
from .graphify import Graphify, approx_tokens
from .harness import ToolExecutionHarness
from .llm import ExtractiveLLM, compress_chunks
from .llm_agents import LLMDirect, LLMGenerator, LLMPlanner, LLMTriage, LLMVerifier
from .openrouter import OpenRouterClient
from .router import ModelRouter
from .loop import ReActLoop, StubPlanner
from .mcp import GraphServer, McpGateway, RagServer, StateServer
from .models import ErrorCode, QueryRequest, QueryResponse, ToolError
from .plugins import (CircuitBreaker, InjectionShieldPlugin, OtelExporter, PiiRedactor, PluginManager, RagasAuditor, RequestContext)
from .rag import HashEmbedder, RagIndex, load_vault, tokens
from .security import InjectionShield
from .state import StateStore
from .telemetry import Telemetry, Trace
from .triage import TriageAgent
from .verify import faithfulness

DIRECT = ("Hello! I can answer questions about approved enterprise policies, look up SKU/LOT status, and file workflow "
          "requests (state-changing actions always need human approval).")
OOS = "That request is outside the enterprise policy and inventory scope I'm allowed to help with."
BLOCK = "I can't process that request because it was blocked by a security policy."


class AgentService:
    def __init__(self, extra_chunks=None, planner=None, state_path=":memory:", redis=None, llm: str | None = None, client=None, verify_manifests: bool = True):
        self.t = Telemetry()
        self.notes = load_vault(C.VAULT_DIR)
        self.index = RagIndex(self.notes, extra_chunks=extra_chunks)
        self.state_path = state_path
        self.state = StateStore(state_path)
        self.graph = Graphify()
        self.shield = InjectionShield()
        self.rag_srv, self.state_srv, self.graph_srv = RagServer(self.index, self.shield), StateServer(self.state), GraphServer(self.graph)
        self.gw = McpGateway([self.rag_srv, self.state_srv, self.graph_srv], self.t, verify_manifests=verify_manifests)
        self.plugins = PluginManager()
        self.breaker = self.plugins.register(CircuitBreaker())
        self.plugins.register(PiiRedactor()); self.plugins.register(InjectionShieldPlugin(self.shield))
        self.ragas = self.plugins.register(RagasAuditor()); self.plugins.register(OtelExporter(self.t))
        self.harness = ToolExecutionHarness(self.gw, self.plugins, self.state, self.t)
        self.emb = HashEmbedder()
        mode = (llm or C.LLM_MODE).lower()
        want_live = mode == "openrouter" or (mode == "auto" and bool(os.environ.get("OPENROUTER_API_KEY")))
        self.client = client if client is not None else (OpenRouterClient() if want_live else None)
        self.router = ModelRouter(self.client, self.t)
        self.live = self.router.live
        self.loop = ReActLoop(self.harness, self.emb, planner or (LLMPlanner(self.router) if self.live else StubPlanner()),
                              budget_s=C.LOOP_BUDGET_LLM_S if self.live else C.LOOP_BUDGET_S)
        self.cache = SemanticCache(redis, self.emb)
        self.memory = HotMemory(self.cache.r)
        self.llm = ExtractiveLLM()
        self.llm_sem = asyncio.Semaphore(C.POOLS["llm"])
        self.domain_lex = {}
        for n in self.notes:
            self.domain_lex.setdefault(n["domain"], set()).update(tokens(n["id"].replace("-", " ")))
        self.triage = TriageAgent(domain_lex={}, vocab=set(self.index.idf))          # deterministic local classifier (fallback / offline)
        self.triage_llm = LLMTriage(self.router, self.triage)
        self.generator = LLMGenerator(self.router, self.llm)
        self.verifier = LLMVerifier(self.router)
        self.direct = LLMDirect(self.router, DIRECT)
        self.hot_memory: dict[str, list] = {}
        self.return_evidence = os.environ.get("RETURN_EVIDENCE") == "1"       # RAGAS scoring needs the exact retrieved contexts

    # ------------------------------------------------------------------
    async def handle(self, req: QueryRequest, role: str = "operator", traceparent: str | None = None) -> QueryResponse:
        t0 = time.perf_counter()
        trace = Trace(traceparent)
        sid = req.session_id or f"s-{uuid.uuid4().hex[:12]}"
        ctx = RequestContext(req.tenant_id, req.user_id, sid, trace, role)
        base = dict(tenant_id=req.tenant_id, user_id=req.user_id, session_id=sid, trace_id=trace.trace_id)
        evidence: list[str] = []

        def finish(resp: QueryResponse) -> QueryResponse:
            resp.latency_ms = round((time.perf_counter() - t0) * 1e3, 3)
            self.t.observe("e2e_ms", resp.latency_ms)
            self.t.incr(f"status.{resp.status}")
            try:
                self.plugins.run_post(ctx, resp, evidence)
            except Exception:
                pass
            resp.spans = [{"name": s["name"], "ms": round(s["ms"], 3)} for s in trace.spans]
            resp.models = ctx.meta.get("llm_calls", [])
            if self.return_evidence:
                resp.evidence = ctx.meta.get("evidence", [])
            resp.llm_cost_usd = round(sum(c["cost_usd"] for c in resp.models), 6)
            self.state.audit(req.tenant_id, "QUERY", {"route": resp.route, "status": resp.status, "user": req.user_id,
                                                      "q_sha": hashlib.sha256(req.query.encode()).hexdigest()[:16],
                                                      "pii": ctx.meta.get("pii", {}), "steps": resp.steps})
            return resp

        self.state.seed_tenant(req.tenant_id)
        # ---- Pre-execution plugins (PII -> injection) ----
        try:
            with trace.span("hook.pre"):
                text = self.plugins.run_pre(ctx, req.query)
        except ToolError as e:
            return finish(QueryResponse(**base, route="REJECT_OOS", status="BLOCKED", answer=BLOCK, error=e.to_dict(), cache="bypass"))
        # ---- Semantic cache (skipped for follow-ups: an answer that depended on session memory must never be served to another session) ----
        recent = self.memory.load(req.tenant_id, sid)
        with trace.span("cache.lookup"):
            cached = None if recent else self.cache.get(req.tenant_id, req.user_id, text)
        if cached:
            self.t.incr("cache.hit")
            r = QueryResponse(**{**cached, **base}); r.cache = "hit"; r.models = []; r.llm_cost_usd = 0.0
            if r.status == "OK" and r.route == "HYBRID_RAG":
                self.memory.push(req.tenant_id, sid, {"q": text[:300], "a": r.answer[:400]})
            return finish(r)
        self.t.incr("cache.miss")
        # ---- Triage ----
        with trace.span("triage.classify") as sp:
            tri = await self.triage_llm.classify(text, ctx, recent)
            sp["attrs"].update(route=tri.route, risk=tri.risk_tier)
        if tri.route == "REJECT_OOS":
            return finish(QueryResponse(**base, route=tri.route, status="REJECTED", answer=OOS))
        if tri.route == "DIRECT_ANSWER":
            reply, _m = await self.direct.reply(text, ctx)
            return finish(QueryResponse(**base, route=tri.route, status="OK", answer=reply))
        # ---- Bounded ReAct loop ----
        prior = None
        try:
            prior = await self.gw.call_tool("state-store", "get_session_facts", {"session_id": sid}, req.tenant_id, trace)
        except ToolError:
            pass
        with trace.span("agent.react"):
            st = await self.loop.run(ctx, text, tri.route, prior or None, recent)
        if st.status == "HALTED":
            err = st.error_data or {}
            msg = (err.get("data") or {}).get("remediation") or err.get("remediation") or "I stopped because the request could not be completed safely."
            return finish(QueryResponse(**base, route=tri.route, status="HALTED", answer=msg, steps=st.steps, error=err, cache="miss"))
        if st.pending_approval_id:
            return finish(QueryResponse(**base, route=tri.route, status="PENDING_APPROVAL", steps=st.steps, pending_approval_id=st.pending_approval_id,
                                        answer=f"Your request needs human approval before it can run (approval id {st.pending_approval_id}).", cache="miss"))
        # ---- Generation (or deterministic abstention short-circuit) ----
        chunks = list({c["chunk_id"]: c for c in st.chunks}.values())
        obs = [o for o in st.observations]
        if not chunks and not any("failed" not in o for o in obs):
            ans = C.ABSTENTION_MESSAGE
            if obs:
                ans = " ".join(obs) + " " + ans
            return finish(QueryResponse(**base, route=tri.route, status="ABSTAINED", answer=ans, steps=st.steps, cache="miss"))
        q_gen = st.answer_query or text
        chunks = compress_chunks(q_gen, chunks)                              # token optimisation: only relevant sentences reach the LLM
        ctx.meta["evidence"] = [{"chunk_id": c["chunk_id"], "text": c["text"]} for c in chunks]
        multi_hop = any(c[0] == "hop" for c in st.calls)
        async with self.llm_sem:
            with trace.span("llm.generate") as sp:
                if self.live:
                    answer, cites, ttft, gen_model, abstain = await self.generator.generate(q_gen, chunks, obs, ctx, tri.risk_tier, tri.complexity, multi_hop, recent)
                else:
                    answer, cites, ttft = await asyncio.to_thread(self.llm.generate, q_gen, chunks, obs)
                    gen_model, abstain = "local-extractive", False
                sp["attrs"].update(ttft_ms=ttft, model=gen_model)
        self.t.observe("ttft_ms", ttft)
        if abstain:                                                           # model says the evidence is insufficient
            return finish(QueryResponse(**base, route=tri.route, status="ABSTAINED", answer=C.ABSTENTION_MESSAGE, steps=st.steps, cache="miss"))
        if chunks and not cites:
            self.t.incr("generate.no_citation")
        # ---- Verification subagent (independent model context, may veto) ----
        evidence = [c["text"] for c in chunks] + obs
        with trace.span("verify.claims") as sp:
            f, verdicts, ver_model = await self.verifier.check(answer, evidence, ctx, avoid_model=gen_model)
            sp["attrs"].update(model=ver_model, claims=len(verdicts))
        base_tok = sum(approx_tokens(c.text) for c in self.index.chunks[:8]) + approx_tokens(text)
        opt_tok = approx_tokens(text) + sum(approx_tokens(c["text"]) for c in chunks) + 12
        self.t.observe("tokens.baseline", base_tok); self.t.observe("tokens.optimized", opt_tok); self.t.observe("tokens.out", approx_tokens(answer))
        if f < C.MIN_FAITHFULNESS:
            self.t.incr("verify.veto")
            return finish(QueryResponse(**base, route=tri.route, status="ABSTAINED", answer=C.ABSTENTION_MESSAGE, steps=st.steps, faithfulness=round(f, 4)))
        for key, val in st.facts:                                  # verified facts -> long-term memory
            try:
                await self.gw.call_tool("state-store", "upsert_fact", {"session_id": sid, "key": key, "value": val}, req.tenant_id, trace)
            except ToolError:
                pass
        resp = QueryResponse(**base, route=tri.route, status="OK", answer=answer, citations=cites, steps=st.steps, faithfulness=round(f, 4))
        self.memory.push(req.tenant_id, sid, {"q": text[:300], "a": answer[:400]})
        self.t.observe("steps", st.steps)
        if st.drift:
            self.t.observe("drift", sum(st.drift) / len(st.drift))
        out = finish(resp)
        if tri.route != "TRANSACTION_EXEC" and not recent:
            self.cache.put(req.tenant_id, req.user_id, text, out.model_dump(exclude={"tenant_id", "user_id", "session_id", "trace_id", "spans", "latency_ms", "cache", "models", "llm_cost_usd", "evidence"}))
        return out

    # ---- HITL ----
    def decide(self, tenant_id: str, approval_id: str, approve: bool, reviewer: str):
        return self.state.decide(tenant_id, approval_id, approve, reviewer)
