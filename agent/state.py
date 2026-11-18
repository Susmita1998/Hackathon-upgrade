"""Tenant-scoped state store (SQLite stand-in for PostgreSQL+pgvector) with hash-chained audit log.

Every query carries tenant_id. No raw PII is ever written (callers pass redacted text).
"""
from __future__ import annotations
import hashlib, json, sqlite3, threading, time, uuid
from .models import ErrorCode, ToolError

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_facts(tenant_id TEXT, session_id TEXT, k TEXT, v TEXT, ts REAL, PRIMARY KEY(tenant_id,session_id,k));
CREATE TABLE IF NOT EXISTS inventory_sku(tenant_id TEXT, sku TEXT, name TEXT, qty INTEGER, PRIMARY KEY(tenant_id,sku));
CREATE TABLE IF NOT EXISTS inventory_lot(tenant_id TEXT, lot_id TEXT, sku TEXT, status TEXT, PRIMARY KEY(tenant_id,lot_id));
CREATE TABLE IF NOT EXISTS pending_approvals(id TEXT PRIMARY KEY, tenant_id TEXT, tool TEXT, args TEXT, requested_by TEXT, status TEXT, created REAL, decided_by TEXT, decided REAL);
CREATE TABLE IF NOT EXISTS idempotency(tenant_id TEXT, key TEXT, result TEXT, PRIMARY KEY(tenant_id,key));
CREATE TABLE IF NOT EXISTS audit_log(seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, ts REAL, event TEXT, payload TEXT, prev_hash TEXT, hash TEXT);
"""
GENESIS = "0" * 64


class StateStore:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")             # several processes (API + stdio MCP state server) share one file
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # ---- seed ----
    def seed_tenant(self, tenant_id: str):
        with self.lock:
            if self.db.execute("SELECT 1 FROM inventory_sku WHERE tenant_id=? LIMIT 1", (tenant_id,)).fetchone():
                return
            skus = [(tenant_id, f"SKU-{i:03d}", f"Item {i}", 100 + i) for i in range(1, 121)]
            self.db.executemany("INSERT OR IGNORE INTO inventory_sku VALUES (?,?,?,?)", skus)
            lots = [(tenant_id, f"LOT-{n:05d}", f"SKU-{(n % 120) + 1:03d}", "released") for n in (44, 12345, 1, 2, 3)]
            lots.append((tenant_id, "LOT-88392", "SKU-099", "quarantined"))
            self.db.executemany("INSERT OR IGNORE INTO inventory_lot VALUES (?,?,?,?)", lots)
            self.db.commit()

    # ---- reads ----
    def lookup_sku(self, tenant_id, sku):
        r = self.db.execute("SELECT sku,name,qty FROM inventory_sku WHERE tenant_id=? AND sku=?", (tenant_id, sku)).fetchone()
        if not r:
            raise ToolError(ErrorCode.NOT_FOUND, f"{sku} not found")
        return {"sku": r[0], "name": r[1], "qty": r[2]}

    def lookup_lot(self, tenant_id, lot_id):
        r = self.db.execute("SELECT lot_id,sku,status FROM inventory_lot WHERE tenant_id=? AND lot_id=?", (tenant_id, lot_id)).fetchone()
        if not r:
            raise ToolError(ErrorCode.NOT_FOUND, f"{lot_id} not found")
        return {"lot_id": r[0], "sku": r[1], "status": r[2]}

    def get_facts(self, tenant_id, session_id):
        rows = self.db.execute("SELECT k,v FROM session_facts WHERE tenant_id=? AND session_id=?", (tenant_id, session_id)).fetchall()
        return dict(rows)

    def upsert_fact(self, tenant_id, session_id, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO session_facts VALUES (?,?,?,?,?)", (tenant_id, session_id, k, v, time.time()))
            self.db.commit()
        return {"ok": True}

    # ---- mutations (only ever executed after HITL approval) ----
    def execute_mutation(self, tenant_id, tool, args, idem_key):
        with self.lock:
            prior = self.db.execute("SELECT result FROM idempotency WHERE tenant_id=? AND key=?", (tenant_id, idem_key)).fetchone()
            if prior:
                return {**json.loads(prior[0]), "duplicate": True}
            if tool == "transition_workflow":
                self.lookup_lot(tenant_id, args["lot_id"])
                self.db.execute("UPDATE inventory_lot SET status=? WHERE tenant_id=? AND lot_id=?", (args["to_status"], tenant_id, args["lot_id"]))
                result = {"lot_id": args["lot_id"], "status": args["to_status"]}
            elif tool == "submit_form":
                result = {"form": args["form"], "submitted": True, "ref": uuid.uuid4().hex[:8]}
            else:
                raise ToolError(ErrorCode.INVALID_PARAMS, f"unknown mutation {tool}")
            self.db.execute("INSERT INTO idempotency VALUES (?,?,?)", (tenant_id, idem_key, json.dumps(result)))
            self.db.commit()
            return result

    # ---- HITL ----
    def request_approval(self, tenant_id, tool, args, requested_by):
        aid = str(uuid.uuid4())
        with self.lock:
            self.db.execute("INSERT INTO pending_approvals VALUES (?,?,?,?,?,?,?,?,?)",
                            (aid, tenant_id, tool, json.dumps(args), requested_by, "PENDING_APPROVAL", time.time(), None, None))
            self.db.commit()
        self.audit(tenant_id, "HITL_REQUESTED", {"approval_id": aid, "tool": tool, "by": requested_by})
        return aid

    def list_approvals(self, tenant_id, status=None):
        q = "SELECT id,tool,args,requested_by,status,created,decided_by FROM pending_approvals WHERE tenant_id=?"
        params = [tenant_id]
        if status:
            q += " AND status=?"; params.append(status)
        return [dict(zip(["id", "tool", "args", "requested_by", "status", "created", "decided_by"], r)) for r in self.db.execute(q, params)]

    def decide(self, tenant_id, approval_id, approve: bool, reviewer: str):
        with self.lock:
            r = self.db.execute("SELECT tool,args,status,requested_by FROM pending_approvals WHERE id=? AND tenant_id=?", (approval_id, tenant_id)).fetchone()
            if not r:
                raise ToolError(ErrorCode.NOT_FOUND, "approval not found")
            if r[2] != "PENDING_APPROVAL":
                raise ToolError(ErrorCode.DUPLICATE_REQUEST, f"already {r[2]}")
            if reviewer == r[3]:
                raise ToolError(ErrorCode.RBAC_DENIED, "requester cannot approve own request")
            status = "APPROVED" if approve else "REJECTED"
            self.db.execute("UPDATE pending_approvals SET status=?,decided_by=?,decided=? WHERE id=?", (status, reviewer, time.time(), approval_id))
            self.db.commit()
        result = None
        if approve:
            args = json.loads(r[1])
            result = self.execute_mutation(tenant_id, r[0], args, args["idempotency_key"])
        self.audit(tenant_id, f"HITL_{status}", {"approval_id": approval_id, "by": reviewer})
        return {"status": status, "result": result}

    # ---- audit chain ----
    def audit(self, tenant_id, event, payload: dict):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")                       # serialise writers across processes; read the chain head inside the lock
            try:
                row = self.db.execute("SELECT hash FROM audit_log WHERE tenant_id=? ORDER BY seq DESC LIMIT 1", (tenant_id,)).fetchone()
                prev = row[0] if row else GENESIS
                body = json.dumps(payload, sort_keys=True, default=str)
                ts = time.time()
                h = hashlib.sha256((prev + tenant_id + str(ts) + event + body).encode()).hexdigest()
                self.db.execute("INSERT INTO audit_log(tenant_id,ts,event,payload,prev_hash,hash) VALUES (?,?,?,?,?,?)", (tenant_id, ts, event, body, prev, h))
                self.db.commit()
            except Exception:
                self.db.rollback(); raise

    def verify_chain(self, tenant_id) -> bool:
        prev = GENESIS
        for ts, event, payload, ph, h in self.db.execute("SELECT ts,event,payload,prev_hash,hash FROM audit_log WHERE tenant_id=? ORDER BY seq", (tenant_id,)):
            if ph != prev or hashlib.sha256((prev + tenant_id + str(ts) + event + payload).encode()).hexdigest() != h:
                return False
            prev = h
        return True

    def audit_events(self, tenant_id, event_prefix=""):
        return [r for r in self.db.execute("SELECT event,payload FROM audit_log WHERE tenant_id=? AND event LIKE ? ORDER BY seq", (tenant_id, event_prefix + "%"))]
