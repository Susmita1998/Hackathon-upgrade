"""Signed MCP server manifests (Gate 13). Each server's manifest lists its tools/resources and the SHA-256 of every source
file that implements it; the canonical JSON is signed with Ed25519. The gateway verifies signature, source hashes and the live
tool set at startup (fail closed: a server that does not verify is marked unhealthy and never called).

Sign:   python3 -I scripts/sign_manifests.py      (private key lives in .keys/, gitignored; public key is committed)
Verify: automatic in McpGateway / on GET /api/v1/mcp/verify
"""
from __future__ import annotations
import base64, hashlib, json
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = ROOT / "mcp_servers/manifests.json"
PUBKEY = ROOT / "mcp_servers/manifest_pub.pem"
PRIVKEY = ROOT / ".keys/manifest_ed25519.pem"
SOURCES = {
    "hybrid-rag": ["mcp_servers/rag_server.py", "agent/mcp.py", "agent/rag.py", "agent/security.py", "agent/config.py", "agent/models.py"],
    "graphify-vault": ["mcp_servers/graphify_server.py", "agent/mcp.py", "agent/graphify.py", "agent/rag.py", "agent/config.py", "agent/models.py"],
    "state-store": ["mcp_servers/state_store_server.py", "agent/mcp.py", "agent/state.py", "agent/models.py"],
}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def file_hash(rel: str) -> str:
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()


def build_manifest(server) -> dict:
    return {"name": server.name, "version": "1.0", "protocol": "mcp/json-rpc-2.0", "tools": sorted(server.tools), "resources": sorted(server.resources),
            "sources": {rel: file_hash(rel) for rel in SOURCES[server.name]}}


def generate_keypair() -> None:
    PRIVKEY.parent.mkdir(exist_ok=True)
    key = ed25519.Ed25519PrivateKey.generate()
    PRIVKEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    PRIVKEY.chmod(0o600)
    PUBKEY.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def sign_all(servers) -> dict:
    if not PRIVKEY.exists():
        generate_keypair()
    priv = serialization.load_pem_private_key(PRIVKEY.read_bytes(), None)
    out = {}
    for s in servers:
        m = build_manifest(s)
        out[s.name] = {"manifest": m, "signature": base64.b64encode(priv.sign(canonical(m))).decode()}
    doc = {"alg": "Ed25519", "key_id": hashlib.sha256(PUBKEY.read_bytes()).hexdigest()[:16], "servers": out}
    MANIFESTS.write_text(json.dumps(doc, indent=1, sort_keys=True))
    return doc


def verify_server(server, manifests_path: Path = MANIFESTS, pubkey_path: Path = PUBKEY) -> tuple[bool, str]:
    """Returns (ok, reason). Checks signature, every source-file hash, and that the live tool/resource set matches."""
    try:
        doc = json.loads(manifests_path.read_text())
        entry = doc["servers"][server.name]
        pub = serialization.load_pem_public_key(pubkey_path.read_bytes())
        pub.verify(base64.b64decode(entry["signature"]), canonical(entry["manifest"]))
    except FileNotFoundError:
        return False, "manifest or public key missing"
    except KeyError:
        return False, "server not listed in manifest"
    except InvalidSignature:
        return False, "signature invalid (manifest tampered or signed with another key)"
    except Exception as e:
        return False, f"manifest unreadable: {type(e).__name__}"
    m = entry["manifest"]
    for rel, h in m["sources"].items():
        try:
            if file_hash(rel) != h:
                return False, f"source changed since signing: {rel}"
        except FileNotFoundError:
            return False, f"source missing: {rel}"
    if sorted(server.tools) != m["tools"] or sorted(server.resources) != m["resources"]:
        return False, "live tool/resource set differs from signed manifest"
    return True, "ok"
