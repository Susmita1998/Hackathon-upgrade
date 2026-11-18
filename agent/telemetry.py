"""Lightweight OpenTelemetry-style tracing: W3C traceparent, named spans, percentile metrics.

Spans are kept in-process and optionally appended to reports/traces.jsonl (OTLP-shaped JSON).
Swap `Telemetry.export` for an OTLP gRPC exporter pointed at the collector (localhost:4317) in production.
"""
from __future__ import annotations
import json, os, re, secrets, threading, time
from collections import defaultdict, deque
from contextlib import contextmanager

_TP = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def pct(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


class Trace:
    def __init__(self, traceparent: str | None = None):
        m = _TP.match(traceparent or "")
        self.trace_id = m.group(1) if m else secrets.token_hex(16)
        self.parent_span = m.group(2) if m else None
        self.spans: list[dict] = []
        self.t0 = time.perf_counter()

    @contextmanager
    def span(self, name: str, **attrs):
        sid = secrets.token_hex(8)
        t = time.perf_counter()
        rec = {"name": name, "span_id": sid, "trace_id": self.trace_id, "parent": self.parent_span, "attrs": attrs}
        try:
            yield rec
        finally:
            rec["ms"] = (time.perf_counter() - t) * 1e3
            rec["start_ms"] = (t - self.t0) * 1e3
            self.spans.append(rec)

    def add(self, name: str, ms: float, **attrs):
        self.spans.append({"name": name, "span_id": secrets.token_hex(8), "trace_id": self.trace_id, "parent": self.parent_span,
                           "attrs": attrs, "ms": ms, "start_ms": (time.perf_counter() - self.t0) * 1e3})

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-01"


class Telemetry:
    def __init__(self, max_samples: int = 50000):
        self.hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_samples))
        self.counters: dict[str, float] = defaultdict(float)
        self.lock = threading.Lock()
        self.export_path = os.environ.get("TRACE_EXPORT_PATH")
        self.recent: deque = deque(maxlen=200)

    def observe(self, name: str, value: float):
        with self.lock:
            self.hist[name].append(value)

    def incr(self, name: str, n: float = 1):
        with self.lock:
            self.counters[name] += n

    def finish(self, trace: Trace, **attrs):
        with self.lock:
            for s in trace.spans:
                self.hist[f"span:{s['name']}"].append(s["ms"])
            self.recent.append({"trace_id": trace.trace_id, "spans": trace.spans, **attrs})
        if self.export_path:
            with open(self.export_path, "a") as f:
                f.write(json.dumps({"trace_id": trace.trace_id, "spans": trace.spans, **attrs}, default=str) + "\n")

    def p(self, name: str, q: float) -> float:
        with self.lock:
            return pct(list(self.hist.get(name, [])), q)

    def summary(self) -> dict:
        with self.lock:
            return {k: {"n": len(v), "p50": pct(list(v), 50), "p95": pct(list(v), 95), "p99": pct(list(v), 99)} for k, v in self.hist.items()} | \
                   {"counters": dict(self.counters)}
