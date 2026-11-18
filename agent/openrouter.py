"""OpenRouter chat client: async, streaming (for TTFT), spend cap, error taxonomy. The API key is read from the environment only
and is never logged, stored, or included in exceptions."""
from __future__ import annotations
import asyncio, json, os, time, weakref
from dataclasses import dataclass, field
import httpx
from . import config as C

URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    """kind: rate_limit | timeout | server | blocked | invalid | budget | no_key | parse"""
    def __init__(self, kind: str, message: str = "", status: int | None = None):
        super().__init__(f"{kind}: {message}"); self.kind, self.status = kind, status


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    ttft_ms: float | None = None
    fallbacks: list = field(default_factory=list)       # [(model, error_kind)] tried before success
    role: str = ""


class OpenRouterClient:
    def __init__(self, api_key: str | None = None, budget_usd: float | None = None):
        self._key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY")
        self.budget = C.OR_BUDGET_USD if budget_usd is None else budget_usd
        self.spent = 0.0
        self.calls = 0
        self._clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def __repr__(self):                                   # never leak the key
        return f"OpenRouterClient(key={'set' if self._key else 'missing'}, spent=${self.spent:.4f}/{self.budget})"

    @property
    def available(self) -> bool:
        return bool(self._key)

    def _http(self) -> httpx.AsyncClient:                 # one client per running event loop (Streamlit calls asyncio.run repeatedly)
        loop = asyncio.get_running_loop()
        c = self._clients.get(loop)
        if c is None or c.is_closed:
            c = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0), limits=httpx.Limits(max_connections=64))
            self._clients[loop] = c
        return c

    async def chat(self, model: str, messages: list[dict], *, max_tokens: int = 400, temperature: float | None = 0.0,
                   timeout: float = 20.0, stream: bool = False) -> LLMResult:
        if not self._key:
            raise LLMError("no_key", "OPENROUTER_API_KEY not set")
        if self.spent >= self.budget:
            raise LLMError("budget", f"spend cap ${self.budget:.2f} reached")
        # hidden "reasoning" tokens ate the whole max_tokens budget on planner turns (empty output); the ReAct objective is explicit in the JSON anyway
        if model in C.MANDATORY_REASONING_MODELS:            # e.g. Sonnet 5.5: reasoning cannot be disabled -> keep it short and leave token headroom
            reasoning, max_tokens = {"effort": "low"}, max_tokens + 700
        else:
            reasoning = {"enabled": False}
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True}, "reasoning": reasoning}
        if temperature is not None:
            body["temperature"] = temperature
        if stream:
            body["stream"] = True
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "X-Title": "Unreliable Assistant"}
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(self._stream(body, headers) if stream else self._once(body, headers), timeout)
        except asyncio.TimeoutError:
            raise LLMError("timeout", f"{model} exceeded {timeout}s")
        except httpx.TimeoutException:
            raise LLMError("timeout", f"{model} http timeout")
        except httpx.TransportError as e:
            raise LLMError("server", f"transport error {type(e).__name__}")
        text, usage, ttft = res
        self.calls += 1
        cost = float(usage.get("cost") or 0.0)
        self.spent += cost
        return LLMResult(text=text, model=model, prompt_tokens=int(usage.get("prompt_tokens", 0)), completion_tokens=int(usage.get("completion_tokens", 0)),
                         cost_usd=cost, latency_ms=(time.perf_counter() - t0) * 1e3, ttft_ms=ttft)

    @staticmethod
    def _raise_for(status: int, body: str):
        low = body.lower()
        if status == 429:
            raise LLMError("rate_limit", "429", status)
        if status == 402:
            raise LLMError("budget", "insufficient credits", status)
        if status == 404 and ("guardrail" in low or "no endpoints" in low or "no longer available" in low or "deprecated" in low):
            raise LLMError("blocked", "model unavailable for this key/workspace", status)
        if status in (408, 504):
            raise LLMError("timeout", str(status), status)
        if status >= 500:
            raise LLMError("server", str(status), status)
        raise LLMError("invalid", f"HTTP {status}", status)

    async def _once(self, body, headers):
        r = await self._http().post(URL, json=body, headers=headers)
        if r.status_code != 200:
            self._raise_for(r.status_code, r.text)
        d = r.json()
        if "error" in d:
            self._raise_for(int(d["error"].get("code", 500) or 500), json.dumps(d["error"]))
        return d["choices"][0]["message"].get("content") or "", d.get("usage", {}), None

    async def _stream(self, body, headers):
        t0 = time.perf_counter(); ttft = None; parts = []; usage = {}
        async with self._http().stream("POST", URL, json=body, headers=headers) as r:
            if r.status_code != 200:
                self._raise_for(r.status_code, (await r.aread()).decode(errors="ignore"))
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    d = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if "error" in d:
                    self._raise_for(int(d["error"].get("code", 500) or 500), json.dumps(d["error"]))
                if d.get("usage"):
                    usage = d["usage"]
                for ch in d.get("choices", []):
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        if ttft is None:
                            ttft = (time.perf_counter() - t0) * 1e3
                        parts.append(piece)
        return "".join(parts), usage, ttft
