"""Regression coverage for adaptation without execution progress (shard-0515)."""

import json
import time

import pytest

from efe_adaptive.adaptive.service import AdaptiveService
from efe_adaptive.adaptive.state import DEFAULT_POLICY
from binding_fixture import make_bridge, FixtureBinding


def test_effectful_program_preserves_child_receipt_and_cannot_repeat_action(tmp_path):
    bridge = make_bridge()
    service = AdaptiveService(bridge, tmp_path / "adaptive", time.monotonic() + 60, binding=FixtureBinding(bridge))
    service.lifecycle.review = lambda *_: {"status": "pass", "reason": "Scripted independent review"}
    catalog = {tool["id"]: tool for tool in service.binding.catalog()}
    decision = {
        "intent": "Advance once",
        "expected_outcome": "A new public reading",
        "evidence": ["observation:0"],
        "policy_version": service.state.policy_version,
        "completion": "continue",
    }
    child = {
        "request_id": "program/1",
        "tool": "fixture.advance",
        "tool_version": catalog["fixture.advance"]["version"],
        "arguments": {"request_id": "program/1", "delta": 1},
        "decision": decision,
    }
    parent = {
        "request_id": "program",
        "tool": "runtime.program",
        "tool_version": catalog["runtime.program"]["version"],
        "arguments": {
            "source": "def run(x, tools): return tools(x)['status']",
            "input": child,
            "allowed_tools": ["fixture.advance"],
        },
        "decision": decision,
    }
    try:
        assert not service.call("execute", parent)["isError"]
        assert bridge.step == 1
        assert service.broker.receipt("program/1")["parent_id"] == "program"
        assert service.broker.receipt("program")["result"]["output"] == "completed"
        assert not service.call("execute", parent)["isError"]
        assert bridge.step == 1 and service.broker.counts["physical_operations"] == 1
        assert service.broker.evidence(["observation:1"])[0]["execution_id"] == "program/1"
    finally:
        service.close()


@pytest.fixture
def service(tmp_path):
    bridge = make_bridge()
    value = AdaptiveService(bridge, tmp_path / "adaptive", time.monotonic() + 60, binding=FixtureBinding(bridge))
    value.lifecycle.review = lambda *_: {"status": "pass", "reason": "Scripted independent verdict"}
    yield value
    value.close()


def proposal(service, *, note="Reobserve and change approach", parent=None, **overrides):
    return {
        "protocol": "feedback-policy-v1",
        "implementation": {**DEFAULT_POLICY, "recovery_note": note},
        "parent": parent,
        "diagnosis": "The predicted outcome was not observed",
        "evidence": [service.binding.observation_id],
        **overrides,
    }


def invoke(service, name, arguments):
    response = service.call(name, arguments)
    return response["isError"], json.loads(response["content"][0]["text"])


def count(service, kind):
    return service.broker.db.execute("SELECT COUNT(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]


def walk(service, ident="walk"):
    tool = next(t for t in service.binding.catalog() if t["id"] == "fixture.advance")
    return invoke(
        service,
        "execute",
        {
            "request_id": ident,
            "tool": tool["id"],
            "tool_version": tool["version"],
            "arguments": {"request_id": ident, "delta": 1},
            "decision": {
                "intent": "try the revised approach",
                "expected_outcome": "new observation",
                "evidence": [service.binding.observation_id],
                "policy_version": service.state.policy_version,
                "completion": "continue",
            },
        },
    )


def test_discover_candidate_loop_keeps_one_pending_repair_then_resumes_actions(service):
    error, created = invoke(service, "candidate", proposal(service))
    assert not error
    ident = created["value"]["candidate_id"]
    # Reproduce the original pattern with distinct diagnoses and implementations.
    for index in range(20):
        error, discovered = invoke(service, "discover", {})
        assert not error
        pending = discovered["state"]["adaptation"]["pending"]
        assert pending["candidate_id"] == ident and pending["next_action"] == "validate"
        error, rejected = invoke(
            service,
            "candidate",
            proposal(
                service,
                note=f"Reacquire the target before revised approach {index}",
                diagnosis=f"Another diagnosis {index}",
            ),
        )
        assert error and ident in rejected["error"]
        assert rejected["state"]["adaptation"]["pending"] == pending
        assert "candidate" not in rejected["state"]["adaptation"]["available_tools"]
    assert count(service, "candidate.created") == 1
    assert service.bridge.step == 0
    assert not invoke(service, "validate", {"candidate_id": ident})[0]
    assert not invoke(service, "activate", {"candidate_id": ident})[0]
    assert not walk(service)[0]
    assert service.bridge.step == 1
    assert not invoke(service, "candidate", proposal(service, note="React to the new outcome", parent=ident))[0]
    assert count(service, "candidate.created") == 2


def test_candidate_and_validation_retries_reuse_actual_status_and_review(service):
    reviews = []
    service.lifecycle.review = lambda *args: reviews.append(args) or {"status": "pass"}
    value = proposal(service)
    ident = service.lifecycle.propose(value)["candidate_id"]
    value["implementation"]["recovery_note"] = "Caller mutation must not alter the candidate"
    retry = service.lifecycle.propose(
        proposal(service, parent=ident, diagnosis="Reworded diagnosis", evidence=["observation:0", "observation:0"])
    )
    assert retry["candidate_id"] == ident and retry["status"] == "unvalidated" and retry["reused"]
    first = service.lifecycle.validate(ident)
    assert service.lifecycle.validate(ident) == first and len(reviews) == 1
    assert count(service, "validation.job") == 1
    assert service.lifecycle.propose(proposal(service))["status"] == "pass"
    assert not invoke(service, "activate", {"candidate_id": ident})[0]
    assert service.lifecycle.propose(proposal(service))["status"] == "active"
    assert not invoke(service, "activate", {"candidate_id": ident})[0]
    assert count(service, "candidate.activated") == 1
    assert service.snapshot()["adaptation"]["pending"] is None


@pytest.mark.parametrize("close", ["activate", "discard"])
def test_closed_adaptation_needs_new_environment_evidence_not_memory_or_receipt_replay(service, close):
    assert not walk(service)[0]
    value = proposal(service)
    ident = service.lifecycle.propose(value)["candidate_id"]
    if close == "activate":
        service.lifecycle.validate(ident)
        assert not invoke(service, close, {"candidate_id": ident})[0]
    else:
        assert not invoke(service, close, {"candidate_id": ident, "reason": "Use existing tools"})[0]
    assert "feedback-policy-v1" not in service.snapshot()["adaptation"]["available_protocols"]
    assert invoke(service, "candidate", proposal(service, note="Different wording"))[0]
    assert not invoke(service, "memory", {"update": {"failure": "Restate the same observation"}})[0]
    assert not invoke(service, "receipt", {"request_id": "walk"})[0]
    assert invoke(service, "candidate", proposal(service, note="Still no new outcome"))[0]
    assert not walk(service, "walk-2")[0]
    # New execution alone does not legitimize a proposal that cites only old evidence.
    assert invoke(service, "candidate", {**value, "diagnosis": "Stale diagnosis", "implementation": DEFAULT_POLICY})[0]
    assert not invoke(service, "candidate", proposal(service, note="New outcome needs repair"))[0]


@pytest.mark.parametrize("review_status", ["fail", "inconclusive"])
def test_failed_review_requires_a_corrected_child_and_keeps_report(service, review_status):
    assert not walk(service)[0]
    reviews = []
    service.lifecycle.review = lambda *args: (
        reviews.append(args) or {"status": review_status, "reason": "Repair is not justified"}
    )
    value = proposal(service)
    parent = service.lifecycle.propose(value)["candidate_id"]
    report = service.lifecycle.validate(parent)
    assert report["report"]["status"] == review_status
    assert service.lifecycle.validate(parent) == report and len(reviews) == 1
    assert service.lifecycle.propose({**value, "parent": parent})["status"] == review_status
    assert invoke(service, "candidate", {**value, "parent": parent, "evidence": ["observation:0", "observation:1"]})[0]
    assert count(service, "candidate.created") == 1
    assert invoke(service, "candidate", proposal(service, note="Unrelated new root"))[0]
    assert invoke(service, "activate", {"candidate_id": parent})[0]
    error, repaired = invoke(service, "candidate", proposal(service, note="Corrected concrete recovery", parent=parent))
    assert not error and repaired["value"]["candidate_id"] != parent
    assert service.lifecycle.reports[parent] == report
    assert repaired["state"]["adaptation"]["pending"]["candidate"]["parent"] == parent


def test_discard_preserves_evidence_and_report_without_activating(service):
    value = proposal(service)
    ident = service.lifecycle.propose(value)["candidate_id"]
    report = service.lifecycle.validate(ident)
    assert not invoke(service, "discard", {"candidate_id": ident, "reason": "No longer needed"})[0]
    assert not invoke(service, "discard", {"candidate_id": ident, "reason": "Retry"})[0]
    assert count(service, "candidate.discarded") == 1
    assert service.lifecycle.validate(ident) == report
    assert service.lifecycle.propose(value)["status"] == "discarded"
    assert invoke(service, "activate", {"candidate_id": ident})[0]
    assert service.lifecycle.active == {}
    assert not walk(service)[0]


def test_pending_repair_can_discover_an_unread_or_changed_environment_contract(service, monkeypatch):
    ident = service.lifecycle.propose(proposal(service))["candidate_id"]
    assert "discover" in service.snapshot()["adaptation"]["available_tools"]
    assert not invoke(service, "discover", {})[0]
    assert "discover" not in service.snapshot()["adaptation"]["available_tools"]
    catalog = service.binding.catalog()
    monkeypatch.setattr(service.binding, "catalog", lambda: [{**tool, "version": "changed"} for tool in catalog])
    assert "discover" in service.snapshot()["adaptation"]["available_tools"]
    assert not invoke(service, "discover", {})[0]
    state = service.snapshot()["adaptation"]
    assert "discover" not in state["available_tools"] and state["pending"]["candidate_id"] == ident


def test_runtime_defect_rolls_back_and_allows_a_corrected_child_without_an_extra_action(service):
    good_source = "def run(x, tools): return (x['target'] - x['current'] + 180) % 360 - 180"
    good = service.lifecycle.propose(proposal(service, protocol="angle-delta-v1", implementation=good_source))[
        "candidate_id"
    ]
    assert service.lifecycle.validate(good)["release"]["local_eligible"]
    service.lifecycle.activate(good)
    assert not walk(service)[0]
    bad_source = "def run(x, tools):\n if x['current'] == 999: raise ValueError('runtime defect')\n return (x['target'] - x['current'] + 180) % 360 - 180"
    bad = service.lifecycle.propose(
        proposal(service, protocol="angle-delta-v1", implementation=bad_source, parent=good)
    )["candidate_id"]
    assert service.lifecycle.validate(bad)["release"]["local_eligible"]
    service.lifecycle.activate(bad)
    error, response = invoke(service, "compute", {"candidate_id": bad, "input": {"current": 999, "target": 0}})
    assert error
    assert response["state"]["adaptation"]["active"]["angle-delta-v1"] == good
    assert response["state"]["adaptation"]["pending"]["status"] == "quarantined"
    assert service.lifecycle.compute(good, {"current": 350, "target": 10})["output"] == 20
    fixed = service.lifecycle.propose(
        proposal(service, protocol="angle-delta-v1", implementation=good_source, parent=bad)
    )
    assert fixed["candidate_id"] not in {good, bad}
    assert service.bridge.step == 1
