"""Graphify subgraph pruning over Obsidian wikilinks (Gate 17)."""
from __future__ import annotations
import json, re
from . import config as C
from .rag import tokens, load_vault

WIKI = re.compile(r"\[\[([^\]]+)\]\]")


def approx_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.3))


class Graphify:
    def __init__(self, graph_path=C.GRAPH_PATH, vault=C.VAULT_DIR):
        g = json.loads(graph_path.read_text())
        self.nodes = {n["id"]: n for n in g["nodes"]}
        self.adj: dict[str, set[str]] = {}
        for e in g["edges"]:
            self.adj.setdefault(e["src"], set()).add(e["dst"]); self.adj.setdefault(e["dst"], set()).add(e["src"])
        self.notes = {n["id"]: n for n in load_vault(vault)}
        self.title_tokens = {i: set(tokens(n.get("title", ""))) for i, n in self.nodes.items() if n["type"] == "policy"}

    def resolve_anchor(self, query: str) -> str | None:
        q = set(tokens(query))
        best, score = None, 0.0
        for nid, tt in self.title_tokens.items():
            if tt and tt <= q and len(tt) > score:
                best, score = nid, len(tt)
        return best

    def subgraph(self, anchor: str, depth: int = 2, max_tokens: int = 320) -> dict:
        anchor = anchor if anchor in self.nodes else (self.resolve_anchor(anchor) or "")
        if not anchor:
            return {"anchor": None, "nodes": [], "items": [], "text": "", "tokens": 0}
        seen, frontier, order = {anchor}, [anchor], [anchor]
        for _ in range(depth):
            nxt = []
            for n in frontier:
                for m in sorted(self.adj.get(n, ())):
                    if m not in seen and m in self.notes:
                        seen.add(m); nxt.append(m); order.append(m)
            frontier = nxt
        parts, used, items = [], 0, []
        for nid in order:                      # compact: facts only, no frontmatter
            body = self.notes[nid]["body"]
            t = approx_tokens(body)
            if used + t > max_tokens:
                body = " ".join(body.split()[: max(0, int((max_tokens - used) / 1.3))])
                t = approx_tokens(body) if body else 0
            if body:
                parts.append(body); used += t; items.append({"id": nid, "text": body})
            if used >= max_tokens:
                break
        return {"anchor": anchor, "nodes": order, "items": items, "text": "\n".join(parts), "tokens": used}

    def raw_dump_tokens(self, query: str, k: int = 8) -> int:
        """Baseline: naive top-k whole-note dump (what the prototype did)."""
        return sum(approx_tokens(n["body"]) for n in list(self.notes.values())[:k])
