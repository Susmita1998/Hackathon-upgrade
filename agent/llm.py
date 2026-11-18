"""Deterministic extractive generator (stand-in for vLLM / TensorRT-LLM).

Interface: generate(question, chunks, observations) -> (answer, cited_chunk_ids, ttft_ms).
Because it only copies sentences from retrieved evidence it cannot invent facts; a real LLM must be gated by
the Verification subagent (agent/verify.py) instead.
"""
from __future__ import annotations
import re, time
from .rag import tokens

SENT = re.compile(r"^- (.+)$", re.M)


def sentences(text: str) -> list[str]:
    s = SENT.findall(text)
    return s if s else [x.strip() for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]


def _score(q: set, st: set) -> float:
    return len(q & st) / max(len(q), 1)


class ExtractiveLLM:
    def generate(self, question: str, chunks: list[dict], observations: list[str]) -> tuple[str, list[str], float]:
        t0 = time.perf_counter()
        parts: list[str] = []
        cites: list[str] = []
        cands = [(ci, ch["chunk_id"], s, set(tokens(s))) for ci, ch in enumerate(chunks) for s in sentences(ch["text"])]
        cands = [c for c in cands if c[3]]
        if re.search(r"\bsummar", question, re.I) and chunks:
            parts, cites = sentences(chunks[0]["text"]), [chunks[0]["chunk_id"]]
        elif cands:
            q = set(tokens(question))
            ranked = sorted(cands, key=lambda c: (-_score(q, c[3]), c[0]))
            top = _score(q, ranked[0][3])
            if top > 0.05:
                primary = ranked[0][0]
                clauses = [c for c in re.split(r",|\band\b|\?", question) if len(tokens(c)) >= 2]
                if len(clauses) <= 1:
                    for c in ranked:
                        if _score(q, c[3]) >= 0.8 * top and c[2] not in parts and len(parts) < 2:
                            parts.append(c[2]); cites.append(c[1])
                else:                                         # one best sentence per clause, from the primary chunk
                    pool = [c for c in cands if c[0] == primary]
                    for clause in clauses:
                        cq = set(tokens(clause))
                        best = max(pool, key=lambda c: _score(cq, c[3]), default=None)
                        if best and _score(cq, best[3]) > 0.05 and best[2] not in parts:
                            parts.append(best[2]); cites.append(best[1])
                    if not parts:
                        parts.append(ranked[0][2]); cites.append(ranked[0][1])
        parts += list(observations)
        ttft = (time.perf_counter() - t0) * 1e3
        ans = " ".join(parts)
        if cites:
            ans += " " + " ".join(f"[{c}]" for c in dict.fromkeys(cites))
        return ans, list(dict.fromkeys(cites)), ttft


def compress_chunks(question: str, chunks: list[dict], keep_sentences: int = 4) -> list[dict]:
    """Send the LLM only the sentences that matter (drops headings, unrelated facts and weak chunks).

    Summaries need every sentence, so they are passed through unchanged.
    """
    if re.search(r"\bsummar", question, re.I) or not chunks:
        return chunks
    q = set(tokens(question))
    out, best_overall = [], 0.0
    scored = []
    for ch in chunks:
        sents = [(len(q & set(tokens(x))) / max(len(q), 1), x) for x in sentences(ch["text"])]
        top = max((sc for sc, _ in sents), default=0.0)
        scored.append((top, ch, sents)); best_overall = max(best_overall, top)
    for top, ch, sents in scored:
        if top < 0.5 * best_overall and len(chunks) > 2 and ch.get("chunk_hash") != "graph":
            continue
        keep = {x for _, x in sorted(sents, key=lambda t: -t[0])[:keep_sentences]}
        text = "\n".join(f"- {x}" for x in sentences(ch["text"]) if x in keep)
        out.append({**ch, "text": text})
    return out or chunks
