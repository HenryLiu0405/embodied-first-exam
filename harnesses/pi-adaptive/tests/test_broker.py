import json
from pathlib import Path
import socket
import time
import pytest
from efe_adaptive.adaptive.broker import Broker, digest
from efe_adaptive.adaptive.lifecycle import Lifecycle
from efe_adaptive.adaptive.sandbox import run_program
from efe_adaptive.adaptive.state import TaskState, DEFAULT_POLICY
from efe_adaptive.pi_adaptive import resolve_profile, package_identity
from efe_adaptive.runtime import load_harness_config

CONFIG = Path(__file__).parents[1] / "examples/harness.json"


class TextBinding:
    def __init__(self):
        self.observation_id = "text:0"
        self.calls = 0
        self.version = "v1"
        self.crash = False
        self.effect = "observe"

    def catalog(self):
        return [
            {
                "id": "fixture.inspect",
                "version": self.version,
                "readiness": "ready",
                "effect": self.effect,
                "requires_current_observation": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"advance": {"type": "boolean"}},
                    "required": ["advance"],
                    "additionalProperties": False,
                },
            }
        ]

    def execute(self, tool, arguments):
        self.calls += 1
        if self.crash:
            raise RuntimeError("Lost connection after dispatch")
        self.observation_id = f"text:{self.calls}"
        return {
            "observations": [{"id": self.observation_id, "channels": ["text"]}],
            "control_steps": int(arguments["advance"]),
            "isError": False,
        }


def request(binding, ident="r1"):
    return {
        "request_id": ident,
        "tool": "fixture.inspect",
        "tool_version": binding.version,
        "arguments": {"advance": True},
        "decision": {
            "intent": "inspect",
            "expected_outcome": "new reading",
            "evidence": [binding.observation_id],
            "policy_version": digest(DEFAULT_POLICY),
            "completion": "continue",
        },
    }


def test_durable_duplicate_conflict_lost_receipt(tmp_path):
    binding = TextBinding()
    path = tmp_path / "journal.sqlite"
    broker = Broker(path, binding, time.monotonic() + 30)
    req = request(binding)
    first = broker.execute(req)
    assert first["status"] == "completed" and first["counts"] == dict(
        compute=0, observations=1, physical_operations=0, control_steps=1
    )
    assert broker.execute(req) == first and binding.calls == 1
    with pytest.raises(ValueError, match="conflict"):
        broker.execute({**req, "arguments": {"advance": False}})
    broker.close()
    reopened = Broker(path, binding, time.monotonic() + 30)
    assert reopened.execute(req) == first and binding.calls == 1


def test_uncertain_execution_blocks_replay_and_version_activation(tmp_path):
    binding = TextBinding()
    binding.crash = True
    broker = Broker(tmp_path / "journal.sqlite", binding, time.monotonic() + 30)
    req = request(binding)
    with pytest.raises(RuntimeError):
        broker.execute(req)
    assert broker.execute(req)["status"] == "indeterminate" and binding.calls == 1
    assert broker.execute(request(binding, "r2"))["status"] == "rejected"
    assert not broker.settled
    with pytest.raises(ValueError, match="settled"):
        Lifecycle(tmp_path / "candidates", broker).activate("anything")


@pytest.mark.parametrize("change", ["version", "evidence", "deadline", "effect", "schema"])
def test_admission_never_dispatches_invalid_contract(tmp_path, change):
    binding = TextBinding()
    broker = Broker(tmp_path / "journal.sqlite", binding, time.monotonic() + 30)
    req = request(binding)
    if change == "version":
        binding.version = "v2"
    if change == "evidence":
        req["decision"]["evidence"] = ["text:99"]
    if change == "deadline":
        broker.deadline = time.monotonic() - 1
    if change == "effect":
        binding.effect = "unknown"
    if change == "schema":
        req["arguments"] = {"extra": 1}
    assert broker.execute(req)["status"] == "rejected" and binding.calls == 0


def test_interrupted_intent_is_not_replayed(tmp_path):
    binding = TextBinding()
    broker = Broker(tmp_path / "journal.sqlite", binding, time.monotonic() + 30)
    req = request(binding)
    broker.db.execute("INSERT INTO requests VALUES (?,?,?,?,NULL)", ("r1", digest(req), "running", json.dumps(req)))
    broker.db.commit()
    broker.close()
    reopened = Broker(tmp_path / "journal.sqlite", binding, time.monotonic() + 30)
    assert reopened.execute(req)["status"] == "indeterminate" and binding.calls == 0


def test_evidence_provenance_and_post_action_freshness(tmp_path):
    binding = TextBinding()
    broker = Broker(tmp_path / "j.sqlite", binding, time.monotonic() + 30)
    state = TaskState(broker, "Inspect")
    with pytest.raises(ValueError):
        state.update({"completion": "supported"})
    with pytest.raises(ValueError):
        state.update({"claims": [{"text": "truth", "status": "observed", "evidence": ["text:0"]}]})
    state.update(
        {
            "claims": [{"text": "reading plausible", "status": "inferred", "evidence": ["text:0"]}],
            "feedback": "supported",
        }
    )
    assert state.snapshot()["recommendation"]["effort"] == "medium"
    state.update(
        {
            "claims": [{"text": "prediction failed", "status": "contradicted", "evidence": ["text:0"]}],
            "feedback": "contradicted",
        }
    )
    assert state.snapshot()["recommendation"]["effort"] == "high"


def test_os_isolation_private_files_network_oracle_and_environment(tmp_path, monkeypatch):
    sentinel = tmp_path / "private-evaluator.txt"
    sentinel.write_text("secret evaluator")
    monkeypatch.setenv("EFE_API_KEY", "fake-test-sentinel")
    code = """import os, socket
def run(x, tools):
    result = {"credential": os.environ.get("EFE_API_KEY"), "private_read": False, "private_write": False, "network": False}
    try:
        open(x['path']).read(); result["private_read"] = True
    except OSError: pass
    try:
        open(x['path'], "w").write("tamper"); result["private_write"] = True
    except OSError: pass
    try:
        s=socket.socket(); s.settimeout(.2); s.connect(("127.0.0.1",x['port'])); result["network"] = True
    except OSError: pass
    return result
"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert run_program(code, {"path": str(sentinel), "port": listener.getsockname()[1]}) == {
            "credential": None,
            "private_read": False,
            "private_write": False,
            "network": False,
        }
    assert sentinel.read_text() == "secret evaluator"


def test_generated_children_only_use_broker_and_record_parent(tmp_path):
    binding = TextBinding()
    broker = Broker(tmp_path / "j.sqlite", binding, time.monotonic() + 30)
    req = request(binding)
    req["parent_id"] = "program:test"
    got = run_program("def run(x, tools): return tools(x)", req, invoke=broker.execute)
    assert got["parent_id"] == "program:test" and binding.calls == 1
    with pytest.raises(ValueError, match="forbidden"):
        run_program("def run(x, tools): return tools(x)", req)


def test_validation_repair_activation_and_scope(tmp_path):
    broker = Broker(tmp_path / "j.sqlite", TextBinding(), time.monotonic() + 60)
    lifecycle = Lifecycle(tmp_path / "candidates", broker)
    base = {
        "protocol": "turn-adapter-v1",
        "parent": None,
        "evidence": ["text:0"],
        "diagnosis": "wrong signed turn",
        "implementation": "def run(x, tools): return {'direction':'left','degrees':20}",
    }
    bad = lifecycle.propose(base)["candidate_id"]
    assert lifecycle.validate(bad)["report"]["status"] == "fail"
    with pytest.raises(ValueError):
        lifecycle.activate(bad)
    good = lifecycle.propose(
        {
            **base,
            "parent": bad,
            "implementation": "def run(x, tools):\n a=x['angle']; return {'direction':'left' if a>=0 else 'right','degrees':max(10,min(120,abs(a)))}",
        }
    )["candidate_id"]
    report = lifecycle.validate(good)
    assert report["release"]["local_eligible"] and not report["release"]["publish_eligible"]
    lifecycle.activate(good)
    assert lifecycle.compute(good, {"angle": -30})["output"] == {"direction": "right", "degrees": 30}


def test_independent_review_cannot_override_failed_contract(tmp_path):
    broker = Broker(tmp_path / "j.sqlite", TextBinding(), time.monotonic() + 30)
    life = Lifecycle(tmp_path / "c", broker)
    seen = []
    life.review = lambda *args: seen.append(args) or {"status": "pass"}
    bad = life.propose(
        {
            "protocol": "feedback-policy-v1",
            "implementation": {**DEFAULT_POLICY, "completion_requires_evidence": False},
            "parent": None,
            "diagnosis": "skip check",
            "evidence": ["text:0"],
        }
    )["candidate_id"]
    assert not life.validate(bad)["release"]["local_eligible"] and not seen
    good = life.propose(
        {
            "protocol": "feedback-policy-v1",
            "implementation": DEFAULT_POLICY,
            "parent": bad,
            "diagnosis": "prediction failure",
            "evidence": ["text:0"],
        }
    )["candidate_id"]
    assert life.validate(good)["release"]["local_eligible"] and len(seen) == 1


def test_configuration_replaces_prototype_and_rejects_reasoning_quotas():
    config = load_harness_config(CONFIG)
    assert resolve_profile(config, {"model": "test-model"})["version"] == "pi-adaptive-v1"
    for old in ["embodied-v1", "typo"]:
        with pytest.raises(ValueError):
            resolve_profile({"pi_profile": old})
    for key in ("max_high_fraction", "reasoning_deadline_seconds"):
        changed = {**config, "pi_adaptive": {**config["pi_adaptive"], key: 1}}
        with pytest.raises(ValueError):
            resolve_profile(changed)
    assert package_identity()["profile"] == "pi-adaptive-v1"
