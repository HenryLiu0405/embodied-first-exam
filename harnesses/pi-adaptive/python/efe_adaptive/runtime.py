"""Run the optional adaptive Pi SDK in an isolated task session.

This research runner is independent of Harbor and never emits official scores.
"""

from __future__ import annotations
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time
from typing import Any
import uuid

from .telemetry.recorder import event, get_recorder, span
from .telemetry.usage import normalize_model_usage
from .endpoints import configured_base_url, validate_base_url
from .native_events import NativeEvents
from .services import LocalServices
from .pi_adaptive import PACKAGE, package_identity, resolve_profile
from .adaptive.bootstrap import BootstrapBridge
from .adaptive.service import AdaptiveService


def load_harness_config(path: str | Path | None) -> dict[str, Any]:
    """Load explicit runtime settings and reject inline credential fields."""
    if path is None:
        raise ValueError("An explicit harness configuration is required")
    value = json.loads(Path(path).expanduser().read_text())
    if not isinstance(value, dict):
        raise ValueError("Harness config must be a JSON object")
    allowed = {
        "node_command",
        "responses_base_url",
        "responses_base_url_env",
        "allow_http",
        "api_key_env",
        "auth",
        "timeout_seconds",
        "request_timeout_seconds",
        "startup_timeout_seconds",
        "context_window",
        "reasoning_effort",
        "pi_profile",
        "pi_adaptive",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown harness settings: {sorted(unknown)}; use api_key_env for credentials")
    if "allow_http" in value and not isinstance(value["allow_http"], bool):
        raise ValueError("allow_http must be a boolean")
    if value.get("auth", "bearer") not in {"azure", "bearer"}:
        raise ValueError("auth must be 'azure' or 'bearer'")
    if value.get("responses_base_url") is not None:
        validate_base_url(value["responses_base_url"], field="responses_base_url")
    for key in ("timeout_seconds", "request_timeout_seconds", "startup_timeout_seconds", "context_window"):
        if key in value and (
            isinstance(value[key], bool)
            or not isinstance(value[key], (int, float))
            or not math.isfinite(value[key])
            or value[key] <= 0
        ):
            raise ValueError(f"{key} must be positive and finite")
    if value.get("pi_profile") != "adaptive-v1":
        raise ValueError("This runner requires pi_profile=adaptive-v1")
    resolve_profile(value)
    return value


def native_connection(config: dict[str, Any]) -> tuple[str, str]:
    """Resolve a live gateway and its credential; synthetic checks do not call this."""
    url = configured_base_url(config, "responses_base_url")
    if not url:
        raise ValueError("Live native runs require an explicit responses_base_url or responses_base_url_env")
    url = validate_base_url(url, field="responses_base_url", allow_http=config.get("allow_http") is True)
    if config.get("auth", "bearer") not in {"azure", "bearer"}:
        raise ValueError("auth must be 'azure' or 'bearer'")
    key_env = config.get("api_key_env", "EFE_API_KEY")
    credential = os.environ.get(key_env)
    if not credential:
        raise ValueError(f"Missing credential environment variable: {key_env}")
    return url, credential


def native_command(harness, config):
    if harness != "pi":
        raise ValueError("Only the pinned adaptive Pi SDK is supported")
    package_identity()
    node = config.get("node_command", "node")
    if not isinstance(node, str) or not node:
        raise ValueError("node_command must name one executable")
    executable = shutil.which(node)
    if executable is None:
        raise FileNotFoundError("Node.js 22.19+ is required")
    return [str(Path(executable).resolve()), "--experimental-strip-types", str(PACKAGE / "src/runner.ts")]


def runtime_identity(harness, config):
    command = native_command(harness, config)
    return {
        "command": command,
        "executable_sha256": hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
        **package_identity(),
    }


def child_environment(home: Path) -> dict[str, str]:
    # Allowlist, not a growing blacklist of providers' secret variable names.
    """Pass system essentials and an isolated home, excluding inherited secrets."""
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "LD_LIBRARY_PATH")
        if key in os.environ
    }
    env.update(HOME=str(home), NO_COLOR="1", TERM="dumb")
    return env


class JsonProcess:
    """Own a native JSONL process with concurrent event and stderr readers."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str], events: NativeEvents):
        """Store runtime dependencies and initialize the native session boundary."""
        self.events = events
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            start_new_session=True,
        )
        self.incoming: queue.Queue = queue.Queue()
        self.stderr = deque(maxlen=30)
        self.threads = []
        for stream, kind in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            thread = threading.Thread(target=self._read, args=(stream, kind), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _read(self, stream, kind):
        """Capture native stdout immediately while the main thread runs physics."""
        for line in stream:
            line = line.rstrip("\n")
            if kind == "stdout":
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                if isinstance(row, dict):
                    try:
                        self.events.context.copy().run(self.events.consume, row)
                        self.incoming.put(("event", row))
                        continue
                    except Exception as exc:
                        self.incoming.put(("reader_error", exc))
                        return
            self.incoming.put((kind, line))

    def send(self, value):
        """Record and deliver one native control-protocol command."""
        event("native.command", kind="native", name=self.events.harness, data=value)
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def poll(self, bridge: AdaptiveService, deadline: float):
        """Pump owner-thread actions and return the next native protocol event."""
        bridge.drain()
        if time.monotonic() >= deadline:
            raise TimeoutError("Native harness episode timeout")
        try:
            kind, line = self.incoming.get(timeout=0.05)
        except queue.Empty:
            if self.process.poll() is not None:
                raise RuntimeError(f"Native process exited ({self.process.returncode}): " + "\n".join(self.stderr))
            return None
        if kind == "stderr":
            self.stderr.append(line)
            event("native.stderr", kind="native", name=self.events.harness, data={"text": line})
            return None
        if kind == "reader_error":
            raise RuntimeError("Native event recording failed") from line
        if kind != "event":
            event("native.stdout", kind="native", name=self.events.harness, data={"text": line})
            return None
        return line

    def close(self):
        """Terminate the native process group and close its pipes and readers."""
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
        for thread in self.threads:
            thread.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class NativeHarness:
    """Drive one task-local Pi session using explicit public interface access."""

    def __init__(self, harness, config, model_config):
        if harness != "pi" or config.get("pi_profile") != "adaptive-v1":
            raise ValueError("This runner supports only pi with adaptive-v1")
        allowed = {"model", "max_tokens", "send_max_tokens", "temperature", "send_temperature"}
        if not isinstance(model_config, dict) or set(model_config) - allowed:
            raise ValueError("Unknown model settings; credentials belong in environment variables")
        if not isinstance(model_config.get("model"), str) or not model_config["model"].strip():
            raise ValueError("An explicit model deployment is required")
        if "temperature" in model_config and (
            isinstance(model_config["temperature"], bool)
            or not isinstance(model_config["temperature"], (int, float))
            or not math.isfinite(model_config["temperature"])
            or not 0 <= model_config["temperature"] <= 2
        ):
            raise ValueError("temperature must be finite and between 0 and 2")
        for key in ("send_max_tokens", "send_temperature"):
            if key in model_config and not isinstance(model_config[key], bool):
                raise ValueError(f"{key} must be a boolean")
        self.harness, self.config = harness, dict(config)
        self.model_config = {"max_tokens": 8192, "send_temperature": False, **model_config}
        self.pi_profile = resolve_profile(self.config, self.model_config)

    def run(self, bootstrap, output_dir, *, responder=None, adaptive_setup=None):
        """Retain failed-attempt evidence as well as successful settlements."""
        bridge = BootstrapBridge(bootstrap)
        identity = runtime_identity(self.harness, self.config)
        url, credential = (None, None) if responder is not None else native_connection(self.config)
        deadline = time.monotonic() + float(self.config.get("timeout_seconds", 3600))
        recorder = get_recorder()
        directory = (
            (recorder.directory / "native") if recorder else Path(output_dir) / "native_sessions" / uuid.uuid4().hex
        ).resolve()
        directory.mkdir(parents=True, exist_ok=False)
        service = AdaptiveService(bridge, directory / "adaptive", deadline)
        home, workspace = directory / "home", directory / "workspace"
        home.mkdir()
        workspace.mkdir()
        process = events = None
        try:
            if adaptive_setup:
                adaptive_setup(service)
            with span(
                "harness.session",
                input={"harness": "pi-adaptive"},
                attributes={
                    "identity": identity,
                    "config": {**self.config, "responses_base_url": url},
                    "synthetic": responder is not None,
                    "official": False,
                },
            ) as session:
                with LocalServices(
                    service,
                    upstream_url=url.rstrip("/") + "/responses" if url else None,
                    api_key=credential,
                    azure=self.config.get("auth", "bearer") == "azure",
                    timeout=float(self.config.get("request_timeout_seconds", 600)),
                    responder=responder,
                    harness="pi",
                    model_options=self.model_config,
                    pi_profile=self.pi_profile,
                    deadline=deadline,
                ) as services:
                    try:
                        service.connect_reviewer(services)
                        env = child_environment(home)
                        self._pi_config(home, services, env)
                        runner_config = directory / "pi-adaptive.json"
                        runner_config.write_text(
                            json.dumps(
                                {
                                    "profile": self.pi_profile,
                                    "agent_dir": str(home / ".pi" / "agent"),
                                    "session_dir": str(directory / "sessions"),
                                    "model": self.model_config["model"],
                                    "max_actions": service.max_steps,
                                    "instruction": service.instruction,
                                    "connection": "environment",
                                }
                            )
                        )
                        env["PI_ADAPTIVE_CONFIG"] = str(runner_config)
                        events = NativeEvents("pi", services.call_names, services.requested_calls)
                        process = JsonProcess(native_command("pi", self.config), workspace, env, events)
                        self._wait_for_tools(process, services, service, deadline)
                        self._pi(process, service, deadline)
                        if not service.done:
                            service.done, service.termination = True, "agent_completed"
                        if services.calls == 0 or not services.tools_listed.is_set():
                            raise RuntimeError("Pi did not discover tools and make an observable model request")
                        result = {
                            "harness": "pi-adaptive",
                            "native_directory": str(directory),
                            "identity": identity,
                            "model_requests": services.calls,
                            "steps": service.step,
                            "raw_model_usage": services.usage,
                            "termination_reason": service.termination,
                            "tools_exposed": sorted(services.exposed),
                            "synthetic": responder is not None,
                            "official": False,
                            "task_success": None,
                            "interface": service.adapters.report(),
                        }
                        session.set_output(result)
                        return result
                    finally:
                        services.closed.set()
                        if process is not None:
                            process.close()
                        if events is not None:
                            events.close()
                        (directory / "interface-trial.json").write_text(
                            json.dumps(
                                {
                                    **service.adapters.report(),
                                    "model_requests": services.calls,
                                    "raw_model_usage": services.usage,
                                    "usage_coverage": {
                                        "requests": services.calls,
                                        "with_usage": sum(
                                            normalize_model_usage(row)["total_tokens"] is not None
                                            for row in services.usage
                                        ),
                                    },
                                    "runtime_identity": identity,
                                    "synthetic": responder is not None,
                                    "official": False,
                                    "task_success": None,
                                },
                                indent=2,
                            )
                        )
        finally:
            service.close()

    def _pi_config(self, home, services, env):
        """Configure Pi models and direct MCP tools in a fresh agent directory."""
        agent = home / ".pi" / "agent"
        agent.mkdir(parents=True)
        env["PI_CODING_AGENT_DIR"] = str(agent)
        model = {
            "id": self.model_config["model"],
            "input": ["text", "image"],
            "reasoning": True,
            "contextWindow": int(self.config.get("context_window", 131072)),
            "maxTokens": int(self.model_config.get("max_tokens", 8192)),
        }
        if self.pi_profile:
            # Disable unspecified labels; a model's provider mapping is explicit, never inferred from its name.
            model["thinkingLevelMap"] = {
                **dict.fromkeys(("off", "minimal", "low", "medium", "high", "xhigh", "max")),
                **self.pi_profile["model_capabilities"]["effort_map"],
            }
            # Transport cooldown covers HTTP and SSE throttles; the SDK owns turn retries.
            (agent / "settings.json").write_text(
                json.dumps(
                    {
                        "httpIdleTimeoutMs": int(self.config.get("request_timeout_seconds", 600) * 1000),
                        "retry": {"provider": {"maxRetries": 0}},
                    }
                )
            )
        (agent / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "efe_adaptive": {
                            "api": "openai-responses",
                            "baseUrl": services.url + "/v1",
                            "apiKey": services.bearer,
                            "models": [model],
                        }
                    }
                }
            )
        )
        (agent / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        ("environment" if self.pi_profile else "efe_adaptive"): {
                            "url": services.url + "/mcp",
                            "headers": {"Authorization": "Bearer " + services.bearer},
                            "exposure": "direct",
                            "timeout": 1200000,
                        }
                    }
                }
            )
        )

    def _wait_for_tools(self, process, services, bridge, deadline):
        """Require environment discovery before sending a task to the model."""
        timeout = float(self.config.get("startup_timeout_seconds", 30))
        with span("harness.environment_discovery", input={"timeout_seconds": timeout}):
            startup_deadline = min(deadline, time.monotonic() + timeout)
            while not services.tools_listed.is_set():
                try:
                    process.poll(bridge, startup_deadline)
                except TimeoutError as exc:
                    if time.monotonic() >= deadline:
                        raise
                    raise TimeoutError(
                        f"{self.harness} environment-tool discovery timed out after {timeout:g}s"
                    ) from exc

    def _pi(self, process, bridge, deadline):
        """Start a Pi RPC prompt and wait past retries for agent_settled."""
        process.send(
            {"id": "episode", "type": "prompt", "message": bridge.initial_prompt(), "images": bridge.initial_images()}
        )
        failed_message = None
        while True:
            row = process.poll(bridge, deadline)
            if row is None:
                continue
            if row.get("type") == "response" and row.get("success") is False:
                raise RuntimeError(f"Pi rejected request: {row.get('error')}")
            if row.get("type") == "message_end":
                message = row.get("message", {})
                if message.get("role") == "assistant":
                    failed_message = (
                        message.get("errorMessage") if message.get("stopReason") in {"error", "aborted"} else None
                    )
            if row.get("type") == "agent_settled":
                if row.get("aborted") or failed_message:
                    raise RuntimeError(f"Pi episode failed: {failed_message or 'aborted'}")
                return
