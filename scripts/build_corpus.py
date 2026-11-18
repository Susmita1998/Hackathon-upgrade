"""Build the synthetic enterprise corpus: Obsidian vault, graph.json, golden set, triage + red-team data.

Everything is generated deterministically (seeded) so results are reproducible.
Usage: python3 -I scripts/build_corpus.py
"""
import hashlib, json, random, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "knowledge_vault"
rnd = random.Random(7)

TOPICS = [
    ("Cold Chain Storage", "logistics", 2), ("Batch Recall", "quality", 3), ("Lot Quarantine", "quality", 3),
    ("Supplier Onboarding", "procurement", 1), ("Purchase Approval", "finance", 3), ("Invoice Dispute", "finance", 2),
    ("Data Retention", "compliance", 2), ("Access Review", "security", 2), ("Incident Escalation", "operations", 2),
    ("Safety Inspection", "safety", 3), ("Hazmat Handling", "safety", 3), ("Warehouse Receiving", "logistics", 1),
    ("Returns Processing", "logistics", 1), ("Customer Refund", "finance", 3), ("Vendor Audit", "compliance", 2),
    ("Change Management", "operations", 2), ("Backup Recovery", "security", 2), ("Password Rotation", "security", 1),
    ("Travel Expense", "finance", 1), ("Equipment Calibration", "quality", 2), ("Label Verification", "quality", 1),
    ("Shipment Hold", "logistics", 2), ("Training Certification", "compliance", 1), ("Contract Renewal", "procurement", 2),
    ("Fire Drill", "safety", 1), ("Software Release", "operations", 2), ("Privacy Request", "compliance", 3),
    ("Stock Adjustment", "logistics", 3), ("Credit Limit", "finance", 3), ("Visitor Access", "security", 1),
]
ATTRS = [
    ("maximum processing time", "{n} hours"), ("required approvals", "{n} approvers"),
    ("record retention period", "{n} days"), ("escalation deadline", "{n} minutes"),
    ("review frequency", "every {n} weeks"), ("sample size", "{n} units"),
    ("notification window", "{n} hours"), ("audit lookback period", "{n} months"),
    ("maximum batch size", "{n} items"), ("cooling-off period", "{n} days"),
]
OWNERS = ["Quality Lead", "Operations Manager", "Compliance Officer", "Finance Controller", "Security Officer",
          "Logistics Supervisor", "Procurement Director", "Safety Marshal"]

def slug(t): return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")

notes = []
for i, (title, domain, tier) in enumerate(TOPICS):
    attrs = rnd.sample(ATTRS, 5)
    facts = []
    for a, tpl in attrs:
        val = tpl.format(n=rnd.randint(3, 97))
        facts.append({"attr": a, "value": val, "sentence": f"For {title}, the {a} is {val}."})
    owner = rnd.choice(OWNERS)
    facts.append({"attr": "responsible owner", "value": owner, "sentence": f"For {title}, the responsible owner is the {owner}."})
    notes.append({"id": slug(title), "title": title, "domain": domain, "risk_tier": tier, "facts": facts, "links": []})

by_id = {n["id"]: n for n in notes}
for n in notes:  # 2 links each; first link drives multi-hop questions
    others = [m for m in notes if m["id"] != n["id"]]
    for tgt in rnd.sample(others, 2):
        n["links"].append(tgt["id"])
    t = by_id[n["links"][0]]
    n["facts"].append({"attr": "escalation procedure", "value": t["title"], "link": t["id"],
                       "sentence": f"Escalations for {n['title']} follow the [[{t['id']}]] procedure ({t['title']})."})

def render(n):
    fm = f"---\nid: {n['id']}\ndomain: {n['domain']}\nrisk_tier: {n['risk_tier']}\nversion: 1\n---\n"
    body = f"# {n['title']}\n\n" + "\n".join(f"- {f['sentence']}" for f in n["facts"])
    body += "\n\nRelated: " + ", ".join(f"[[{l}]]" for l in n["links"]) + "\n"
    return fm + body

for f in VAULT.glob("*.md"): f.unlink()
for n in notes:
    (VAULT / f"{n['id']}.md").write_text(render(n))
# demo note used by the load/demo query "Verify batch LOT-88392 safety protocol"
demo = {"id": "lot-88392-safety-protocol", "title": "Lot 88392 Safety Protocol", "domain": "safety", "risk_tier": 3,
        "links": ["batch-recall", "lot-quarantine"], "facts": [
    {"attr": "safety protocol", "value": "quarantine pending inspection", "sentence": "For LOT-88392, the safety protocol is quarantine pending inspection."},
    {"attr": "inspection window", "value": "48 hours", "sentence": "For LOT-88392, the inspection window is 48 hours."},
    {"attr": "triage status", "value": "amber", "sentence": "For LOT-88392, the triage status is amber until inspection completes."}]}
notes.append(demo); by_id[demo["id"]] = demo
(VAULT / f"{demo['id']}.md").write_text(render(demo))

# ---------------- graph.json (Graphify) -----------------
nodes = [{"id": n["id"], "type": "policy", "title": n["title"], "domain": n["domain"], "risk_tier": n["risk_tier"]} for n in notes]
edges = [{"src": n["id"], "dst": l, "type": "wikilink"} for n in notes for l in n["links"]]
for m in sorted((ROOT / "agent").glob("*.py")):  # code nodes so the graph spans modules too
    nodes.append({"id": f"code:{m.stem}", "type": "module", "title": m.name})
(ROOT / "graph.json").write_text(json.dumps({"nodes": nodes, "edges": edges}, indent=1))

# ---------------- golden dataset (250) -----------------
def fact_q(n, f): return f"What is the {f['attr']} for {n['title']}?"
gold = []
def add(kind, q, ans, ids, answerable=True):
    gold.append({"id": f"Q{len(gold)+1:03d}", "type": kind, "question": q, "ground_truth": ans,
                 "relevant_ids": ids, "answerable": answerable})

plain = [(n, f) for n in notes[:-1] for f in n["facts"] if "link" not in f]
rnd.shuffle(plain)
for n, f in plain[:63]:
    add("factoid", fact_q(n, f), f["value"], [n["id"]])
for n, f in plain[63:125]:
    cond = rnd.choice(["Before scheduling work,", "For compliance reporting,", "When preparing an audit,"])
    add("complex_reasoning", f"{cond} what {f['attr']} applies to {n['title']}, and who is the responsible owner?",
        f"{f['value']}; {[x for x in n['facts'] if x['attr']=='responsible owner'][0]['value']}", [n["id"]])
hop = [n for n in notes[:-1]]
for n in (hop * 2)[:50]:
    tgt = by_id[n["links"][0]]
    f = rnd.choice([x for x in tgt["facts"] if "link" not in x and x["attr"] != "responsible owner"])
    add("multi_hop", f"What is the {f['attr']} of the procedure that escalations for {n['title']} follow?",
        f["value"], [n["id"], tgt["id"]])
for n in (notes[:-1] * 2)[:38]:
    add("summary", f"Summarize the {n['title']} policy.", " ".join(f["sentence"] for f in n["facts"]), [n["id"]])
NEG = ["What is the cafeteria menu for Friday?", "Who won the 2022 football World Cup?", "What is the CEO's home address?",
       "How do I bake sourdough bread?", "What will the weather be in Paris tomorrow?", "Recommend a good sci-fi movie.",
       "What is the stock price of Acme Corp today?", "Translate hello into Klingon.", "Who is the best tennis player ever?",
       "What is the capital of Australia?", "How tall is Mount Everest?", "Write a poem about the sea.",
       "What is my colleague's salary?", "What is the airspeed velocity of an unladen swallow?",
       "Which restaurant near the office has the best pizza?", "What are next year's lottery numbers?",
       "How many moons does Jupiter have?", "Who painted the Mona Lisa?", "What is the WiFi password at the airport?",
       "Explain quantum entanglement to a child.", "What time does the sun set in Tokyo?", "Suggest a name for my dog.",
       "What is the plot of Hamlet?", "How do I fix my bicycle chain?", "Who is the president of Brazil?",
       "What is the boiling point of mercury?", "Give me a joke about accountants.", "What is the best programming language?",
       "How many calories are in an apple?", "When is the next solar eclipse?", "What is the population of Canada?",
       "Who invented the telephone?", "What year did the Berlin Wall fall?", "Which vitamins help with sleep?",
       "What is the speed of light?", "How do I learn to play guitar?", "What is the tallest building in Dubai?"]
for q in NEG[:37]:
    add("negative", q, "", [], answerable=False)
assert len(gold) == 250, len(gold)
(ROOT / "data").mkdir(exist_ok=True)
(ROOT / "data" / "golden_dataset.json").write_text(json.dumps(gold, indent=1))

# ---------------- triage labelled set -----------------
def tri(q, route, domain="general", risk=1, cx="low"): return {"query": q, "route": route, "domain": domain, "risk_tier": risk, "complexity": cx}
labelled = []
verbs = ["submit", "approve", "cancel", "transition", "update", "create", "close", "release"]
for n in notes[:-1]:
    t = n["title"]
    labelled += [
        tri(f"{rnd.choice(['Before scheduling work', 'For reporting', 'When preparing a review', 'During onboarding'])}, what {rnd.choice(ATTRS)[0]} applies to {t}, and who is the owner?", "HYBRID_RAG", n["domain"], n["risk_tier"], "high"),
        tri(f"What is the {rnd.choice(ATTRS)[0]} of the procedure that escalations for {t} follow?", "HYBRID_RAG", n["domain"], n["risk_tier"], "high"),
        tri(f"What is the {rnd.choice(ATTRS)[0]} for {t}?", "HYBRID_RAG", n["domain"], n["risk_tier"]),
        tri(f"Explain the {t} policy", "HYBRID_RAG", n["domain"], n["risk_tier"], "medium"),
        tri(f"Summarize our {t} rules", "HYBRID_RAG", n["domain"], n["risk_tier"], "medium"),
        tri(f"{rnd.choice(verbs).capitalize()} the {t} form for SKU-{rnd.randint(1,120):03d}", "TRANSACTION_EXEC", n["domain"], 3),
        tri(f"Please {rnd.choice(verbs)} workflow {t} for LOT-{rnd.randint(1,99999):05d}", "TRANSACTION_EXEC", n["domain"], 3),
    ]
for i in range(40):
    kind = rnd.choice(["Verify batch LOT-{l:05d} safety protocol and return triage status", "Check the status of LOT-{l:05d}",
                       "How many units of SKU-{k:03d} are in stock?", "Show details for SKU{k} and its lot LOT-{l:05d}",
                       "What is the safety protocol for lot {l}?"])
    labelled.append(tri(kind.format(l=rnd.randint(1, 99999), k=rnd.randint(1, 120)), "HYBRID_RAG", "logistics", 1))
for q in ["hi", "hello there", "thanks", "thank you", "good morning", "who are you", "what can you do", "help", "hey assistant", "bye"]:
    labelled.append(tri(q, "DIRECT_ANSWER"))
for r in NEG:
    labelled.append(tri(r, "REJECT_OOS"))
rnd.shuffle(labelled)
(ROOT / "data" / "triage_labelled.json").write_text(json.dumps(labelled, indent=1))
print(f"notes={len(notes)} edges={len(edges)} gold={len(gold)} triage={len(labelled)}")
