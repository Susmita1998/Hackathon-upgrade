"""Signed manifests, blueprint span names, cross-request hot memory, real MCP stdio transport."""
import base64, json, shutil
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from conftest import ask, run
from test_llm import FakeClient, happy, plan_script, RETRIEVE, svc_with, triage
from agent import manifest as M
from agent.cache import HotMemory
from agent.mcp_real import RealMcpClients
from agent.models import QueryRequest
from agent.service import AgentService


# ---------------- signed manifests ----------------
def test_shipped_manifests_verify(svc):
    assert all(st["verified"] for st in svc.gw.manifest_status.values()), svc.gw.manifest_status


def test_tampered_signature_rejected(svc, tmp_path):
    doc = json.loads(M.MANIFESTS.read_text()); doc["servers"]["hybrid-rag"]["manifest"]["tools"].append("evil_tool")
    p = tmp_path / "m.json"; p.write_text(json.dumps(doc))
    ok, reason = M.verify_server(svc.rag_srv, p)
    assert not ok and "signature invalid" in reason


def test_changed_source_detected(svc, tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    pub = tmp_path / "pub.pem"; pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    m = M.build_manifest(svc.rag_srv); m["sources"]["agent/rag.py"] = "0" * 64           # pretend the file was different when signed
    doc = {"servers": {"hybrid-rag": {"manifest": m, "signature": base64.b64encode(key.sign(M.canonical(m))).decode()}}}
    p = tmp_path / "m.json"; p.write_text(json.dumps(doc))
    ok, reason = M.verify_server(svc.rag_srv, p, pub)
    assert not ok and "source changed" in reason and "agent/rag.py" in reason


def test_unsigned_tool_makes_server_fail_closed():
    s = AgentService()
    s.rag_srv.tools["exfiltrate"] = lambda t, a: {}                                      # a tool the manifest never signed
    s.gw.verify_manifests()
    assert not s.gw.manifest_status["hybrid-rag"]["verified"] and "differs" in s.gw.manifest_status["hybrid-rag"]["reason"]
    r = ask(s, "What is the sample size for Fire Drill?")
    assert r.status == "ABSTAINED"                                                       # never called the untrusted server
    del s.rag_srv.tools["exfiltrate"]; s.gw.verify_manifests()
    assert s.gw.manifest_status["hybrid-rag"]["verified"]


# ---------------- trace spans ----------------
def test_blueprint_span_names_emitted(svc):
    r = ask(svc, "What is the sample size for Fire Drill?", user="spans")
    names = {s["name"] for s in r.spans}
    assert {"hook.pre", "cache.lookup", "triage.classify", "mcp.rag.bm25", "mcp.rag.biencoder", "mcp.rag.rrf", "mcp.rag.rerank",
            "agent.react.step", "llm.generate", "hook.post.ragas"} <= names, names


# ---------------- cross-request hot memory ----------------
def test_hot_memory_keeps_last_three_turns_per_session(fresh):
    for i in range(5):
        fresh.memory.push("tenant_1", "s1", {"q": f"q{i}", "a": f"a{i}"})
    assert [t["q"] for t in fresh.memory.load("tenant_1", "s1")] == ["q2", "q3", "q4"]                # oldest first, max 3
    assert fresh.memory.load("tenant_1", "s2") == [] and fresh.memory.load("tenant_2", "s1") == []     # session + tenant isolated
    assert fresh.memory.key("tenant_1", "s1").startswith("tenant_1:")


def test_previous_turns_reach_the_planner_and_generator():
    f = happy(plan=plan_script(RETRIEVE, {"objective": "o", "action": "final"}, RETRIEVE, {"objective": "o", "action": "final"})); s = svc_with(f)
    ask(s, "What is the sample size for Fire Drill?", session="conv1")
    ask(s, "And what about its approvals?", session="conv1", user="u1")
    plan_msgs = [c[2][1]["content"] for c in f.calls if c[0] == "plan"]
    gen_msgs = [c[2][1]["content"] for c in f.calls if c[0] == "generate"]
    tri_msgs = [c[2][1]["content"] for c in f.calls if c[0] == "triage"]
    assert "<recent_turns>" in tri_msgs[-1] and "<recent_turns>" not in tri_msgs[0]          # follow-ups are triaged with context
    assert "[RECENT_TURNS]" in plan_msgs[-1] and "sample size" in plan_msgs[-1].lower()
    assert "<conversation_history>" in gen_msgs[-1] and "<conversation_history>" not in gen_msgs[0]


def test_memory_stores_only_redacted_text_and_survives_redis_outage(fresh):
    ask(fresh, "Mail jo[at]acme[dot]com: what is the sample size for Fire Drill?", session="p1")
    assert "jo[at]acme" not in json.dumps(fresh.memory.load("tenant_1", "p1"))
    fresh.memory.up = False
    assert ask(fresh, "What is the sample size for Fire Drill?", user="x", session="p2").status == "OK"


# ---------------- real MCP over stdio ----------------
def test_api_calls_real_mcp_servers_over_stdio(tmp_path):
    db = str(tmp_path / "shared.db")
    s = AgentService(state_path=db)

    async def go():
        async with RealMcpClients(db) as real:
            s.gw.real = real
            a = await s.handle(QueryRequest(tenant_id="tenant_1", user_id="u", query="What is the sample size for Fire Drill?"))
            b = await s.handle(QueryRequest(tenant_id="tenant_1", user_id="u2", query="Check the status of LOT-88392"))
            c = await s.handle(QueryRequest(tenant_id="tenant_1", user_id="u3", query="Quarantine lot LOT-00002"))
            d = await s.handle(QueryRequest(tenant_id="tenant_1", user_id="u4", query="Check the status of LOT-99999"))
            s.gw.real = None
            return a, b, c, d
    a, b, c, d = run(go())
    assert a.status == "OK" and "63" in a.answer and any(sp["name"].startswith("mcp.hybrid-rag") for sp in a.spans)
    assert b.status == "OK" and "quarantined" in b.answer.lower()                         # state-store subprocess read the shared DB
    assert c.status == "PENDING_APPROVAL" and len(s.state.list_approvals("tenant_1", "PENDING_APPROVAL")) == 1   # approval row written by the subprocess
    assert d.status in ("ABSTAINED", "OK") and "not found" in json.dumps(d.model_dump()).lower() or d.status == "ABSTAINED"
    assert s.state.verify_chain("tenant_1")                                               # audit chain intact with two writer processes


def test_follow_up_is_not_treated_as_greeting_when_memory_exists():
    f = happy(triage=triage("DIRECT_ANSWER")); s = svc_with(f)
    assert ask(s, "and which approvals does it need?", session="fu").route == "DIRECT_ANSWER"      # no memory: the model's choice stands
    s.memory.push("tenant_1", "fu2", {"q": "What is the sample size for Fire Drill?", "a": "63 units [fire-drill#0]"})
    assert ask(s, "and which approvals does it need?", session="fu2", user="u2").route == "HYBRID_RAG"   # with memory: it is a follow-up
    assert ask(s, "hello", session="fu2", user="u3").route == "DIRECT_ANSWER"                          # real greetings stay direct


def test_context_dependent_answers_are_never_cached_across_sessions(fresh):
    q = "What is the sample size for Fire Drill?"
    fresh.memory.push("tenant_1", "A", {"q": "earlier", "a": "earlier answer"})
    a = ask(fresh, q, user="same", session="A")
    assert a.cache == "miss"                                                             # computed with memory, so it was not stored
    b = ask(fresh, q, user="same", session="B")                                          # fresh session, same user, same text
    assert b.cache == "miss"                                                             # not served from A's context-dependent answer
    assert ask(fresh, q, user="same", session="C").cache == "hit"                        # a normal stateless answer is cached as before
