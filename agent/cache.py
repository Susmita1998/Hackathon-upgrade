"""Tenant-isolated semantic cache on Redis (fakeredis here; point REDIS_URL at a real cluster in production).

Key = {tenant}:cache:SHA256(tenant_id:user_id:norm_query); TTL = 300s +/- 45s jitter (ADR-03).
"""
from __future__ import annotations
import hashlib, json, os, random, re
import numpy as np
from . import config as C
from .rag import HashEmbedder


def norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower()).rstrip("?.! ")


def cache_key(tenant_id: str, user_id: str, query: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}:{user_id}:{norm_query(query)}".encode()).hexdigest()
    return f"{tenant_id}:cache:{digest}"


def make_redis():
    url = os.environ.get("REDIS_URL")
    if url:
        import redis
        return redis.Redis.from_url(url, decode_responses=True)
    import fakeredis
    return fakeredis.FakeRedis(decode_responses=True)


class SemanticCache:
    def __init__(self, r=None, embedder=None):
        self.r = r or make_redis()
        self.emb = embedder or HashEmbedder()
        self.semantic = False                 # runbook toggle: "aggressive semantic cache"
        self.up = True
        self.hits = self.misses = 0

    def _idx(self, tenant, user):
        return f"{tenant}:semidx:{hashlib.sha256(user.encode()).hexdigest()[:16]}"

    def get(self, tenant, user, query):
        if not self.up:
            return None
        try:
            key = cache_key(tenant, user, query)
            raw = self.r.get(key)
            if raw is None and self.semantic:
                q = self.emb.embed([norm_query(query)])[0]
                for item in self.r.lrange(self._idx(tenant, user), 0, 199):
                    d = json.loads(item)
                    if float(np.dot(q, np.array(d["v"], dtype="float32"))) >= C.SEMANTIC_CACHE_COSINE:
                        raw = self.r.get(d["k"])
                        if raw:
                            break
            if raw is None:
                self.misses += 1
                return None
            self.hits += 1
            return json.loads(raw)
        except Exception:                                  # Redis down -> bypass (FMEA #1)
            return None

    def put(self, tenant, user, query, payload: dict):
        if not self.up:
            return
        try:
            key = cache_key(tenant, user, query)
            ttl = C.CACHE_TTL_S + random.randint(-C.CACHE_TTL_JITTER_S, C.CACHE_TTL_JITTER_S)
            self.r.set(key, json.dumps(payload), ex=ttl)
            if self.semantic:
                v = self.emb.embed([norm_query(query)])[0].tolist()
                idx = self._idx(tenant, user)
                self.r.lpush(idx, json.dumps({"k": key, "v": v})); self.r.ltrim(idx, 0, 199); self.r.expire(idx, C.CACHE_TTL_S * 2)
        except Exception:
            pass

    @property
    def hit_ratio(self):
        n = self.hits + self.misses
        return self.hits / n if n else 0.0


class HotMemory:
    """Sliding window of the last 3 turns per (tenant, session) in Redis/RAM (Gate 5). Only redacted text is stored; keys are tenant-scoped."""
    TURNS = 3

    def __init__(self, r):
        self.r = r
        self.up = True

    @staticmethod
    def key(tenant: str, session: str) -> str:
        return f"{tenant}:hot:{session}"

    def load(self, tenant: str, session: str) -> list[dict]:
        if not self.up:
            return []
        try:
            return [json.loads(x) for x in reversed(self.r.lrange(self.key(tenant, session), 0, self.TURNS - 1))]   # oldest first
        except Exception:
            return []

    def push(self, tenant: str, session: str, turn: dict) -> None:
        if not self.up:
            return
        try:
            k = self.key(tenant, session)
            self.r.lpush(k, json.dumps(turn)); self.r.ltrim(k, 0, self.TURNS - 1); self.r.expire(k, 3600)
        except Exception:
            pass
