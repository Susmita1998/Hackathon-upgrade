"""PII redaction + prompt-injection detection (Gate 1 plugins, Gate 13/14)."""
from __future__ import annotations
import re, unicodedata
from dataclasses import dataclass, field

# Common Cyrillic/Greek lookalikes -> Latin (homoglyph smuggling defence)
_CONFUSABLES = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ѕ": "s", "ԁ": "d", "ɡ": "g",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "α": "a", "ο": "o", "ν": "v", "ρ": "p", "τ": "t", "ı": "i", "ⅼ": "l", "ǀ": "l",
}
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return "".join(_CONFUSABLES.get(c, c) for c in text)


_OB = r"[\[\(\{<]\s*"           # opening bracket
_CB = r"\s*[\]\)\}>]"           # closing bracket
_AT = rf"(?:@|{_OB}at{_CB}|\s+at\s+)"
_DOT = rf"(?:\.|{_OB}dot{_CB}|\s+dot\s+)"
_PII = [
    ("EMAIL", re.compile(rf"\b[\w.+-]+\s*{_AT}\s*[\w-]+(?:\s*{_DOT}\s*[A-Za-z0-9-]+)+\b", re.I)),
    ("SSN", re.compile(r"\b\d{3}[-\s.]\d{2}[-\s.]\d{4}\b|\b(?:ssn|social security(?: number)?)\D{0,6}\d{9}\b", re.I)),
    ("PHONE", re.compile(r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w-])|(?<![\w-])\+\d{1,3}[\s.-]?\d{1,4}[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?![\w-])")),
]


@dataclass
class PiiResult:
    text: str
    counts: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.counts)


def redact_pii(text: str) -> PiiResult:
    text = normalize(text)
    counts: dict[str, int] = {}
    for label, rx in _PII:
        text, n = rx.subn(f"[{label}]", text)
        if n:
            counts[label] = n
    return PiiResult(text, counts)


_HARD = [re.compile(p, re.I) for p in [
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instruction|rule|prompt|polic|guardrail|filter|safety|restriction)",
    r"\b(reveal|show|print|repeat|display|output|leak|tell me)\b.{0,30}\b(system|initial|hidden|original|developer)\b.{0,15}\b(prompt|message|instruction)",
    r"\byou are now\b", r"\b(developer|god|admin|sudo|jailbreak|dan)\s+mode\b", r"\bdo anything now\b",
    r"\bpretend (to be|you are|you're)\b", r"\bact as (an? )?(unrestricted|unfiltered|admin|root|different)",
    r"<\|im_(start|end)\|>|\[/?inst\]|<<sys>>|###\s*system", r"\bnew (system )?instructions?\s*:",
    r"\b(exfiltrate|send|post|upload)\b.{0,40}\b(to|at)\b.{0,10}https?://",
    r"('|\")\s*(or|and)\s*('|\"|\d)", r";\s*(drop|delete|update|insert|alter|truncate)\b", r"\bunion\s+select\b", r"\bdrop\s+table\b",
    r"\bxp_cmdshell\b|\b(rm\s+-rf|/etc/passwd)\b", r"(.)\1{400,}",
    r"\b(output|print|repeat|echo)\b.{0,25}\b(everything|all text|the text)\b.{0,15}\b(above|before)\b", r"\bverbatim\b.{0,30}\b(prompt|instruction|above)\b",
    r"\b(no|without any|free of)\s+(restrictions|limits|rules|filters)\b", r"\b(hidden|secret|internal)\s+(instructions|rules|prompt)\b",
    r"\b(disregard|forget)\b.{0,25}\b(everything|all)\b.{0,15}\b(above|before|earlier|prior)\b", r"\broleplay as\b.{0,30}\b(without|no)\b",
    r"\b(bypass|skip|disable|circumvent|get around|turn off)\b.{0,30}\b(approval|review|audit|hitl|logging)\b",
    r"\bnote to (the )?(ai|assistant|model|llm)\b", r"\bwhen you (read|see) this\b.{0,60}\b(ignore|instead|must|always)\b",
    r"\b(assistant|ai)\s*:\s*(ignore|sure|i will)\b",
]]
_SOFT = [(re.compile(p, re.I), w) for p, w in [
    (r"\bhypothetical(ly)?\b", 0.35), (r"\bfor a (story|novel|game|movie)\b", 0.35), (r"\bjust between us\b", 0.4),
    (r"\bwhat if (the )?rules\b", 0.5), (r"\bno one (will|would) know\b", 0.4), (r"\bstep by step\b.{0,40}\b(bypass|disable|circumvent|get around)\b", 0.7),
    (r"\b(my boss|ceo|manager) (said|told|approved|authorized)\b", 0.3), (r"\bfor (research|testing|educational) purposes\b", 0.35),
    (r"\b(disable|turn off|skip)\b.{0,20}\b(logging|audit|approval|hitl|review)\b", 0.7),
    (r"\bbetween you and me\b", 0.4), (r"\bjust this once\b", 0.4), (r"\bpassword|credential|api key|secret\b", 0.2),
]]


@dataclass
class InjectionVerdict:
    blocked: bool
    reason: str = ""
    score: float = 0.0


class InjectionShield:
    """Direct + indirect (chunk) injection detection with a session-level crescendo accumulator."""
    def __init__(self):
        self.sessions: dict[str, float] = {}

    def scan(self, text: str, session_key: str | None = None) -> InjectionVerdict:
        t = normalize(text)
        for rx in _HARD:
            if rx.search(t):
                return InjectionVerdict(True, f"hard-pattern:{rx.pattern[:40]}", 1.0)
        soft = sum(w for rx, w in _SOFT if rx.search(t))
        if session_key is not None:
            total = self.sessions.get(session_key, 0.0) * 0.9 + soft  # slow decay
            self.sessions[session_key] = total
            soft = total
        if soft >= 1.0:
            return InjectionVerdict(True, "crescendo", soft)
        return InjectionVerdict(False, "", soft)

    def scan_chunk(self, text: str) -> InjectionVerdict:
        return self.scan(text, session_key=None)
