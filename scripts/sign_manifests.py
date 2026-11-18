"""Sign the three MCP server manifests. Re-run after any change to a server or the code it imports (that is the point)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ["LLM_MODE"] = "off"
from agent.manifest import MANIFESTS, PUBKEY, sign_all, verify_server
from agent.service import AgentService

svc = AgentService(llm="off", verify_manifests=False)
doc = sign_all(list(svc.gw.servers.values()))
bad = [(s.name, verify_server(s)) for s in svc.gw.servers.values() if not verify_server(s)[0]]
print(f"signed {len(doc['servers'])} manifests with key {doc['key_id']} -> {MANIFESTS.name}; public key {PUBKEY.name}; verify failures: {bad or 'none'}")
