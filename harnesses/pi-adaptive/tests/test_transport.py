import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.request

import pytest

from efe_adaptive.telemetry.recorder import TraceRecorder
from trace_fixture import model_records, check_trace
from efe_adaptive.adaptive.service import AdaptiveService, AdaptiveInfrastructureError
from provider_fixture import stream_response
from efe_adaptive.adaptive.state import DEFAULT_POLICY
from binding_fixture import make_bridge, FixtureBinding
from efe_adaptive.pi_adaptive import resolve_profile
from efe_adaptive.provider_errors import ProviderBackoff, provider_failure, retry_after_seconds
from efe_adaptive.runtime import load_harness_config
from efe_adaptive.services import LocalServices

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_harness_config(ROOT / "examples/harness.json")


def make_service(path):
    bridge = make_bridge()
    return AdaptiveService(bridge, path, time.monotonic() + 30, binding=FixtureBinding(bridge))


def candidate(service):
    return service.lifecycle.propose(
        {
            "protocol": "feedback-policy-v1",
            "implementation": DEFAULT_POLICY,
            "parent": None,
            "diagnosis": "Revise action after public feedback",
            "evidence": ["observation:0"],
        }
    )["candidate_id"]


def failure_stream(payload, *, filtered=False):
    result = {"id": "failed", "object": "response", "model": payload["model"], "created_at": 0, "output": []}
    if filtered:
        result.update(status="incomplete", incomplete_details={"reason": "content_filter"})
    else:
        result.update(status="failed", error={"code": "rate_limit_exceeded", "message": "Token rate limit exceeded"})
    return [
        {"type": "response.created", "response": {**result, "status": "in_progress"}},
        {"type": "response.incomplete" if filtered else "response.failed", "response": result},
    ]


def test_review_bypasses_proxy_but_upstream_keeps_proxy_and_candidate_activates(tmp_path, monkeypatch):
    seen = []

    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(self.path)
            if self.path != "http://provider.invalid/responses":
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in stream_response(
                payload, 1, text=json.dumps({"status": "pass", "reason": "Fixed contract and evidence checked"})
            ):
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")

        def log_message(self, *_):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    for key in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(key, f"http://127.0.0.1:{proxy.server_port}")
    for key in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(key, "")
    # Rebuild the inherited global opener exactly as a new server worker would.
    monkeypatch.setattr(urllib.request, "_opener", None)
    try:
        with TraceRecorder(tmp_path / "trace", {}) as recorder:
            service = make_service(tmp_path / "adaptive")
            with LocalServices(
                service,
                upstream_url="http://provider.invalid/responses",
                api_key="test-only",
                model_options={"model": "test-model"},
                pi_profile=resolve_profile(CONFIG),
                deadline=service.deadline,
            ) as transport:
                service.connect_reviewer(transport)
                ident = candidate(service)
                assert service.lifecycle.validate(ident)["report"]["status"] == "pass"
                assert service.lifecycle.activate(ident)["active"]["feedback-policy-v1"] == ident
                assert transport.calls == 1
        assert seen == ["http://provider.invalid/responses"]
        assert len(model_records(recorder.directory)) == 1
        assert check_trace(recorder.directory)
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join()


@pytest.mark.parametrize("filtered", [False, True])
def test_review_stream_errors_recover_only_transient_failures(tmp_path, monkeypatch, filtered):
    original = ProviderBackoff.failed
    monkeypatch.setattr(
        ProviderBackoff, "failed", lambda self, f, headers=None: original(self, f, {"Retry-After": "0"})
    )
    requests = []

    def responder(payload, index):
        requests.append(payload)
        if index == 1:
            yield from failure_stream(payload, filtered=filtered)
        else:
            yield from stream_response(payload, index, text=json.dumps({"status": "pass", "reason": "checked"}))

    service = make_service(tmp_path / "adaptive")
    with TraceRecorder(tmp_path / "trace", {}) as recorder:
        with LocalServices(
            service,
            responder=responder,
            model_options={"model": "test-model"},
            pi_profile=resolve_profile(CONFIG),
            deadline=service.deadline,
        ) as transport:
            service.connect_reviewer(transport)
            ident = candidate(service)
            result = service.lifecycle.validate(ident)
            if filtered:
                assert result["report"]["status"] == "inconclusive"
                assert result["report"]["review"]["infrastructure_error"]["category"] == "content_filter"
                assert not result["release"]["local_eligible"] and ident not in service.lifecycle.health
            else:
                assert result["report"]["status"] == "pass"
                service.lifecycle.activate(ident)
                assert requests[0] == requests[1]
            assert transport.calls == (1 if filtered else 2)
    assert sum(row["data"]["status"] == "failed" for row in model_records(recorder.directory)) == 1
    assert check_trace(recorder.directory)


def test_backoff_honors_headers_window_deadline_and_close(monkeypatch):
    import efe_adaptive.provider_errors as errors

    now = [100.0]
    monkeypatch.setattr(errors.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(errors.random, "uniform", lambda *_: 0)
    backoff = ProviderBackoff()
    failure = provider_failure(error={"code": "rate_limit_exceeded"})
    assert backoff.failed(failure) == 60
    now[0] += 60
    assert backoff.failed(failure) == 120
    assert backoff.failed(failure, {"retry-after-ms": "150000"}) == 150
    assert backoff.remaining() == 150
    with pytest.raises(TimeoutError):
        backoff.wait(threading.Event(), now[0] + 100)
    closed = threading.Event()
    closed.set()
    with pytest.raises(InterruptedError):
        backoff.wait(closed, now[0] + 200)
    backoff.succeeded()
    assert backoff.remaining() == 0 and backoff.failures == 0
    assert backoff.failed(provider_failure(incomplete={"reason": "content_filter"})) == 0
    assert retry_after_seconds({"Retry-After": "nan"}) is None
    assert retry_after_seconds({"retry-after": "3.5"}) == 3.5


def test_review_transport_failure_stops_task_without_quarantining_candidate(tmp_path):
    service = make_service(tmp_path / "adaptive")
    service.lifecycle.review = lambda *_: {
        "status": "inconclusive",
        "reason": "gateway unavailable",
        "infrastructure_error": {"category": "review_transport", "retryable": False},
    }
    ident = candidate(service)
    with pytest.raises(AdaptiveInfrastructureError, match="Independent review unavailable"):
        service.call("validate", {"candidate_id": ident})
    assert service.lifecycle.reports[ident]["report"]["status"] == "inconclusive"
    assert ident not in service.lifecycle.health and service.broker.settled
    assert (
        service.broker.db.execute("SELECT COUNT(*) FROM events WHERE kind='infrastructure.failed'").fetchone()[0] == 1
    )
    service.close()
