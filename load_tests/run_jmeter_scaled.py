"""Runs the real JMeter plan (load_tests/jmeter_50k_plan.jmx) in non-GUI mode at scaled thread counts against the offline-mode API.

Server: N uvicorn workers pinned to cores 0..N-1 (LLM_MODE=off, no key, no spend). JMeter JVM pinned to the last core.
Pacing: Constant Throughput Timer set to 60 samples/min/thread (= the same 1 request/s per VU as the k6 script and the blueprint's sleep(1)).
Stats come from the JTL after dropping the ramp-up window.   Usage: python3 -I load_tests/run_jmeter_scaled.py [--levels 250,500,1000,2000] [--hold 30] [--ramp 10]
"""
import argparse, csv, json, os, signal, statistics, subprocess, sys, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser(); ap.add_argument("--levels", default="250,500,1000,2000"); ap.add_argument("--workers", type=int, default=3)
ap.add_argument("--hold", type=int, default=30); ap.add_argument("--ramp", type=int, default=10); ap.add_argument("--out", default="reports/load_jmeter_scaled.json")
a = ap.parse_args()
JM = os.path.expanduser("~/tools/apache-jmeter-5.6.3/bin/jmeter"); PORT = 8000; NCORES = os.cpu_count() or 4
ENV = {**os.environ, "LLM_MODE": "off", "OPENROUTER_API_KEY": ""}


def start_server(workers):
    p = subprocess.Popen(["taskset", "-c", ",".join(map(str, range(workers))), sys.executable, "-I", "-m", "uvicorn", "agent.api:app", "--port", str(PORT), "--workers", str(workers), "--log-level", "warning"],
                         cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
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


def pct(v, q):
    v = sorted(v); return v[min(len(v) - 1, int(round(q / 100 * (len(v) - 1))))] if v else float("nan")


def run(threads: int) -> dict:
    jtl = ROOT / f"reports/_jm_{threads}.jtl"; jtl.unlink(missing_ok=True)
    total = a.ramp + a.hold
    env = {**os.environ, "JVM_ARGS": "-Xms512m -Xmx3g", "HEAP": "-Xms512m -Xmx3g"}
    cmd = ["taskset", "-c", str(NCORES - 1), JM, "-n", "-t", str(ROOT / "load_tests/jmeter_50k_plan.jmx"), "-l", str(jtl), "-Jhost=127.0.0.1", f"-Jport={PORT}", f"-Jthreads={threads}",
           f"-Jramp={a.ramp}", f"-Jduration={total}", "-Jrpm=60", "-Jjmeter.save.saveservice.output_format=csv", "-Jjmeter.save.saveservice.timestamp_format=ms"]
    t = time.time(); r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env); wall = time.time() - t
    if not jtl.exists():
        raise RuntimeError("no JTL produced:\n" + (r.stdout + r.stderr)[-1500:])
    rows = list(csv.DictReader(open(jtl)))
    t0 = min(int(x["timeStamp"]) for x in rows)
    steady = [x for x in rows if int(x["timeStamp"]) - t0 >= a.ramp * 1000] or rows
    el = [int(x["elapsed"]) for x in steady]
    http_err = sum(x["responseCode"] != "200" for x in steady)
    assert_fail = sum(x["success"] == "false" and x["responseCode"] == "200" for x in steady)
    span = (max(int(x["timeStamp"]) for x in steady) - min(int(x["timeStamp"]) for x in steady)) / 1000 or 1
    msgs = {}
    for x in steady:
        if x["success"] == "false":
            k = (x["failureMessage"] or x["responseMessage"] or "")[:90]; msgs[k] = msgs.get(k, 0) + 1
    res = {"threads": threads, "requests_steady": len(steady), "requests_total": len(rows), "rps": len(steady) / span, "p50_ms": pct(el, 50), "p90_ms": pct(el, 90), "p95_ms": pct(el, 95),
           "p99_ms": pct(el, 99), "avg_ms": statistics.mean(el), "max_ms": max(el), "http_error_pct": 100 * http_err / len(steady), "assertion_fail_pct": 100 * assert_fail / len(steady),
           "failure_messages": dict(sorted(msgs.items(), key=lambda kv: -kv[1])[:4]), "wall_s": round(wall, 1), "sla_met": pct(el, 50) <= 250 and pct(el, 95) <= 450 and pct(el, 99) <= 600}
    jtl.unlink(missing_ok=True)
    print(f"  jmeter threads={threads:5} rps={res['rps']:7.1f} p50={res['p50_ms']:7.1f} p95={res['p95_ms']:8.1f} p99={res['p99_ms']:8.1f} ms  http_err={res['http_error_pct']:.2f}%  assertion_fail={res['assertion_fail_pct']:.2f}%  SLA={'MET' if res['sla_met'] else 'MISSED'}  {res['failure_messages'] or ''}", flush=True)
    return res


rows = []
for lv in [int(x) for x in a.levels.split(",")]:
    srv = start_server(a.workers)
    try:
        rows.append(run(lv))
    finally:
        stop(srv)
Path(ROOT / a.out).write_text(json.dumps({"env": {"jmeter": "5.6.3", "java": subprocess.run(["java", "-version"], capture_output=True, text=True).stderr.split("\n")[0], "cores": NCORES, "workers": a.workers,
        "llm": "off (offline stub mode)", "pacing": "Constant Throughput Timer 60/min/thread", "ramp_s": a.ramp, "hold_s": a.hold, "note": "stats exclude the ramp window; JMeter and server share one 4-core machine, pinned to separate cores"},
        "ladder": rows}, indent=1))
print("wrote", a.out)
