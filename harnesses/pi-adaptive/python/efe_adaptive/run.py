"""Run the experimental Pi harness from a public interface bootstrap."""

import argparse
import json
from pathlib import Path

from .adaptive.bootstrap import BootstrapBridge
from .pi_adaptive import package_identity
from .runtime import NativeHarness, load_harness_config
from .telemetry.recorder import TraceRecorder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--harness-config", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("outputs/interface"))
    parser.add_argument("--check", action="store_true", help="Validate inputs and pinned SDK without calling a model")
    args = parser.parse_args(argv)
    bootstrap = json.loads(args.bootstrap.read_text())
    BootstrapBridge(bootstrap)
    config = load_harness_config(args.harness_config)
    model = json.loads(args.model_config.read_text())
    harness = NativeHarness("pi", config, model)
    if args.check:
        identity = package_identity()
        print(json.dumps({"valid": True, "sdk_version": identity["version"], "official": False}))
        return 0

    recorder = TraceRecorder(
        args.output.resolve(),
        {
            "benchmark": "public-interface",
            "harness": "pi-adaptive",
            "task": bootstrap["task"],
            "official": False,
        },
    )
    with recorder:
        result = harness.run(bootstrap, recorder.directory)
        (recorder.directory / "result.json").write_text(json.dumps(result, indent=2))
        recorder.finish(
            outcome={
                "task_success": None,
                "steps": result["steps"],
                "termination_reason": result["termination_reason"],
                "official": False,
            }
        )
    print(json.dumps({"directory": str(recorder.directory), "task_outcome": "unscorable", "official": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
