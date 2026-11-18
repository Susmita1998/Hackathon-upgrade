"""Generate a simplified docs/workflow_orchestration.png (Gate 2) with Graphviz at 300 DPI."""
import sys
from graphviz import Digraph

out = sys.argv[1] if len(sys.argv) > 1 else "docs/workflow_orchestration"
g = Digraph("workflow", format="png")
g.attr(rankdir="TB", dpi="300", fontname="Helvetica", labelloc="t", fontsize="24",
       label="The Unreliable Assistant — Workflow Orchestration", nodesep="0.5", ranksep="0.55", splines="spline")
g.attr("node", shape="box", style="rounded,filled", fontname="Helvetica", fontsize="14",
       fillcolor="white", margin="0.2,0.12")
g.attr("edge", fontname="Helvetica", fontsize="11")

def box(name, label, color):
    c = Digraph(name="cluster_" + name)
    c.attr(label=label, style="rounded,filled", fillcolor=color, fontsize="16", fontname="Helvetica-Bold")
    return c

c = box("a", "1. Ingress & Security", "#E8F0FE")
c.node("in", "Client Request")
c.node("pre", "Pre-Execution Plugins\nPII Redactor → Injection Classifier", fillcolor="#FFF3CD")
c.node("cache", "Redis Semantic Cache\n(tenant-isolated)", fillcolor="#F8D7DA")
g.subgraph(c)

c = box("b", "2. Triage & Routing", "#E6F4EA")
c.node("tri", "Triage Agent\ntemp=0.0, ≤25 ms", fillcolor="#D4EDDA")
c.node("direct", "Direct Response")
c.node("retr", "Retrieval Subagent")
c.node("exe", "Execution Subagent")
c.node("aud", "Audit Subagent")
g.subgraph(c)

c = box("c", "3. MCP Servers (JSON-RPC)", "#FFF4E5")
c.node("rag", "mcp-hybrid-rag\nBM25 + Bi-Encoder → RRF → Cross-Encoder", fillcolor="#FFE0B2")
c.node("gv", "mcp-graphify-vault", fillcolor="#FFE0B2")
c.node("st", "mcp-state-store\n(Postgres)", fillcolor="#FFE0B2")
g.subgraph(c)

c = box("d", "4. Control & Egress", "#F3E8FD")
c.node("react", "Bounded ReAct Loop\nmax_steps=5")
c.node("cb", "Circuit Breaker", fillcolor="#F8D7DA")
c.node("hitl", "Tier 3 HITL Gate", fillcolor="#FFF3CD")
c.node("post", "Post-Execution\nRAGAS & OTel Export", fillcolor="#D4EDDA")
c.node("out", "Response", fillcolor="#CCE5FF")
g.subgraph(c)

for a, b, kw in [
    ("in", "pre", {}), ("pre", "cache", {}), ("cache", "tri", {"label": "miss"}),
    ("tri", "direct", {}), ("tri", "retr", {}), ("tri", "exe", {}), ("tri", "aud", {}),
    ("retr", "rag", {}), ("retr", "gv", {}), ("exe", "st", {}), ("aud", "st", {}),
    ("rag", "react", {}), ("gv", "react", {}), ("st", "react", {}), ("direct", "react", {}),
    ("react", "cb", {}), ("cb", "hitl", {}), ("hitl", "post", {}), ("post", "out", {}),
]:
    g.edge(a, b, **kw)
g.edge("cache", "out", xlabel="hit", color="green", constraint="false")

g.render(out, cleanup=True)
print("wrote", out + ".png")
