"""Triage agent (Gate 6, ADR-05 no-GPU fallback): TF-IDF + logistic regression behind the same interface a fine-tuned SLM would use.

Outputs (intent, domain, risk_tier, complexity) and a route: DIRECT_ANSWER | HYBRID_RAG | TRANSACTION_EXEC | REJECT_OOS.
Deterministic (no sampling): equivalent of temperature 0.0.
"""
from __future__ import annotations
import json, re, time
from dataclasses import dataclass
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from . import config as C

ENTITY = re.compile(r"\b(?:SKU[\s_-]*\d{1,6}|LOT[\s_-]*\d{1,5})\b", re.I)
MUTATE = re.compile(r"\b(submit|approve|cancel|transition|update|create|close|release|recall|quarantine|delete|modify)\b", re.I)
GREETING = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|evening|afternoon)|bye|help|who are you|what can you do)\b[\s\w,!.?]{0,20}$", re.I)


@dataclass
class TriageResult:
    route: str; intent: str; domain: str; risk_tier: int; complexity: str; ms: float


def _clean(q: str) -> str:
    return ENTITY.sub(" ENTITY ", q.lower())


class TriageAgent:
    def __init__(self, labelled: list[dict] | None = None, domain_lex: dict | None = None, vocab: set | None = None):
        data = labelled if labelled is not None else json.loads((C.DATA_DIR / "triage_labelled.json").read_text())
        self.model = make_pipeline(TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, preprocessor=_clean),
                                   LogisticRegression(max_iter=1000, C=8.0))
        self.model.fit([d["query"] for d in data], [d["route"] for d in data])
        self.domain_lex = domain_lex or {}
        self.vocab = vocab                              # corpus vocabulary: domain gate for out-of-scope queries

    def classify(self, query: str) -> TriageResult:
        t0 = time.perf_counter()
        route = self.model.predict([query])[0]
        has_entity = bool(ENTITY.search(query))
        if GREETING.match(query):
            route = "DIRECT_ANSWER"
        elif route == "TRANSACTION_EXEC" and not (MUTATE.search(query) and has_entity):
            route = "HYBRID_RAG"                         # mutation needs both a verb and a target entity
        elif has_entity and MUTATE.search(query) and route != "REJECT_OOS":
            route = "TRANSACTION_EXEC"
        if self.vocab is not None and not has_entity and route != "DIRECT_ANSWER":
            from .rag import tokens
            toks = tokens(query)
            if toks and sum(t in self.vocab for t in toks) / len(toks) < 0.4:
                route = "REJECT_OOS"
        words = set(re.findall(r"[a-z]+", query.lower()))
        domain = next((d for d, kws in self.domain_lex.items() if words & kws), "general")
        risk = 3 if route == "TRANSACTION_EXEC" else (2 if has_entity else 1)
        complexity = "high" if len(query.split()) > 25 else "medium" if len(query.split()) > 12 or " and " in query.lower() else "low"
        return TriageResult(route, {"HYBRID_RAG": "question", "TRANSACTION_EXEC": "action", "DIRECT_ANSWER": "chitchat", "REJECT_OOS": "off_topic"}[route],
                            domain, risk, complexity, (time.perf_counter() - t0) * 1e3)
