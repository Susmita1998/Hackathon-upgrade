# The Unreliable Assistant → Enterprise Grade

Spec: [design.md](design.md) (Gate 1) · Diagram: [docs/workflow_orchestration.png](docs/workflow_orchestration.png) · Results: [docs/audit_report.md](docs/audit_report.md)

**Status:** end-to-end build. With `OPENROUTER_API_KEY` set the API makes **real LLM calls at every agentic step** (triage, ReAct planner, generator, independent verifier, direct reply) through a model router with fail-over and local fallbacks (design.md ADR-06). Embeddings, reranking, Redis and the database are still local stand-ins (hash-embedder, lexical cross-encoder, fakeredis, SQLite); the workspace guardrail blocks OpenRouter embedding/rerank models.
The 50,000-VU load run, 1-hour soak, real LLM TTFT and the SLM fine-tune are **not done**; the audit report lists exactly what is measured vs pending.

## Live LLM (OpenRouter)
```bash
export OPENROUTER_API_KEY=...        # read from the environment only; never stored or logged
export OR_BUDGET_USD=3.00            # hard spend cap per process (default 3.00); beyond it every step uses its local fallback
export LLM_MODE=auto                 # auto = live if a key is set | openrouter | off (offline deterministic stubs; used by the tests)
python3 -I eval/run_ragas.py         # REAL RAGAS (ragas library; judge claude-haiku-4.5, local BGE embeddings) -> reports/ragas_live.json (billed, ~$0.5-1)
python3 -I eval/run_llm_eval.py      # real-LLM evaluation -> reports/llm_eval.json (costs real money; --quick for a ~$0.06 smoke)
```
Allowed models in this workspace: `anthropic/claude-haiku-5.5`, `claude-haiku-4.5`, `claude-sonnet-5.5`. Override chains with `OR_MODELS_<ROLE>=a,b` (roles: triage, plan, direct, generate, generate_strong, verify, judge).

## Real MCP servers, signed manifests, memory
```bash
python3 -I scripts/sign_manifests.py                  # re-sign after changing a server or code it imports (private key: .keys/, git-ignored)
MCP_TRANSPORT=stdio python3 -I -m uvicorn agent.api:app --port 8000   # API calls the 3 MCP servers as real stdio subprocesses (default: in-process)
curl localhost:8000/api/v1/mcp/verify                 # manifest signature + source-hash check, per server
```
Pass a `session_id` in the request to use conversation memory (last 3 turns, per tenant+session).

## Run
```bash
pip install -r requirements.txt
python3 -I scripts/build_corpus.py && python3 -I scripts/build_redteam.py   # vault, graph.json, datasets
LLM_MODE=off python3 -I -m pytest tests -q                                  # 75 offline tests (live paths use a scripted fake client)
python3 -I -m uvicorn agent.api:app --port 8000                             # API: POST /api/v1/agent/query
python3 -I -m streamlit run app.py                                          # 6-tab workbench
python3 -I load_tests/smoke_load.py --vus 100 --seconds 20 --varied         # small smoke load (needs API running)
python3 -I eval/run_kpis.py --smoke-json reports/load_smoke.json            # 36-KPI scorecard -> reports/
python3 -I scripts/make_audit_report.py                                     # docs/audit_report.md
python3 -I scripts/gen_workflow.py docs/workflow_orchestration              # regenerate diagram (needs graphviz)
```
Real MCP servers: `python3 mcp_servers/rag_server.py` (stdio), `graphify_server.py` (stdio), `state_store_server.py` (streamable-http :8081).
Full 50K test: `k6 run load_tests/k6_50k_load.js` and the JMeter plan in `load_tests/` on distributed injectors.

## Layout
`agent/` core (rag, harness, loop, plugins, mcp, state, cache, triage, service, api) · `mcp_servers/` · `knowledge_vault/` + `graph.json` · `data/` · `eval/` · `tests/` · `load_tests/` · `reports/`

API roles via `X-Role` (`user` T1-T2, `operator` can request Tier 3, `reviewer` can approve). `X-Tenant-ID` must match the body `tenant_id`.
