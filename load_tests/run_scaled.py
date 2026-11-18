"""Orchestrates the scaled-down k6 load test (offline stub mode: LLM_MODE=off, never the live LLM).

  1. capacity: ONE uvicorn worker pinned to core 0, k6 pinned to core 3, closed loop (no think time) -> max RPS per core for cache-miss and cache-hit traffic
  2. scaling:  N uvicorn workers pinned to cores 0..N-1, k6 on the last core, VU ladder with the blueprint's sleep(1) pacing -> latency/error/throughput vs load
Writes reports/load_k6_scaled.json.   Usage: python3 -I load_tests/run_scaled.py [--levels 250,500,1000,2000,3000] [--workers 3] [--hold 30]
"""
import argparse, json, os, signal, subprocess, sys, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser(); ap.add_argument("--levels", default="250,500,1000,2000,3000"); ap.add_argument("--workers", type=int, default=3)
ap.add_argument("--hold", type=int, default=30); ap.add_argument("--ramp", type=int, default=10); ap.add_argument("--skip-capacity", action="store_true"); ap.add_argument("--hit-capacity-only", action="store_true")
a = ap.parse_args()
K6 = os.path.expanduser("~/bin/k6"); PORT = 8000; NCORES = os.cpu_count() or 4
ENV = {**os.environ, "LLM_MODE": "off", "OPENROUTER_API_KEY": "", "MCP_TIMEOUT_S": "0.150"}   # offline: no key, no spend


def start_server(workers: int, cores: str):
    cmd = ["taskset", "-c", cores, sys.executable, "-I", "-m", "uvicorn", "agent.api:app", "--port", str(PORT), "--workers", str(workers), "--log-level", "warning"]
    p = subprocess.Popen(cmd, cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(90):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
                time.sleep(1.5); return p
        except Exception:
            time.sleep(1)
    raise RuntimeError("server did not start")


def stop(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM); p.wait(timeout=20)
    except Exception:
        pass
    time.sleep(2)


def k6(profile: str, vus: int, hold: int, ramp: int, core: str, tag: str) -> dict:
    out = ROOT / f"reports/_k6_{tag}.json"
    cmd = ["taskset", "-c", core, K6, "run", "--quiet", f"--summary-export={out}", "-e", f"VUS={vus}", "-e", f"PROFILE={profile}", "-e", f"HOLD={hold}", "-e", f"RAMP={ramp}",
           "-e", f"BASE_URL=http://127.0.0.1:{PORT}", str(ROOT / "load_tests/k6_scaled_load.js")]
    t = time.time(); r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True); dur = time.time() - t
    m = json.loads(out.read_text())["metrics"]; d = m["http_req_duration"]
    checks = m.get("checks", {}); reqs = m["http_reqs"]
    total = reqs["count"]; failed = m.get("http_req_failed", {})
    res = {"profile": profile, "vus": vus, "requests": total, "rps": reqs["rate"], "p50_ms": d["med"], "p90_ms": d["p(90)"], "p95_ms": d["p(95)"], "p99_ms": d["p(99)"],
           "avg_ms": d["avg"], "max_ms": d["max"], "failed_pct": 100 * failed.get("value", failed.get("rate", 0)), "checks_pct": 100 * checks.get("value", checks.get("rate", 0)),
           "wall_s": round(dur, 1), "sla_met": d["p(95)"] <= 450 and d["p(99)"] <= 600 and d["med"] <= 250}
    out.unlink(missing_ok=True)
    print(f"  {profile:9} vus={vus:5} rps={res['rps']:7.1f} p50={res['p50_ms']:8.1f} p95={res['p95_ms']:8.1f} p99={res['p99_ms']:8.1f} ms  failed={res['failed_pct']:.2f}%  SLA={'MET' if res['sla_met'] else 'MISSED'}", flush=True)
    return res


if a.hit_capacity_only:
    srv = start_server(1, "0")
    try:
        hit = k6("closed_hit", 64, 20, 5, "3", "cap_hit_closed")
    finally:
        stop(srv)
    f = ROOT / "reports/load_k6_scaled.json"; doc = json.loads(f.read_text())
    doc["capacity"]["cache_hit_closed_loop"] = hit; doc["capacity"].pop("cache_hit_blueprint_query", None)
    f.write_text(json.dumps(doc, indent=1)); print("updated capacity block"); sys.exit(0)

out = {"env": {"cores": NCORES, "python": sys.version.split()[0], "k6": subprocess.run([K6, "version"], capture_output=True, text=True).stdout.strip(), "llm": "off (offline stub mode)",
               "note": "server and k6 share one 4-core machine, pinned to separate cores; fakeredis/SQLite are per-worker (no shared cache), so hit ratios are per-worker"}}
if not a.skip_capacity:
    print("== capacity: 1 uvicorn worker on core 0, k6 on core 3, closed loop (no sleep)", flush=True)
    srv = start_server(1, "0")
    try:
        out["capacity"] = {"cache_miss_closed_loop": k6("closed", 64, 20, 5, "3", "cap_miss"), "cache_hit_blueprint_query": None}
        k = k6("blueprint", 64, 20, 5, "3", "cap_hit")           # blueprint query keeps hitting the per-user cache (still paced by sleep(1)); upper bound check below
        out["capacity"]["cache_hit_blueprint_query"] = k
    finally:
        stop(srv)
levels = [int(x) for x in a.levels.split(",")]
workers = a.workers; last = NCORES - 1
for profile in ("blueprint", "miss"):
    print(f"== scaling ladder, profile={profile}, {workers} workers on cores 0-{workers-1}, k6 on core {last}", flush=True)
    rows = []
    for v in levels:
        srv = start_server(workers, ",".join(map(str, range(workers))))
        try:
            rows.append(k6(profile, v, a.hold, a.ramp, str(last), f"{profile}_{v}"))
        finally:
            stop(srv)
    out.setdefault("ladder", {})[profile] = rows
out["config"] = {"levels": levels, "workers": workers, "hold_s": a.hold, "ramp_s": a.ramp}
(ROOT / "reports/load_k6_scaled.json").write_text(json.dumps(out, indent=1))
print("wrote reports/load_k6_scaled.json")
