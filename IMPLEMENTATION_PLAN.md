# Implementation plan: simulation, golden scenarios, operator UX

Status: Phase 0 (audit) and Phase 1 (simulation engine and golden scenario) complete; Phase 2 next. Decisions and disagreements with the brief
are in `DECISIONS.md`. The authority for behaviour is `PRD.md` (ADR-021 … ADR-027 are the most
relevant).

The goal of this phase is not more features. It is to make the existing closed loop obviously
real, safe, testable, understandable and demonstrable:

    correctness > safety > deterministic demonstration > operator clarity > testability > polish

## 1. Current architecture (verified by reading the code, not from memory)

    aiops (Python launcher) -> attach/start control plane -> exec Rust TUI (HTTP only)

Control plane (`aiops/`, stdlib only):

| Concern | Module | Notes relevant to this phase |
|---|---|---|
| Observation | `telemetry.RealTelemetry(gpu, vllm)` | Generic: takes a GPU callable and a vLLM-like object. `sources` names `nvidia-smi`, `vllm-probe`, `vllm-metrics`. |
| Detection | `detector.py`, `engine.Engine._detect` | Pure detector functions; the 2-consecutive-probe debounce lives in the engine (ADR-027). |
| Incident | `incident.Incident` | State machine, timeline. No notion of mode. |
| Evidence / RCA | `engine._evidence`, `rca.diagnose` | **`"source_type": "real"` is a literal** in `_evidence`. |
| Proposal / policy | `remediate.propose`, `policy` | One action, `restart_workload`; approval always required. |
| Execution | `remediate.execute` | Takes any object with `get_workload` / `restart_workload`. |
| Verification | `verify.real_verifier` | Takes any provider + any telemetry with `.gpu()` and `.vllm.probe()/.metrics()`. Independent of Docker. |
| Persistence / audit | `store.Store`, `audit.AuditLog` | SQLite + hash chain. No notion of mode. |
| Workload (real) | `docker.DockerProvider`, `workload_presence` | Identity-bound, label-checked. |
| Lifecycle / watchdog | `lifecycle`, `watchdog`, `launcher` | Unchanged by this phase. |
| API | `api.make_server` | `view()`/`detail()` build incident JSON; `/status` reports health, workload state. |
| TUI | `tui/` (Rust) | HTTP client only; static scan forbids process/Docker/GPU/DB access. |

Existing simulation-like pieces (all test-only today):

* `tests/test_engine.py::World`, `tests/test_engine_real.py::Rig`: stateful stand-ins, good for
  unit tests, but they are not a product feature, are not labelled, and share the production
  store/audit with no marking.
* `deploy/kubernetes/`: a CPU stand-in StatefulSet (a real Kubernetes path, not simulation).
* `gpu_fault.py` + `supervise.py`: a bounded real GPU stressor. It does **not** make vLLM fail
  (vLLM preallocates VRAM, ADR-018) and is not exposed anywhere.

The only reliable real fault is `docker pause` (ADR-018/019); the real end-to-end test already
exists (`tests/gpu/test_real_vllm_engine.py`, `test_real_tui.py`).

## 2. Coupling that would make a naive simulation unsafe or misleading

1. `engine._evidence` hard-codes `source_type: "real"`, and `RealTelemetry.sources` names real
   tools. A simulation reusing them unchanged would emit evidence that claims to be real.
2. `AuditLog`, `Incident` and `Store` carry no mode, so a simulated chain/incident is
   indistinguishable from a real one, and a real engine could open a simulation database.
3. `engine.py` imports `kubectl.py`, which imports `subprocess`. "Cannot reach subprocess" can
   therefore NOT be a pure import-graph property; it needs runtime proof as well.
4. The engine selects the real verifier via `hasattr(telemetry, "vllm")`. That is fine (the
   verifier is Docker-independent) and is the right reuse point.

## 3. Phases

### Phase 1: simulation engine and golden scenario (this commit series)

Package `aiops/sim/` (imports nothing that can reach Docker, NVIDIA, processes or the network):

* `world.SimWorld`: deterministic synthetic state (lifecycle generation, inference/metrics health,
  GPU readings). Nothing in it reaches outside the object.
* `world.SimProvider`: `get_workload` / `restart_workload` over a `SimWorld`, identity-bound to
  one workload name exactly like `DockerProvider`; a restart changes the lifecycle generation
  (`sim:<name>:generation-N`), never a flag the verifier trusts.
* `world.SimTelemetry(RealTelemetry)`: same observation shape and the same production
  `metrics()`, but `sources` name `simulated-*`.
* `session.SimSession`: the production `Engine` + production `real_verifier` wired to a
  `SimWorld`, mode `SIMULATION`, its own `Store`. Stage is *derived* from real engine state,
  never scripted.
* `scenario`: the golden scenario `model-unresponsive-recovery`, runnable without the TUI.
* CLI `aiops demo [--scenario NAME] [--approve | --reject]` (no config, no infrastructure).

Small, backward-compatible production changes (so labels are structural, not cosmetic):

* `AuditLog(mode=None)`: when set, every appended event carries `"mode"`.
* `Incident.mode` (only when not REAL) and `source_type` follows the engine mode.
* `Store(path, mode=None)`: a SIMULATION store records a marker; a real `Store` refuses a
  database carrying it, and a SIMULATION store refuses an unmarked database (fail closed).
* `Engine(mode="REAL")`; `/api/v1/status` reports `mode`; incident views carry it.

Safety proof: AST scan of `aiops/sim/` (no `subprocess`, `socket`, `urllib`, `shutil`, `os.system`,
`aiops.docker/gpu/vllm/runtime/lifecycle/...`), a fresh-interpreter check that running a scenario
loads none of those modules, and an in-process run with `subprocess`, sockets, `os.system` and
`shutil.which` patched to raise.

### Phase 2: Practice mode in the TUI

The TUI can only talk to one control plane over HTTP. The control plane therefore hosts a
`SimSession` under `/api/v1/practice/...` (create, tick/advance, inject, same incident/approve
shapes). The TUI reuses the incident page and approval dialog against that namespace, with a
persistent `PRACTICE · SIMULATION` badge. Entry: `[P]` and the command palette. Practice state is
in memory plus its own SIMULATION store; never the real DB.

### Phase 3: Canonical real scenario

Name and document the existing Docker-pause scenario as THE real golden scenario (no GPU OOM). A
single named test and a README walkthrough; no new mechanism.

### Phase 4: Guarded real fault injector (only after 1 to 3 are stable)

Separate `aiops/faults/` (NOT in `DockerProvider`). One fault: pause. Identity/label/running
checks, explicit confirmation, a persisted lease (fault id, workload identity, expiry), an
independent reaper that unpauses only the identity-verified workload when the lease expires (also
swept at control-plane start), recovery audit, no arbitrary commands.

### Phase 5: TUI information architecture and language

Plain language first, technical detail behind `D`; four areas (Overview, Incidents, Activity,
System), empty states, mode badges, contrast / `NO_COLOR`. Every phrase checked against what the
evidence actually establishes; no stronger claims than the telemetry supports.

### Phase 6: CI and clean-machine validation

Portable suites (`unit`, `integration`, `simulation-golden`, `tui`) need no GPU, Docker or
Kubernetes; `real-local` / `real-gpu` stay opt-in. A documented clean-machine procedure.

### Phase 7: Final verification and documentation, including the 15-question review.

## 4. Test strategy

* Simulation tests assert **state transitions**, not strings: incident status sequence, audit
  event sequence, lifecycle generation change, verification checks all exactly True, probe log.
* Negative tests: no approval, rejection, failed verification, persistence failure, malformed
  observation, isolation, labels.
* The golden scenario is deterministic (same transcript and audit event sequence every run).
* Existing real-GPU tests must keep passing; they are run before each phase is declared done.

## 5. Explicit non-goals

Web UI, OpenTelemetry, LLM RCA, Kubernetes expansion, more detectors, automatic workload
start/restart/creation, arbitrary shell or Docker commands, GPU OOM stress, rollback, correlation,
multi-user auth, cloud deployment, extra TUI screens for their own sake.
