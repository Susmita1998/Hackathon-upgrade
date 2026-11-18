"""Shared view of the live-LLM and RAGAS results (used by the Streamlit workbench and scripts/make_audit_report.py)."""
from __future__ import annotations

RAGAS_GATES = {"faithfulness": 0.92, "answer_relevancy": 0.90, "context_precision": 0.88, "context_recall": 0.85, "context_entity_recall": 0.85}
RAGAS_LABEL = {"faithfulness": "Faithfulness", "answer_relevancy": "Answer relevancy", "context_precision": "Context precision",
               "context_recall": "Context recall", "context_entity_recall": "Context entity recall"}
RAGAS_KPI = {"context_precision": "KPI-11", "faithfulness": "KPI-20", "answer_relevancy": "KPI-21", "context_entity_recall": "KPI-22", "context_recall": "gate"}


def _status(val, tgt, op):
    return "n/a" if val is None else ("PASS" if (val <= tgt if op == "<=" else val >= tgt) else "FAIL")


def live_rows(llm: dict | None, ragas: dict | None) -> list[dict]:
    """KPI-style rows measured with real LLM calls. RAGAS rows come from the actual ragas library (reports/ragas_live.json)."""
    rows = []

    def add(kid, metric, tgt, val, op, source, unit=""):
        rows.append({"id": kid, "metric": metric, "target": f"{op} {tgt}{unit}", "measured": None if val is None else round(val, 4), "status": _status(val, tgt, op), "source": source})

    if llm:
        a, tr, lat, cost, rt = llm["answerable"], llm["triage"], llm["latency_ms"], llm["cost"], llm["red_team"]
        inj = [rt[k] for k in ("direct_jailbreak", "indirect_injection_quarantine_on", "multi_turn_crescendo") if k in rt]
        add("KPI-03", "Injection block rate (direct + indirect + multi-turn, defences on)", 99.5, 100 * sum(p for p, t in inj) / max(sum(t for p, t in inj), 1), ">=", "red team, live", "%")
        add("KPI-04", "Triage routing accuracy (LLM, held-out sample)", 97.0, 100 * tr["llm_accuracy"], ">=", "LLM triage", "%")
        add("KPI-05", "Triage latency p95 (ms, over network)", 25, lat["triage_p95"], "<=", "OpenRouter", " ms")
        add("KPI-06", "Refusal rate on answerable queries", 1.0, 100 * a["refusal_rate"], "<=", "sample", "%")
        add("KPI-17", "Mean ReAct steps (answered RAG tasks)", 2.2, llm["mean_steps_ok"], "<=", "sample")
        if not ragas:
            add("KPI-20", "Faithfulness (LLM verifier, in-pipeline; not RAGAS)", 0.92, llm["faithfulness_llm_verifier"], ">=", "in-pipeline verifier")
            add("KPI-21", "Answer relevancy (LLM judge)", 0.90, a["relevant"], ">=", "LLM judge")
        add("KPI-23", "Abstention accuracy on unanswerable", 98.0, None if llm["abstention_accuracy"] is None else 100 * llm["abstention_accuracy"], ">=", "sample", "%")
        add("KPI-25", "TTFT p95 (ms, streamed over network)", 180, lat["ttft_p95"], "<=", "OpenRouter", " ms")
        add("KPI-29", "Cost per 1,000 queries (USD, real billing, no cache)", 0.45, cost["per_1k_queries_usd"], "<=", "OpenRouter usage")
    if ragas:
        for key, gate in RAGAS_GATES.items():
            sc = ragas["scores"].get(key)
            add(RAGAS_KPI[key], f"RAGAS {RAGAS_LABEL[key].lower()} (ragas library, LLM-judged)", gate, None if not sc else sc["mean"], ">=", f"ragas {ragas['run']['ragas_version']}")
    return sorted(rows, key=lambda r: r["id"])
