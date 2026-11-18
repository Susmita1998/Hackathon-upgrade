"""Real RAGAS scoring (the `ragas` library) of live pipeline answers.

Pipeline: real OpenRouter calls at every step -> answer + the exact evidence the LLM saw -> RAGAS metrics scored by an LLM judge
(anthropic/claude-haiku-4.5 via OpenRouter) with local BAAI/bge-small-en-v1.5 embeddings for answer relevancy
(OpenRouter embedding models are blocked for this workspace). Writes reports/ragas_live.json.

Usage: python3 -I eval/run_ragas.py [--per-type 12] [--judge anthropic/claude-haiku-4.5]
"""
import argparse, asyncio, json, math, os, random, re, statistics, sys, time, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LLM_MODE", "openrouter")
os.environ["RETURN_EVIDENCE"] = "1"
import httpx
import importlib.metadata as md
from openai import AsyncOpenAI
from ragas.embeddings.base import BaseRagasEmbedding
from ragas.llms import llm_factory
from ragas.metrics.collections import AnswerRelevancy, ContextEntityRecall, ContextPrecisionWithReference, ContextRecall, Faithfulness
from sentence_transformers import SentenceTransformer
from agent import config as C
from agent.models import QueryRequest
from agent.openrouter import OpenRouterClient
from agent.rag import load_vault
from agent.reporting import RAGAS_GATES
from agent.service import AgentService

ap = argparse.ArgumentParser(); ap.add_argument("--per-type", type=int, default=12); ap.add_argument("--judge", default="anthropic/claude-haiku-4.5")
ap.add_argument("--budget", type=float, default=2.0); a = ap.parse_args()
KEY = os.environ["OPENROUTER_API_KEY"]
gold = json.loads((ROOT / "data/golden_dataset.json").read_text())
notes = {n["id"]: n for n in load_vault(C.VAULT_DIR)}


def key_usage() -> float:
    r = httpx.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {KEY}"}, timeout=20)
    return float(r.json()["data"]["usage"])


def reference_for(g) -> str:
    """A natural-language reference answer built from the vault sentence(s) that contain the ground-truth value(s)."""
    if g["type"] == "summary":
        return g["ground_truth"]
    vals = [v.strip() for v in g["ground_truth"].split(";")] if g["type"] == "complex_reasoning" else [g["ground_truth"]]
    note = notes[g["relevant_ids"][-1] if g["type"] == "multi_hop" else g["relevant_ids"][0]]
    lines = [x[2:] for x in note["body"].splitlines() if x.startswith("- ")]
    picked = []
    for v in dict.fromkeys(vals):
        for line in lines:
            if v.lower() in line.lower() and line not in picked:
                picked.append(line); break
    return " ".join(picked) or g["ground_truth"]


class Bge(BaseRagasEmbedding):
    def __init__(self):
        self.m = SentenceTransformer("BAAI/bge-small-en-v1.5", device="cpu")
    def embed_text(self, text, **kw): return self.m.encode([text], normalize_embeddings=True)[0].tolist()
    async def aembed_text(self, text, **kw): return await asyncio.to_thread(self.embed_text, text)
    def embed_texts(self, texts, **kw): return self.m.encode(list(texts), normalize_embeddings=True).tolist()
    async def aembed_texts(self, texts, **kw): return await asyncio.to_thread(self.embed_texts, texts)


async def main():
    t0, usage0 = time.time(), key_usage()
    rnd = random.Random(21)
    sample = []
    for typ in ("factoid", "complex_reasoning", "multi_hop", "summary"):
        pool = [g for g in gold if g["type"] == typ]; rnd.shuffle(pool); sample += pool[: a.per_type]
    svc = AgentService(llm="openrouter", client=OpenRouterClient(budget_usd=a.budget))
    assert svc.live, "OPENROUTER_API_KEY not set"
    sem = asyncio.Semaphore(3)

    async def run_one(i, g):
        async with sem:
            return await svc.handle(QueryRequest(tenant_id="tenant_1", user_id=f"ragas{i}", query=g["question"]))
    resps = await asyncio.gather(*[run_one(i, g) for i, g in enumerate(sample)])
    pipeline_spend = svc.client.spent

    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=KEY, default_headers={"X-Title": "Unreliable Assistant RAGAS"})
    llm = llm_factory(a.judge, provider="openai", client=client, temperature=0, max_tokens=1500)
    emb = Bge()
    m_faith, m_rel, m_prec, m_rec, m_ent = Faithfulness(llm=llm), AnswerRelevancy(llm=llm, embeddings=emb), ContextPrecisionWithReference(llm=llm), ContextRecall(llm=llm), ContextEntityRecall(llm=llm)
    sem2 = asyncio.Semaphore(4); errors = {}

    async def safe(name, coro):
        async with sem2:
            try:
                v = (await coro).value
                return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
            except Exception as e:
                errors[name] = errors.get(name, 0) + 1
                errors[f"{name}_last"] = f"{type(e).__name__}: {str(e)[:120]}"
                return None

    async def score(g, r):
        ctxs = [e["text"] for e in r.evidence] or ["(no context)"]
        ref = reference_for(g)
        # citation markers like [fire-drill#0] are metadata, not claims: RAGAS otherwise counts them as an unsupported statement
        # (measured: the same answer scores faithfulness 0.5 with the marker and 1.0 without it)
        resp_text = re.sub(r"\s*\[[^\[\]]*#\d+\]", "", r.answer).strip()
        f, rel, prec, rec, ent = await asyncio.gather(
            safe("faithfulness", m_faith.ascore(user_input=g["question"], response=resp_text, retrieved_contexts=ctxs)),
            safe("answer_relevancy", m_rel.ascore(user_input=g["question"], response=resp_text)),
            safe("context_precision", m_prec.ascore(user_input=g["question"], reference=ref, retrieved_contexts=ctxs)),
            safe("context_recall", m_rec.ascore(user_input=g["question"], retrieved_contexts=ctxs, reference=ref)),
            safe("context_entity_recall", m_ent.ascore(reference=ref, retrieved_contexts=ctxs)))
        return {"id": g["id"], "type": g["type"], "status": r.status, "question": g["question"], "answer": r.answer, "scored_response": resp_text, "reference": ref, "contexts": ctxs, "n_contexts": len(r.evidence),
                "faithfulness": f, "answer_relevancy": rel, "context_precision": prec, "context_recall": rec, "context_entity_recall": ent}

    answered = [(g, r) for g, r in zip(sample, resps) if r.status == "OK" and r.evidence]
    rows = await asyncio.gather(*[score(g, r) for g, r in answered])
    metrics = list(RAGAS_GATES)
    scores = {}
    for m in metrics:
        vals = [x[m] for x in rows if x[m] is not None]
        scores[m] = {"mean": statistics.mean(vals) if vals else None, "n": len(vals), "gate": RAGAS_GATES[m], "pass": bool(vals) and statistics.mean(vals) >= RAGAS_GATES[m],
                     "min": min(vals) if vals else None, "below_gate": sum(v < RAGAS_GATES[m] for v in vals)}
    by_type = {t: {m: (statistics.mean([x[m] for x in rows if x["type"] == t and x[m] is not None]) if any(x["type"] == t and x[m] is not None for x in rows) else None) for m in metrics}
               for t in ("factoid", "complex_reasoning", "multi_hop", "summary")}
    usage1 = key_usage()
    out = {"run": {"ragas_version": md.version("ragas"), "judge_model": a.judge, "answer_relevancy_embeddings": "BAAI/bge-small-en-v1.5 (local, sentence-transformers)",
                   "sampled": len(sample), "answered_and_scored": len(rows), "not_answered": len(sample) - len(answered), "per_type": a.per_type,
                   "pipeline_spend_usd": round(pipeline_spend, 4), "total_spend_usd": round(usage1 - usage0, 4), "seconds": round(time.time() - t0, 1), "metric_errors": errors},
           "scores": scores, "by_type": by_type, "samples": rows,
           "notes": ["Scored with the real ragas library (collections API). Reference answers are built from the vault sentence(s) containing the ground-truth value.",
                     "Judge = claude-haiku-4.5, which is also the pipeline's independent verifier model, so faithfulness is partly self-graded; use a different judge family for an unbiased score.",
                     "Answers the pipeline declined (abstained/blocked) are not scored by RAGAS and are counted in not_answered.",
                     "Citation markers such as [fire-drill#0] are removed from the response before scoring: RAGAS counted the marker as an extra unsupported statement (same answer: faithfulness 0.5 with the marker, 1.0 without). The earlier run with markers left in is kept in reports/ragas_live_citations_in_response.json.",
                     "Context precision (ContextPrecisionWithReference) judges each context in isolation. For multi-hop questions it returned 0.0 even when the only context supplied contained the exact reference sentence, so that metric cannot credit evidence that is part of a hop chain; read the per-type table, not only the mean.",
                     "Samples store the full answers and exact contexts so scores can be reproduced or re-judged with a different model."]}
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/ragas_live.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({"run": out["run"], "scores": {k: (round(v["mean"], 3) if v["mean"] is not None else None, "PASS" if v["pass"] else "FAIL", f"n={v['n']}") for k, v in scores.items()},
                      "by_type": {t: {m: (round(x, 2) if x is not None else None) for m, x in d.items()} for t, d in by_type.items()}}, indent=1, default=str))

asyncio.run(main())
