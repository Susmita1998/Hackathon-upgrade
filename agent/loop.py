"""Bounded ReAct loop (Gate 4/5): deterministic state machine, goal anchoring, observation compression, dual memory.

State machine: INIT -> TRIAGE -> REASONING -> TOOL_CALL -> OBSERVATION -> VERIFY -> DONE / HALTED
Hard limits: max 5 iterations, 120 ms per step, 450 ms total budget.
"""
from __future__ import annotations
import asyncio, inspect, json, re, time
from collections import deque
from dataclasses import dataclass, field
from . import config as C
from .graphify import approx_tokens
from .rag import tokens

ENT = re.compile(r"\b(SKU[\s_-]*\d{1,6}|LOT[\s_-]*\d{1,5})\b", re.I)
VERB_STATUS = {"release": "released", "quarantine": "quarantined", "recall": "recalled", "inspect": "inspected", "close": "released"}


@dataclass(frozen=True)
class GoalAnchorBlock:
    """Immutable; always prepended at index 0 of every ReAct prompt."""
    intent: str
    entities: tuple
    constraints: tuple = ("read-only unless Tier 3 approved", "answer only from cited evidence")

    def render(self) -> str:
        return f"[GOAL] {self.intent} | entities={list(self.entities)} | constraints={list(self.constraints)}"


def compress_observation(obs, max_tokens: int = C.MAX_OBSERVATION_TOKENS) -> str:
    """Extractive compressor: drop JSON keys/boilerplate, keep values, cap tokens."""
    if isinstance(obs, dict):
        if "chunks" in obs:
            text = " ".join(f"{c['chunk_id']}: {c['text']}" for c in obs["chunks"])
        else:
            text = "; ".join(f"{k}={v}" for k, v in obs.items() if k not in ("stage_ms", "stages", "quarantined", "confidence", "abstain"))
    else:
        text = str(obs)
    words = text.split()
    cap = int(max_tokens / 1.3)
    return " ".join(words[:cap])


@dataclass
class Action:
    kind: str                          # "tool" | "final"
    tool: str = ""
    args: dict = field(default_factory=dict)
    objective: str = ""                # model's restatement of its current objective (for drift audit)
    final: str = ""


class StubPlanner:
    """Deterministic planner standing in for the LLM reasoning step."""
    def __init__(self, anchored: bool = True):
        self.anchored = anchored

    def next_action(self, route: str, goal: GoalAnchorBlock, state: "LoopState") -> Action:
        obj = goal.intent if self.anchored else (state.observations[-1] if state.observations else goal.intent)
        q = goal.intent
        ents = [e for e in goal.entities]
        called = {c for c, _ in state.calls}
        if route == "HYBRID_RAG":
            if "retrieve" not in called:
                return Action("tool", "hybrid_retrieve", {"query": q}, obj)
            hop = self._hop(q, state)
            if hop and "hop" not in called:
                state.answer_query = hop[1]
                return Action("tool", "resolve_subgraph", {"anchor": hop[0], "depth": 1, "max_tokens": 320}, obj)
            for e in ents:
                if e.upper().startswith("LOT") and f"lookup_lot:{e}" not in called:
                    return Action("tool", "lookup_lot", {"lot_id": e}, obj)
                if e.upper().startswith("SKU") and f"lookup_sku:{e}" not in called:
                    return Action("tool", "lookup_sku", {"sku": e}, obj)
            return Action("final", objective=obj)
        if route == "TRANSACTION_EXEC":
            lots = [e for e in ents if e.upper().startswith("LOT")]
            skus = [e for e in ents if e.upper().startswith("SKU")]
            if lots and f"lookup_lot:{lots[0]}" not in called:
                return Action("tool", "lookup_lot", {"lot_id": lots[0]}, obj)
            if skus and not lots and f"lookup_sku:{skus[0]}" not in called:
                return Action("tool", "lookup_sku", {"sku": skus[0]}, obj)
            if "mutate" not in called and not state.errors:
                m = re.search(r"\b(release|quarantine|recall|inspect|close)\b", q, re.I)
                if lots and (m or re.search(r"transition", q, re.I)):
                    return Action("tool", "transition_workflow", {"lot_id": lots[0], "to_status": VERB_STATUS.get(m.group(1).lower() if m else "", "inspected")}, obj)
                form = re.sub(r"\s+", " ", re.sub(ENT, "", q))[:80].strip() or "generic form"
                args = {"form": form, "note": ""}
                if skus: args["sku"] = skus[0]
                if lots: args["lot_id"] = lots[0]
                return Action("tool", "submit_form", args, obj)
            return Action("final", objective=obj)
        return Action("final", objective=obj)

    @staticmethod
    def _hop(q: str, state: "LoopState"):
        """Multi-hop: follow the [[wikilink]] found in retrieved evidence via mcp-graphify-vault."""
        m = re.search(r"(?:what is|what's) the (.+?) of the procedure that", q, re.I)
        if not m or not state.chunks:
            return None
        subj = re.search(r"escalations for (.+?) follow", q, re.I)
        for ch in state.chunks:
            pat = (re.escape(subj.group(1)) if subj else r"[^\n\[]+?")
            link = re.search(rf"Escalations for {pat} follow the \[\[([^\]]+)\]\] procedure \(([^)]+)\)", ch["text"], re.I)
            if link:
                return link.group(1), f"What is the {m.group(1)} for {link.group(2)}?"
        return None


@dataclass
class LoopState:
    observations: list = field(default_factory=list)
    chunks: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    steps: int = 0
    status: str = "INIT"
    pending_approval_id: str | None = None
    transitions: list = field(default_factory=lambda: ["INIT"])
    drift: list = field(default_factory=list)
    hot: deque = field(default_factory=lambda: deque(maxlen=C.HOT_MEMORY_TURNS))
    facts: list = field(default_factory=list)
    error_data: dict | None = None
    answer_query: str | None = None
    graph_tokens: int = 0
    recent_turns: list = field(default_factory=list)       # last <=3 turns of this session (redacted), oldest first
    ctx: object = None
    done_calls: set = field(default_factory=set)


def entities_from(q: str) -> tuple:
    out = []
    for m in ENT.findall(q):
        out.append(m)
    return tuple(out)


class ReActLoop:
    def __init__(self, harness, embedder, planner=None, budget_s: float = C.LOOP_BUDGET_S):
        self.h, self.emb = harness, embedder
        self.planner = planner or StubPlanner()
        self.budget_s = budget_s

    def _move(self, st, to):
        st.status = to; st.transitions.append(to)

    async def run(self, ctx, query: str, route: str, prior_facts: dict | None = None, recent_turns: list | None = None) -> LoopState:
        st = LoopState(ctx=ctx, recent_turns=list(recent_turns or []))
        ctx.meta["allowed_tools"] = set(C.RETRIEVAL_TOOLS if route == "HYBRID_RAG" else C.EXECUTION_TOOLS)   # subagent tool scope
        goal = GoalAnchorBlock(intent=query, entities=entities_from(query))
        goal_vec = self.emb.embed([goal.intent])[0]
        t_start = time.perf_counter()
        self._move(st, "TRIAGE")
        for _ in range(C.MAX_ITERATIONS):
            if time.perf_counter() - t_start > self.budget_s:
                self._move(st, "HALTED"); st.error_data = {"reason": "loop budget exceeded"}; return st
            self._move(st, "REASONING")
            # prompt[0] is ALWAYS the immutable goal anchor
            prompt = [goal.render()] + ([f"[FACTS] {prior_facts}"] if prior_facts else []) + [f"[TURN] {t['q']} -> {t['a']}" for t in st.recent_turns] + list(st.hot)
            assert prompt[0].startswith("[GOAL]")
            act = self.planner.next_action(route, goal, st)
            if inspect.isawaitable(act):
                act = await act
            if act.objective:
                import numpy as np
                st.drift.append(float(np.dot(goal_vec, self.emb.embed([act.objective])[0])))
            if act.kind == "final":
                self._move(st, "VERIFY"); self._move(st, "DONE"); return st
            dup = (act.tool, json.dumps(act.args, sort_keys=True, default=str))
            if dup in st.done_calls:                                        # same successful call again = no progress -> stop
                self._move(st, "VERIFY"); self._move(st, "DONE"); return st
            st.steps += 1
            self._move(st, "TOOL_CALL")
            t_step = time.perf_counter()
            try:
                res = await asyncio.wait_for(self.h.call(ctx, act.tool, act.args), C.STEP_TIMEOUT_S * 3)  # harness enforces 150ms MCP cap itself
            except asyncio.TimeoutError:
                self._move(st, "HALTED"); st.error_data = {"reason": "step timeout"}; return st
            self._move(st, "OBSERVATION")
            tag = {"hybrid_retrieve": "retrieve", "resolve_subgraph": "hop"}.get(act.tool)
            if act.tool in ("lookup_lot", "lookup_sku"):
                tag = f"{act.tool}:{list(act.args.values())[0]}"
            if act.tool in ("transition_workflow", "submit_form"):
                tag = "mutate"
            st.calls.append((tag or act.tool, act.args))
            if res.ok:
                st.done_calls.add(dup)
                if act.tool == "hybrid_retrieve":
                    st.chunks.extend(res.data["chunks"])
                    if res.data.get("quarantined"):
                        ctx.meta.setdefault("quarantined", []).extend(res.data["quarantined"])
                if act.tool == "resolve_subgraph" and res.data.get("items"):
                    top = res.data["items"][0]
                    st.chunks.append({"chunk_id": f"{top['id']}#0", "chunk_hash": "graph", "text": top["text"], "score": 1.0})
                    st.graph_tokens += res.data.get("tokens", 0)
                obs = compress_observation(res.data) if act.tool != "resolve_subgraph" else ""
                if act.tool in ("hybrid_retrieve", "resolve_subgraph"):
                    obs = ""                                  # evidence is carried by chunks, not observation strings
                else:
                    st.observations.append(obs)
                st.hot.append(f"[OBS:{act.tool}] {compress_observation(res.data)}")
                if act.tool in ("lookup_lot", "lookup_sku"):
                    st.facts.append((list(act.args.values())[0], compress_observation(res.data)))
            else:
                err = res.error
                st.errors.append(err)
                st.hot.append(f"[ERR:{act.tool}] {err['name']}")
                if err["name"] == "HITL_PENDING":
                    st.pending_approval_id = err["data"]["approval_id"]; self._move(st, "VERIFY"); self._move(st, "DONE"); return st
                if err["name"] == "BREAKER_OPEN":
                    self._move(st, "HALTED"); st.error_data = err; return st
                if err["name"] in ("RBAC_DENIED",):
                    self._move(st, "HALTED"); st.error_data = err; return st
                st.observations.append(f"{act.tool} failed: {err['message']}")
            ctx.trace.add("agent.react.step", (time.perf_counter() - t_step) * 1e3, tool=act.tool)
            self._move(st, "VERIFY")
        if st.chunks:                                                       # iteration cap hit but evidence exists: answer from it (still bounded)
            ctx.meta["iteration_cap_reached"] = True
            self._move(st, "VERIFY"); self._move(st, "DONE"); return st
        self._move(st, "HALTED"); st.error_data = {"reason": "max_iterations reached", "remediation": "Narrow the request or add IDs."}
        return st
