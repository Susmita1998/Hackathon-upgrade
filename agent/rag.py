"""3-stage hybrid RAG (Gate 3): BM25 -> Bi-Encoder/FAISS-HNSW -> RRF -> Cross-Encoder.

Backends here are CPU stand-ins with the same interfaces the ONNX models would use:
  * Embedder  : feature-hashing bag-of-ngrams (stand-in for bge-large-en-v1.5 ONNX)
  * Reranker  : IDF-weighted lexical coverage + cosine (stand-in for bge-reranker-v2-m3 FP16)
Swap by implementing `embed(texts)` / `score(query, chunks)`.
"""
from __future__ import annotations
import hashlib, math, re, time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import faiss, numpy as np
from nltk.stem import PorterStemmer
from rank_bm25 import BM25Okapi
from . import config as C
from .models import Chunk

_STEM = PorterStemmer()
STOP = set("a an and are as at be by for from has have how i in is it its of on or our that the their there this to was were what when where which who whom why will with you your do does did can could should would may might about into over under than then so if we us my me please tell give show".split())
_TOK = re.compile(r"[a-z0-9]+")


@lru_cache(maxsize=65536)
def _stem(w: str) -> str:
    return _STEM.stem(w)


def tokens(text: str, drop_stop: bool = True) -> list[str]:
    out = []
    for w in _TOK.findall(text.lower()):
        if drop_stop and w in STOP:
            continue
        out.append(_stem(w))
    return out


def sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def load_vault(vault: Path) -> list[dict]:
    notes = []
    for p in sorted(vault.glob("*.md")):
        raw = p.read_text()
        meta, body = {}, raw
        if raw.startswith("---"):
            _, fm, body = raw.split("---", 2)
            for line in fm.strip().splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        notes.append({"id": meta.get("id", p.stem), "domain": meta.get("domain", ""), "risk_tier": meta.get("risk_tier", ""),
                      "version": meta.get("version", ""), "body": body.strip(), "path": str(p)})
    return notes


def chunk_note(note: dict) -> list[Chunk]:
    words = note["body"].split()
    if len(words) <= C.CHUNK_TOKENS:                       # short atomic note = one chunk, newlines preserved
        text = note["body"]
        return [Chunk(chunk_id=f"{note['id']}#0", chunk_hash=sha(text), text=text, source_uri=f"rag://chunks/{note['id']}#0")]
    step = C.CHUNK_TOKENS - C.CHUNK_OVERLAP
    chunks = []
    for i, start in enumerate(range(0, max(len(words), 1), step)):
        text = " ".join(words[start:start + C.CHUNK_TOKENS])
        chunks.append(Chunk(chunk_id=f"{note['id']}#{i}", chunk_hash=sha(text), text=text, source_uri=f"rag://chunks/{note['id']}#{i}"))
        if start + C.CHUNK_TOKENS >= len(words):
            break
    return chunks


class HashEmbedder:
    dim = 512

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for r, t in enumerate(texts):
            toks = tokens(t)
            feats = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
            for f in feats:
                h = int(hashlib.md5(f.encode()).hexdigest()[:8], 16)
                out[r, h % self.dim] += 1.0 if (h >> 31) & 1 else -1.0
            n = np.linalg.norm(out[r])
            if n:
                out[r] /= n
        return out


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class LexicalCrossEncoder:
    """Joint query/chunk scorer returning a calibrated 0-1 relevance (threshold 0.42)."""
    def __init__(self, idf: dict[str, float]):
        self.idf = idf
        self.cache: dict = {}; self.cache_set: dict = {}; self.cache_bi: dict = {}

    def score(self, query: str, chunks: list[Chunk], cosines: dict[str, float]) -> list[float]:
        qtl = tokens(query)
        q = set(qtl)
        if not q:
            return [0.0] * len(chunks)
        mean_idf = sum(self.idf.values()) / max(len(self.idf), 1)
        known = [t for t in q if t in self.idf]
        unseen = len(q) - len(known)
        denom = sum(self.idf[t] for t in known) + 0.35 * mean_idf * unseen   # OOV words dilute only mildly
        if denom <= 0:
            return [0.0] * len(chunks)
        out = []
        for ch in chunks[:32][:C.RERANK_MAX_LEN]:
            ctl = self.cache.get(ch.chunk_hash) or self.cache.setdefault(ch.chunk_hash, tokens(ch.text))   # chunk tokens are static
            ct = self.cache_set.get(ch.chunk_hash) or self.cache_set.setdefault(ch.chunk_hash, set(ctl))
            cov = sum(self.idf[t] for t in known if t in ct) / denom
            qb = set(zip(qtl, qtl[1:]))
            cb = self.cache_bi.get(ch.chunk_hash) or self.cache_bi.setdefault(ch.chunk_hash, set(zip(ctl, ctl[1:])))
            cov2 = len(qb & cb) / len(qb) if qb else 0.0
            cos = max(0.0, cosines.get(ch.chunk_hash, 0.0))
            out.append(_sigmoid(14.0 * (0.68 * cov + 0.14 * cov2 + 0.18 * cos - 0.50)))
        # batch>32 handled by repeated calls in the pipeline
        return out


@dataclass
class StageResult:
    bm25: list[tuple[str, float]]
    dense: list[tuple[str, float]]
    fused: list[tuple[str, float]]
    reranked: list[tuple[str, float]]
    ms: dict


class RagIndex:
    def __init__(self, notes: list[dict], extra_chunks: list[Chunk] | None = None, embedder=None):
        self.embedder = embedder or HashEmbedder()
        self.chunks: list[Chunk] = [c for n in notes for c in chunk_note(n)] + list(extra_chunks or [])
        self.by_hash = {c.chunk_hash: c for c in self.chunks}
        self.ctoks = [tokens(c.text) for c in self.chunks]
        self.bm25 = BM25Okapi(self.ctoks, k1=C.BM25_K1, b=C.BM25_B)
        df: dict[str, int] = {}
        for toks in self.ctoks:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n = len(self.chunks)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        vecs = self.embedder.embed([c.text for c in self.chunks])
        self.vecs = vecs
        self.faiss = faiss.IndexHNSWFlat(vecs.shape[1], C.HNSW_M, faiss.METRIC_INNER_PRODUCT)
        self.faiss.hnsw.efSearch = C.HNSW_EF_SEARCH
        self.faiss.add(vecs)
        self.reranker = LexicalCrossEncoder(self.idf)

    # --- stages ---
    def stage1_bm25(self, query: str, top_k: int):
        scores = self.bm25.get_scores(tokens(query))
        idx = np.argsort(-scores)[:top_k]
        return [(self.chunks[i].chunk_hash, float(scores[i])) for i in idx if scores[i] > 0]

    def stage2_dense(self, query: str, top_k: int):
        q = self.embedder.embed([query])
        k = min(top_k, len(self.chunks))
        sims, ids = self.faiss.search(q, k)
        return [(self.chunks[i].chunk_hash, float(s)) for s, i in zip(sims[0], ids[0]) if i >= 0]

    @staticmethod
    def rrf(lists: list[list[tuple[str, float]]], k: int, pool: int):
        fused: dict[str, float] = {}
        for lst in lists:
            for rank, (h, _) in enumerate(lst, start=1):
                fused[h] = fused.get(h, 0.0) + 1.0 / (k + rank)    # dict key == dedup by chunk_hash
        return sorted(fused.items(), key=lambda kv: -kv[1])[:pool]

    def retrieve(self, query: str, top_k_sparse=C.BM25_TOP_K, top_k_dense=C.DENSE_TOP_K, rrf_k=C.RRF_K,
                 rerank_k=C.RERANK_K, threshold=C.RERANK_THRESHOLD, chunk_filter=None) -> tuple[list[Chunk], StageResult]:
        t0 = time.perf_counter()
        s1 = self.stage1_bm25(query, top_k_sparse); t1 = time.perf_counter()
        s2 = self.stage2_dense(query, top_k_dense); t2 = time.perf_counter()
        fused = self.rrf([s1, s2], rrf_k, C.RRF_POOL); t3 = time.perf_counter()
        cos = dict(s2)
        pool = [self.by_hash[h] for h, _ in fused]
        scores: list[float] = []
        for i in range(0, len(pool), 32):                     # micro-batches of 32
            scores += self.reranker.score(query, pool[i:i + 32], cos)
        ranked = sorted(zip(pool, scores), key=lambda x: -x[1])
        t4 = time.perf_counter()
        out = []
        for ch, sc in ranked:
            if sc < threshold:
                break
            if chunk_filter and not chunk_filter(ch):
                continue                                       # quarantined (indirect injection)
            out.append(ch.model_copy(update={"score": round(sc, 4)}))
            if len(out) >= rerank_k:
                break
        res = StageResult(s1, s2, fused, [(c.chunk_hash, c.score) for c in out],
                          {"bm25": (t1 - t0) * 1e3, "dense": (t2 - t1) * 1e3, "rrf": (t3 - t2) * 1e3, "rerank": (t4 - t3) * 1e3})
        return out, res
