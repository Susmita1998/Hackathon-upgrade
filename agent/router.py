"""ModelRouter: picks a model chain per role/risk/complexity, retries once with jitter, fails over to the next model,
cools down unhealthy models, and finally raises AllModelsFailed so the caller can use its deterministic local fallback."""
from __future__ import annotations
import asyncio, random, time
from . import config as C
from .openrouter import LLMError, LLMResult


class AllModelsFailed(Exception):
    def __init__(self, role: str, attempts: list):
        super().__init__(f"{role}: all models failed {attempts}"); self.role, self.attempts = role, attempts


class ModelRouter:
    def __init__(self, client, telemetry, pool: int = C.POOLS["llm"]):
        self.client, self.t = client, telemetry
        self.sem = asyncio.Semaphore(pool)                       # llm bulkhead (llm_pool=256)
        self.fail_streak: dict[str, int] = {}
        self.cool_until: dict[str, float] = {}

    @property
    def live(self) -> bool:
        return self.client is not None and self.client.available

    def chain(self, role: str, *, risk: int = 1, complexity: str = "low", avoid: str | None = None) -> list[str]:
        key = role
        if role == "generate" and (risk >= 3 or complexity == "high"):
            key = "generate_strong"
        models = list(C.MODEL_CHAINS[key])
        if avoid and len(models) > 1 and models[0] == avoid:        # independence: verifier should not be the generator
            models = models[1:] + models[:1]
        return models

    def _healthy(self, m: str) -> bool:
        return self.cool_until.get(m, 0) <= time.time()

    async def call(self, role: str, messages: list[dict], *, ctx=None, risk: int = 1, complexity: str = "low", avoid: str | None = None,
                   max_tokens: int = 400, stream: bool = False) -> LLMResult:
        if not self.live:
            raise AllModelsFailed(role, [("-", "no_key")])
        models = [m for m in self.chain(role, risk=risk, complexity=complexity, avoid=avoid)]
        models = [m for m in models if self._healthy(m)] or models[:1]         # never end up with an empty chain
        attempts: list = []
        for model in models:
            for attempt in range(C.LLM_RETRIES + 1):
                try:
                    async with self.sem:
                        res = await self.client.chat(model, messages, max_tokens=max_tokens, temperature=0.0, timeout=C.OR_TIMEOUTS_S.get(role, 20.0), stream=stream)
                    self.fail_streak[model] = 0
                    res.role, res.fallbacks = role, list(attempts)
                    self._record(ctx, res)
                    return res
                except LLMError as e:
                    attempts.append((model, e.kind))
                    self.t.incr(f"llm.err.{e.kind}")
                    if e.kind == "budget" or e.kind == "no_key":
                        raise AllModelsFailed(role, attempts)
                    if e.kind in ("rate_limit", "timeout", "server"):
                        self.fail_streak[model] = self.fail_streak.get(model, 0) + 1
                        if self.fail_streak[model] >= C.LLM_COOLDOWN_AFTER:
                            self.cool_until[model] = time.time() + C.LLM_COOLDOWN_S
                        if attempt < C.LLM_RETRIES:
                            await asyncio.sleep(random.uniform(0.2, 0.6) * (attempt + 1))   # jittered backoff, one retry
                            continue
                    elif e.kind == "blocked":
                        self.cool_until[model] = time.time() + 600
                    break                                                                    # next model in the chain
        self.t.incr("llm.all_failed")
        raise AllModelsFailed(role, attempts)

    def _record(self, ctx, res: LLMResult):
        self.t.observe(f"llm.{res.role}.ms", res.latency_ms)
        self.t.observe("llm.cost_usd", res.cost_usd)
        self.t.observe("llm.prompt_tokens", res.prompt_tokens); self.t.observe("llm.completion_tokens", res.completion_tokens)
        if res.ttft_ms is not None:
            self.t.observe("llm.ttft_ms", res.ttft_ms)
        if res.fallbacks:
            self.t.incr("llm.fallback_used")
        if ctx is not None:
            ctx.meta.setdefault("llm_calls", []).append({"role": res.role, "model": res.model, "ms": round(res.latency_ms, 1),
                "ttft_ms": None if res.ttft_ms is None else round(res.ttft_ms, 1), "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens, "cost_usd": round(res.cost_usd, 6), "fallbacks": res.fallbacks})
            ctx.trace.add(f"llm.{res.role}", res.latency_ms, model=res.model, cost=res.cost_usd, tokens=res.prompt_tokens + res.completion_tokens)
