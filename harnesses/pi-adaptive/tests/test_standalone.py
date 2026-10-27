"""The migrated package must operate without a HumanCLAW checkout or credentials."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from efe_adaptive.runtime import NativeHarness, child_environment, load_harness_config
from efe_adaptive.telemetry.recorder import TraceRecorder
from adaptive_public_fixture import PublicInstrument
from test_transport import failure_stream
from trace_fixture import check_trace, model_records

ROOT = Path(__file__).parents[1]


def test_check_cli_works_from_an_unrelated_directory_without_credentials(tmp_path):
    env = {key: value for key, value in os.environ.items() if "KEY" not in key and "TOKEN" not in key}
    env["PYTHONPATH"] = str(ROOT / "python")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "efe_adaptive.run",
            "--check",
            "--bootstrap",
            str(ROOT / "examples/bootstrap.json"),
            "--harness-config",
            str(ROOT / "examples/harness.json"),
            "--model-config",
            str(ROOT / "examples/model.json"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"valid": True, "sdk_version": "1.3.0", "official": False}
    assert list(tmp_path.iterdir()) == []


def test_credentials_are_not_inherited_by_pi(tmp_path, monkeypatch):
    monkeypatch.setenv("EFE_API_KEY", "test-only-host-secret")
    monkeypatch.setenv("UNRELATED_PRIVATE_TOKEN", "test-only-unrelated-secret")
    child = child_environment(tmp_path)
    assert "EFE_API_KEY" not in child and "UNRELATED_PRIVATE_TOKEN" not in child
    assert child["HOME"] == str(tmp_path)


@pytest.mark.parametrize(
    "change", [{"api_key": "test-only"}, {"timeout_seconds": -1}, {"timeout_seconds": True}, {"pi_profile": "native"}]
)
def test_invalid_or_secret_config_fails_before_running(tmp_path, change):
    config = json.loads((ROOT / "examples/harness.json").read_text()) | change
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_harness_config(path)


def test_model_identity_must_match_declared_capabilities():
    config = load_harness_config(ROOT / "examples/harness.json")
    with pytest.raises(ValueError, match="identity"):
        NativeHarness("pi", config, {"model": "different-model"})


def test_failed_sdk_session_keeps_trace_and_interface_report(tmp_path):
    fixture = PublicInstrument()
    config = load_harness_config(ROOT / "examples/harness.json")
    config["timeout_seconds"] = 20
    try:
        with pytest.raises(RuntimeError), TraceRecorder(tmp_path, {"synthetic": True}) as recorder:
            NativeHarness("pi", config, {"model": "test-model"}).run(
                fixture.bootstrap(),
                tmp_path,
                responder=lambda payload, index: failure_stream(payload, filtered=True),
            )
        assert check_trace(recorder.directory)
        assert model_records(recorder.directory)
        trial = json.loads((recorder.directory / "native/interface-trial.json").read_text())
        assert trial["model_requests"] >= 1
        assert trial["task_success"] is None and trial["official"] is False
        assert trial["local_adaptation_status"] == "inconclusive"
        assert fixture.actions == []
    finally:
        fixture.close()
