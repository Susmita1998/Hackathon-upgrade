"""Small-scale async load smoke test (NOT the 50K-VU test). Usage: python3 -I load_tests/smoke_load.py --vus 200 --seconds 20 [--url http://127.0.0.1:8000]"""
import argparse, asyncio, json, random, statistics, sys, time
from pathlib import Path
import httpx

ap = argparse.ArgumentParser(); ap.add_argument("--vus", type=int, default=200); ap.add_argument("--seconds", type=int, default=20)
ap.add_argument("--url", default="http://127.0.0.1:8000"); ap.add_argument("--out", default="reports/load_smoke.json"); ap.add_argument("--varied", action="store_true")
a = ap.parse_args()
gold = [g["question"] for g in json.loads((Path(__file__).resolve().parent.parent / "data/golden_dataset.json").read_text()) if g["answerable"]]
lat, codes, leaks = [], {}, 0


async def vu(i, client, stop):
    global leaks
    tenant = f"tenant_{i % 50}"
    while time.time() < stop:
        q = random.choice(gold) if a.varied else "Verify batch LOT-88392 safety protocol and return triage status"
        t = time.perf_counter()
        try:
            r = await client.post("/api/v1/agent/query", json={"tenant_id": tenant, "user_id": f"user_{i}", "query": q}, headers={"X-Tenant-ID": tenant})
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
            lat.append((time.perf_counter() - t) * 1e3)
            if r.status_code == 200 and r.json()["tenant_id"] != tenant:
                leaks += 1
        except Exception:
            codes["exc"] = codes.get("exc", 0) + 1
        await asyncio.sleep(1)


async def main():
    limits = httpx.Limits(max_connections=a.vus + 10)
    async with httpx.AsyncClient(base_url=a.url, limits=limits, timeout=10) as c:
        stop = time.time() + a.seconds; t0 = time.time()
        await asyncio.gather(*[vu(i, c, stop) for i in range(a.vus)])
        dur = time.time() - t0
    lat.sort(); n = len(lat); pc = lambda p: lat[min(n - 1, int(p / 100 * (n - 1)))]
    bad = sum(v for k, v in codes.items() if k == "exc" or (isinstance(k, int) and k >= 500))
    out = {"vus": a.vus, "duration_s": a.seconds, "requests": n, "rps": n / dur, "p50_ms": pc(50), "p95_ms": pc(95), "p99_ms": pc(99),
           "rate_5xx_pct": 100 * bad / max(n, 1), "status_codes": {str(k): v for k, v in codes.items()}, "cross_tenant_leaks": leaks,
           "note": "client and server share one machine; Python client overhead inflates latency; NOT a 50K-VU result"}
    Path(a.out).parent.mkdir(exist_ok=True); Path(a.out).write_text(json.dumps(out, indent=1)); print(json.dumps(out, indent=1))

asyncio.run(main())
