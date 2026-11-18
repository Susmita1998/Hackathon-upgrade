"""Lifecycle plugin registry (Pluggy). Hot-toggleable, ordered by priority, fail-closed where configured."""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass, field
import pluggy
from . import config as C
from .models import ErrorCode, ToolError
from .security import InjectionShield, redact_pii

PROJECT = "unreliable_assistant"
hookspec = pluggy.HookspecMarker(PROJECT)
hookimpl = pluggy.HookimplMarker(PROJECT)


class Specs:
    @hookspec
    def pre_execution(self, ctx, text): ...
    @hookspec
    def tool_dispatch(self, ctx, tool, args_hash): ...
    @hookspec
    def tool_result(self, ctx, tool, args_hash, ok): ...
    @hookspec
    def post_execution(self, ctx, response, evidence): ...


@dataclass
class RequestContext:
    tenant_id: str
    user_id: str
    session_id: str
    trace: object
    role: str = "operator"
    meta: dict = field(default_factory=dict)

    @property
    def max_tier(self) -> str:
        return {"user": "TIER_2_COMPUTE", "operator": "TIER_3_MUTATE", "reviewer": "TIER_3_MUTATE"}.get(self.role, "TIER_1_READ")


def args_hash(tool: str, args: dict) -> str:
    return hashlib.sha1((tool + json.dumps(args, sort_keys=True, default=str)).encode()).hexdigest()[:16]


class Plugin:
    name = "plugin"; stage = "PRE_EXECUTION"; priority = 50; fail_closed = False
    enabled = True


class PiiRedactor(Plugin):
    name, stage, priority, fail_closed = "pii_redactor", "PRE_EXECUTION", 10, True

    @hookimpl
    def pre_execution(self, ctx, text):
        res = redact_pii(text)
        ctx.meta["pii"] = res.counts
        return res.text


class InjectionShieldPlugin(Plugin):
    name, stage, priority, fail_closed = "injection_shield", "PRE_EXECUTION", 20, True

    def __init__(self, shield: InjectionShield):
        self.shield = shield

    @hookimpl
    def pre_execution(self, ctx, text):
        v = self.shield.scan(text, session_key=f"{ctx.tenant_id}:{ctx.session_id}")
        if v.blocked:
            ctx.meta["block_reason"] = v.reason
            raise ToolError(ErrorCode.POLICY_BLOCKED, "Request blocked by injection shield", {"reason": v.reason})
        return text


class CircuitBreaker(Plugin):
    name, stage, priority = "circuit_breaker", "TOOL_DISPATCH", 10

    def __init__(self, max_identical_calls: int = C.BREAKER_MAX_FAILS):
        self.max = max_identical_calls
        self.fails: dict[str, int] = {}

    def _k(self, ctx, tool, h): return f"{ctx.tenant_id}:{ctx.session_id}:{tool}:{h}"

    @hookimpl
    def tool_dispatch(self, ctx, tool, args_hash):
        if self.fails.get(self._k(ctx, tool, args_hash), 0) >= self.max:
            raise ToolError(ErrorCode.BREAKER_OPEN, "Circuit breaker open for repeated identical failing call",
                            {"remediation": "Check the SKU/LOT ID and rephrase; this call was failing repeatedly and has been stopped."})

    @hookimpl
    def tool_result(self, ctx, tool, args_hash, ok):
        k = self._k(ctx, tool, args_hash)
        if ok:
            self.fails.pop(k, None)
        else:
            self.fails[k] = self.fails.get(k, 0) + 1

    def reset(self):
        self.fails.clear()

    @property
    def open_keys(self):
        return [k for k, v in self.fails.items() if v >= self.max]


class RagasAuditor(Plugin):
    name, stage, priority = "ragas_auditor", "POST_EXECUTION", 10

    def __init__(self, min_faithfulness: float = C.MIN_FAITHFULNESS):
        self.min = min_faithfulness
        self.scores: list[float] = []

    @hookimpl
    def post_execution(self, ctx, response, evidence):
        from .verify import faithfulness
        if response.status == "OK" and evidence and response.faithfulness is None:
            f = faithfulness(response.answer, evidence)
            response.faithfulness = round(f, 4)
            self.scores.append(f)


class OtelExporter(Plugin):
    name, stage, priority = "otel_exporter", "ALL_SPANS", 90

    def __init__(self, telemetry):
        self.t = telemetry

    @hookimpl
    def post_execution(self, ctx, response, evidence):
        self.t.finish(ctx.trace, status=response.status, route=response.route, tenant=ctx.tenant_id)


class PluginManager:
    def __init__(self):
        self.pm = pluggy.PluginManager(PROJECT)
        self.pm.add_hookspecs(Specs)
        self.plugins: list[Plugin] = []

    def register(self, plugin: Plugin):
        self.pm.register(plugin, name=plugin.name)
        self.plugins.append(plugin)
        self.plugins.sort(key=lambda p: p.priority)
        return plugin

    def get(self, name):
        return next(p for p in self.plugins if p.name == name)

    def toggle(self, name: str, enabled: bool):
        self.get(name).enabled = enabled

    def status(self):
        return [{"name": p.name, "stage": p.stage, "priority": p.priority, "enabled": p.enabled,
                 "fail_closed": p.fail_closed} for p in self.plugins]

    def _active(self, stage, method):
        return [p for p in self.plugins if p.enabled and hasattr(p, method) and (p.stage == stage or p.stage == "ALL_SPANS")]

    def run_pre(self, ctx, text: str) -> str:
        for p in self._active("PRE_EXECUTION", "pre_execution"):
            try:
                text = p.pre_execution(ctx, text)
            except ToolError:
                raise
            except Exception as e:                                  # plugin crashed
                if p.fail_closed:
                    raise ToolError(ErrorCode.POLICY_BLOCKED, f"{p.name} failed closed", {"error": str(e)})
        return text

    def on_dispatch(self, ctx, tool, h):
        for p in self._active("TOOL_DISPATCH", "tool_dispatch"):
            p.tool_dispatch(ctx, tool, h)

    def on_result(self, ctx, tool, h, ok):
        for p in self._active("TOOL_DISPATCH", "tool_result"):
            p.tool_result(ctx, tool, h, ok)

    def run_post(self, ctx, response, evidence):
        for p in [x for x in self.plugins if x.enabled and hasattr(x, "post_execution")]:
            short = {"ragas_auditor": "ragas", "otel_exporter": "otel"}.get(p.name, p.name)
            if p.name == "otel_exporter":                     # exporter runs last so it sees every span, including hook.post.ragas
                p.post_execution(ctx, response, evidence)
            else:
                with ctx.trace.span(f"hook.post.{short}"):
                    p.post_execution(ctx, response, evidence)
