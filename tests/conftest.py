import asyncio, os, sys
os.environ["LLM_MODE"] = "off"
os.environ.setdefault("MCP_TIMEOUT_S", "3"); os.environ.setdefault("STEP_TIMEOUT_S", "3")   # generous: tests must not flake when the box is loaded; timeout behaviour is tested explicitly          # tests are offline and deterministic; live-LLM behaviour is tested with a scripted fake client
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
from agent.models import QueryRequest
from agent.service import AgentService


def run(coro):
    return asyncio.run(coro)


def ask(svc, q, tenant="tenant_1", user="u1", role="operator", session=None):
    return run(svc.handle(QueryRequest(tenant_id=tenant, user_id=user, query=q, session_id=session), role=role))


@pytest.fixture(scope="module")
def svc():
    return AgentService()


@pytest.fixture()
def fresh():
    return AgentService()
