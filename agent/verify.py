"""Claim-to-evidence verification (Verification subagent core; proxy for NLI + RAGAS faithfulness).

NOTE: this is a deterministic token-support check, not a trained NLI model or an LLM-judged RAGAS run.
"""
from __future__ import annotations
import re
from .rag import tokens


def claims(answer: str) -> list[str]:
    out = []
    for line in re.split(r"(?<=[.!?])\s+|\n+", answer):
        line = re.sub(r"\[[^\]]*\]", "", line).strip(" -•")
        if len(tokens(line)) >= 2:
            out.append(line)
    return out


def supported(claim: str, evidence: list[str], thresh: float = 0.8) -> bool:
    ct = set(tokens(claim))
    if not ct:
        return True
    for ev in evidence:
        et = set(tokens(ev))
        if len(ct & et) / len(ct) >= thresh:
            return True
    return False


def faithfulness(answer: str, evidence: list[str]) -> float:
    cl = claims(answer)
    if not cl:
        return 1.0
    return sum(supported(c, evidence) for c in cl) / len(cl)
