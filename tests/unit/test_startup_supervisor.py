from datetime import UTC, datetime

import pytest

from okxq.runtime.startup import SHUTDOWN_ORDER, STEP_ORDER, ShutdownSequence, StartupSequence, StepResult
from okxq.runtime.supervisor import HealthAggregator, OperatorRequest, Supervisor

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def steps(fail_at: str | None = None):
    calls: list[str] = []

    def mk(name):
        async def fn():
            calls.append(name)
            if name == fail_at:
                return StepResult(ok=False, detail="échec simulé")
            return StepResult(ok=True)

        return fn

    return {n: mk(n) for n in STEP_ORDER}, calls


async def test_startup_runs_in_order_and_authorizes_only_when_all_pass(clock):
    s, calls = steps()
    report = await StartupSequence(clock, s).run()
    assert calls == list(STEP_ORDER) and report.entries_authorized


async def test_T33_startup_stops_at_reconciliation_failure(clock):
    s, calls = steps(fail_at="bootstrap_reconcile")
    report = await StartupSequence(clock, s).run()
    assert not report.entries_authorized
    assert calls[-1] == "bootstrap_reconcile" and "authorize_entries" not in calls


async def test_startup_exception_is_blocking(clock):
    s, _ = steps()

    async def boom():
        raise RuntimeError("db down")

    s["verify_environment"] = boom
    report = await StartupSequence(clock, s).run()
    assert not report.entries_authorized and report.steps[-1]["step"] == "verify_environment"


def test_missing_steps_rejected(clock):
    with pytest.raises(ValueError):
        StartupSequence(clock, {})


async def test_shutdown_policy_is_explicit(clock):
    async def ok():
        return StepResult(ok=True)

    seq = ShutdownSequence(
        clock, {n: ok for n in SHUTDOWN_ORDER}, position_policy="keep_positions_supervised"
    )
    report = await seq.run()
    assert [s["step"] for s in report.steps] == list(SHUTDOWN_ORDER)
    with pytest.raises(ValueError):
        ShutdownSequence(clock, {n: ok for n in SHUTDOWN_ORDER}, position_policy="whatever")


async def test_supervisor_roles_reasons_and_results(clock):
    async def pause(req):
        return {"status": "APPLIED", "halt_level": "SOFT_HALT"}

    async def resume(req):
        return {"status": "PENDING", "preconditions": ["reconciliation_ok"]}

    sup = Supervisor(clock=clock, handlers={"pause": pause, "request_resume": resume})
    ok = await sup.process(OperatorRequest("r1", "pause", "account", "maintenance", "op@x", "operator", T0))
    assert ok.status == "APPLIED"
    denied = await sup.process(OperatorRequest("r2", "pause", "account", "x", "reader@x", "reader", T0))
    assert denied.status == "REFUSED"
    noreason = await sup.process(OperatorRequest("r3", "pause", "account", "  ", "op@x", "operator", T0))
    assert noreason.status == "REFUSED"
    pending = await sup.process(OperatorRequest("r4", "request_resume", "account", "ok", "op@x", "admin", T0))
    assert pending.status == "PENDING"  # une requête acceptée n'est pas une reprise effectuée
    unknown = await sup.process(OperatorRequest("r5", "activate_live", "account", "x", "op@x", "admin", T0))
    assert unknown.status == "REFUSED"
    with pytest.raises(ValueError):
        Supervisor(clock=clock, handlers={"activate_live": pause})


def test_health_readiness_requires_critical_components():
    h = HealthAggregator(critical=frozenset({"gateway", "reconciliation"}))
    h.set("gateway", "OK", "connecté")
    assert not h.ready()
    h.set("reconciliation", "WARN", "en cours")
    assert not h.ready() and h.live()
    h.set("reconciliation", "OK")
    assert h.ready()
    with pytest.raises(ValueError):
        h.set("x", "GREEN")
