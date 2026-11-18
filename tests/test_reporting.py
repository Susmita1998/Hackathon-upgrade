"""Live-results view model, evidence passthrough for RAGAS, and the workbench Tab 6 (offline)."""
from conftest import ask
from agent.reporting import RAGAS_GATES, live_rows
from agent.service import AgentService

LLM = {"answerable": {"refusal_rate": 0.02, "relevant": 1.0, "correct": 0.97}, "triage": {"llm_accuracy": 1.0}, "latency_ms": {"triage_p95": 1900, "ttft_p95": 900},
       "mean_steps_ok": 2.1, "faithfulness_llm_verifier": 1.0, "abstention_accuracy": 1.0, "cost": {"per_1k_queries_usd": 2.8},
       "red_team": {"direct_jailbreak": [20, 20], "indirect_injection_quarantine_on": [20, 20], "multi_turn_crescendo": [4, 4], "pii_smuggling": [20, 20]}}
RAGAS = {"run": {"ragas_version": "0.4.4"}, "scores": {m: {"mean": 0.95, "n": 10, "gate": g, "pass": True} for m, g in RAGAS_GATES.items()}}


def test_live_rows_pass_fail_logic():
    rows = {r["id"]: r for r in live_rows(LLM, None)}
    assert rows["KPI-03"]["status"] == "PASS" and rows["KPI-05"]["status"] == "FAIL" and rows["KPI-25"]["status"] == "FAIL"
    assert rows["KPI-29"]["status"] == "FAIL" and rows["KPI-06"]["status"] == "FAIL" and rows["KPI-17"]["status"] == "PASS"


def test_real_ragas_replaces_the_in_pipeline_proxy():
    without = [r["metric"] for r in live_rows(LLM, None) if r["id"] in ("KPI-20", "KPI-21")]
    with_ragas = [r for r in live_rows(LLM, RAGAS) if r["id"] in ("KPI-20", "KPI-21")]
    assert any("not RAGAS" in m for m in without)
    assert len(with_ragas) == 2 and all("ragas library" in r["metric"] and r["status"] == "PASS" for r in with_ragas)
    assert {r["id"] for r in live_rows(LLM, RAGAS)} >= {"KPI-11", "KPI-20", "KPI-21", "KPI-22", "gate"}


def test_ragas_below_gate_is_reported_as_fail():
    bad = {**RAGAS, "scores": {**RAGAS["scores"], "faithfulness": {"mean": 0.80, "n": 10, "gate": 0.92, "pass": False}}}
    assert [r for r in live_rows(LLM, bad) if r["id"] == "KPI-20"][0]["status"] == "FAIL"


def test_evidence_is_only_returned_when_enabled():
    s = AgentService()
    assert ask(s, "What is the sample size for Fire Drill?").evidence == []
    s2 = AgentService(); s2.return_evidence = True
    r = ask(s2, "What is the sample size for Fire Drill?")
    assert r.evidence and "63 units" in r.evidence[0]["text"] and r.evidence[0]["chunk_id"] == "fire-drill#0"


def test_workbench_tab6_renders_live_section():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.tabs) >= 6
