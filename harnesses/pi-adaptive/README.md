# Experimental adaptive Pi harness

A standalone migration of the task-local adaptive harness developed in
HumanCLAW. It starts Pi 1.3.0 with a public task instruction, documentation
entries and HTTP JSON or MCP transport handles. The agent discovers the
interface, proposes an immutable binding, validates and probes it, and then
acts through the registered operations. No HumanCLAW checkout, simulator,
model deployment configuration or private data is required.

**Status:** optional research tooling. This is not a Harbor agent integration
and does not produce official Embodied First Exam results. The
[official-run requirements](../../docs/protocol/running.md#official-runs)
continue to require Harbor's native agents and the official usage gateway.
This module has its own model transport and adaptive tools; it is not selected
by `efe`, `harbor run` or the existing batch scripts. No task, verifier, cutoff,
network policy or existing execution path is changed.

For the process diagram, module responsibilities, interface lifecycle, and a
file-by-file review/testing guide, read [ARCHITECTURE.md](ARCHITECTURE.md).

## Install and check

Tested with Python 3.13 and Node.js 26 on macOS. The SDK requires Node.js
22.19 or newer. Generated Python programs require `sandbox-exec` on macOS or
`bubblewrap` with usable user namespaces on Linux; execution fails if an OS
sandbox is unavailable. There is no unsandboxed fallback. The migration has
not been validated on a Linux simulator or an official Harbor task.

From the repository root:

```sh
cd harnesses/pi-adaptive
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm ci --ignore-scripts
.venv/bin/python -m pytest -q
npm run check
npm test
.venv/bin/ruff format --check python tests
```

The tests use temporary local fixtures and scripted Responses events. The
end-to-end test runs the real pinned Pi SDK through discovery, independent
review, probing, registration and execution. It makes no paid API calls and
does not measure autonomous model capability or benchmark success.

## Configure a session

The three files in [`examples/`](examples/) are placeholders:

- `bootstrap.json`: the public instruction, documentation/source roots and
  available transport addresses. It supplies no action map, hidden state,
  success predicate, adapter or initial observation. HTTP transports support
  JSON; MCP transports support Streamable HTTP. `max_actions` is an optional
  research-session limit, not an EFE task cutoff.
- `harness.json`: model transport, task deadline, explicit medium/high effort
  mapping and context/image limits. Set `model_capabilities.model` to the
  actual Responses deployment. Use `auth: "azure"` for an `api-key` header,
  or `"bearer"` for bearer authorization.
- `model.json`: the same model deployment and its output-token settings.

Validate the files and installed SDK without contacting a model or the example
URLs:

```sh
PYTHONPATH=python .venv/bin/python -m efe_adaptive.run --check \
  --bootstrap examples/bootstrap.json \
  --harness-config examples/harness.json \
  --model-config examples/model.json
```

For an actual research session, replace the example addresses and instruction,
set `EFE_RESPONSES_BASE_URL` to your provider's Responses base URL (ending before
`/responses`) and supply its key in `EFE_API_KEY`. Then run:

```sh
PYTHONPATH=python .venv/bin/python -m efe_adaptive.run \
  --bootstrap my-bootstrap.json \
  --harness-config my-harness.json \
  --model-config my-model.json \
  --output outputs/interface
```

The runner reads the named environment variables; it does not load a repository
`.env` or choose a cloud endpoint implicitly. Pi receives only a temporary
loopback session token. Generated programs receive neither that token nor the
provider key. A transport's optional `credential_env` names a separate host
credential for that service.

`internet: false` limits documentation access to declared `source_roots`;
local file/server roots must be explicitly granted. The example disables web
search. Enabling public discovery is a research-session setting and does not
override any benchmark/container network restriction. Only expose public
interface documentation to the harness, never an evaluator or reference
solution directory.

## Runtime behavior

- **Discovery and scientific workspace:** retain source identities and hashes,
  reuse inspected clients, preserve original RGB-D bytes, dtype, shape, units
  and calibration, and keep task-local programs and outputs. Package setup
  records exact installed versions; it can copy an installed package or fetch
  a pinned wheel when explicitly requested.
- **Binding lifecycle:** proposals are immutable. Deterministic checks and an
  independent model review precede probes; registration exposes only the
  verified scope. Repeated reads, proposals and validation requests reuse
  existing records. Changed interfaces invalidate affected bindings, with
  corrected children or compatible checked parents available for repair.
- **Execution journal:** SQLite receipts are committed before return. Identical
  retries reuse receipts, conflicting request identities are rejected, and
  uncertain dispatch is reconciled through a reviewed status operation rather
  than repeated. A received response with a broken decoder retains its raw
  data and completed transport receipt.
- **Context and policy:** the host retains evidence and pending lifecycle state;
  Pi advertises the available next tools. A second physical operation or a
  completion claim must wait for a model turn that has seen the prior result.
  Explicit multi-operation programs use reviewed, broker-mediated child calls.
- **Accounting:** provider attempts and independent reviews share one meter.
  Explicit transient provider failures use the existing cooldown behavior.
  Authentication, filtering and ambiguous execution failures are not treated
  as permission to repeat physical work.

## Output and limits

Every invocation creates a fresh directory under `outputs/interface/experiment_runs/`:
`run.json`, append-only `events.jsonl`, content-addressed trace artifacts,
`native/interface-trial.json`, and the adaptive journal/workspace. Successful
sessions also leave `result.json`. Failed sessions retain their trace and,
once the local runtime has started, interface evidence. Outputs are ignored by
Git and are not included in this contribution.

`local_adaptation_status` describes verified interaction with the discovered
interface. `task_success` remains `null`, `task_outcome` remains `unscorable`,
and `official` is `false`; only an independent evaluator can provide a task
verdict. Scripted fixture passes are not EFE evaluation results.

Supported here: HTTP JSON and Streamable HTTP MCP with JSON or finite response
streams. This migration does not add an EFE RCB1 binary decoder, a Harbor agent
plugin, arbitrary binary transport support, cross-task publication, or automatic
physical-session restoration. Reusing the module for EFE tasks requires a
separate integration that preserves the task protocol and evaluator boundary.

The harness is a process orchestrator, not a substitute for the task container.
Generated programs use the OS sandbox; the host needs access to the configured
model and public-service endpoints. Linux may configure `EFE_ADAPTIVE_BWRAP`
and `EFE_ADAPTIVE_BWRAP_LD_LIBRARY_PATH` for a portable bubblewrap installation.
Trace storage checks free bytes and inodes before starting; its optional
`EFE_ADAPTIVE_GPFS_*` settings are deployment-owned and have no site defaults.

## Migration and license

The Python package is `python/efe_adaptive/`; TypeScript SDK orchestration is in
`src/`, and deterministic fixtures/regressions are in `tests/` and `test/`.
HumanCLAW-specific environment bindings, action-repair campaigns, Slurm jobs,
pilot task lists, historical reports, model endpoints, results and repository
history are outside this migration.

The imported code retains Apache-2.0 under this directory's [LICENSE](LICENSE)
and [NOTICE](NOTICE). The existing repository keeps its root MIT license.
