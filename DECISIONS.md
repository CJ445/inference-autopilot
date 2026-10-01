# Decisions

Where the implementation brief and the code disagree, or where the brief left a choice open, the
decision and its reasoning are recorded here before the code changes direction. The long-form
architecture decisions remain in `PRD.md` (ADR-NNN).

## D-1: Practice runs in-process, in an isolated object graph (not a second process)

*Brief:* simulation must be "structurally incapable" of reaching Docker/NVIDIA/processes.
*Constraint:* the TUI is HTTP-only and may not spawn anything, so Practice (entered from inside the
TUI) must be hosted by the control plane that is already running.
*Decision:* the control plane hosts a `SimSession` whose object graph (`SimWorld`, `SimProvider`,
`SimTelemetry`, `Engine(mode="SIMULATION")`, its own `Store`) shares no object with the real
engine. The `aiops/sim/` package is forbidden (by an AST test) from importing anything that can
reach Docker, NVIDIA, processes or the network.
*Why not a separate process:* it would need the control plane (or the TUI) to spawn it, adding a
process-spawning path to a component that otherwise has none.
*Honest limit:* an in-process boundary is weaker than a process boundary. `engine.py` imports
`kubectl.py`, which imports `subprocess`, so no import-graph argument can be absolute. The proof
is therefore also behavioural: a scenario runs with `subprocess`, sockets, `os.system` and
`shutil.which` patched to raise, and a fresh interpreter importing `aiops.sim` never loads the
Docker/GPU/vLLM/lifecycle modules.

## D-2: Mode is a structural property of audit, incident, evidence and store

*Brief:* simulation incidents, audit and remediation must be marked SIMULATION and a simulated
resolution must never be interpretable as real.
*Finding:* `engine._evidence` hard-codes `"source_type": "real"`; `AuditLog`, `Incident` and
`Store` carry no mode; `RealTelemetry.sources` names `nvidia-smi` / `vllm-probe`.
*Decision:* small, backward-compatible production changes: `AuditLog(mode)`, `Incident.mode`,
`Engine(mode)` (evidence `source_type` follows it), and `Store(path, mode)` with a marker that a
real `Store` refuses and a simulation `Store` requires. Real databases are byte-for-byte
unchanged (no new table is created for real stores). A boolean `if simulation:` is not relied on.

## D-3: Phase 1 simulates exactly one fault

*Decision:* only `model-unresponsive-recovery` (an inference probe that fails, then recovers after
a simulated restart). GPU memory pressure is not simulated yet: the brief says the unresponsive
path is the canonical scenario, and a second scenario would add surface without adding proof.

## D-4: `aiops demo` is the non-TUI entry; bare `aiops` stays the product entry

*Brief:* "Do not add a second public entrypoint if the architecture already has a better
mechanism."
*Decision:* `aiops demo` is an argparse subcommand like `start/stop/status/doctor` (the existing
convention), needs no config and touches no infrastructure, so it is the headless/CI/automation
form of the same scenario. The product entry for people remains `aiops` then `[P]` (Phase 2).

## D-5: The canonical real scenario is `docker pause`; GPU OOM is not

ADR-018: vLLM preallocates VRAM, so a bounded allocator-cap stressor never made it fail (zero
failures in ~163 probes), and raising the budget breaches the 85% watchdog limit. `docker pause` is
bounded, reversible and reliably produces the unresponsive condition the detector, RCA and
verifier were built for (ADR-019).

## D-6: The simulated restart changes lifecycle identity, not a flag

The verifier's first check is "the workload's lifecycle identity changed". The simulated provider
therefore changes `sim:<name>:generation-N` on restart, and verification observes the new
generation and then three consecutive successful simulated completions, through the *production*
`real_verifier`. A negative test makes the restart not recover the model and requires
`UNRESOLVED`.

## D-7: Keys

`D` currently re-runs diagnostics on the Diagnostics screen only. Phase 5 moves Diagnostics under
System and makes `D` mean "details" on Overview and incident pages; the two are never active on
the same screen.

## D-8: The practice namespace mirrors the real API; the TUI reuses its screens

*Brief:* reuse the existing incident page and approval UX; "do not create a separate fake practice UI".
*Decision:* the control plane serves `/api/v1/practice/{status,incidents,incidents/{id},
incidents/{id}/remediation/approve|reject,audit}` with exactly the real shapes, plus
`/practice/{start,fault,stop}`. The TUI's client simply addresses a different prefix, so Overview,
Incidents, the incident page, the approval dialog and Audit are the same code. The router can only
rewrite a practice path to an engine-shaped route; `/health`, `/version`, `/config`, `/diagnostics`
and `/control/stop` are unreachable through the practice prefix (tested, including a confirmed
stop sent through it). Each namespace takes the lock of its own engine.

## D-9: The practice banner says "containers", not "Docker"

*Brief:* "Nothing here touches your GPU, Docker, or real workload."
*Conflict:* `tui/tests/safety.rs` forbids the word "Docker" anywhere in the TUI source: its purpose
is to prove the TUI has no reference to the container runtime. Weakening that scan to allow one
string would weaken a safety test.
*Decision:* the banner reads "Nothing here touches your GPU, containers, or real workload."

## D-10: A poll for the other namespace is discarded

A real poll that is in flight when practice starts (or a practice poll when it ends) would paint the
wrong world under, or without, the banner. Every snapshot carries the namespace it was fetched for
and the app drops a mismatch; entering and leaving practice also clears the view.

## D-11: One acceptance function for both golden scenarios; the PRD story is amended, not silently ignored

*Brief:* the real Docker-pause scenario is the canonical real scenario and must use the same
conceptual semantics as the simulation; GPU OOM is not forced.
*Decision:* `tests/golden_acceptance.py` is the single judge (PRD §72 plus audit order, recorded
checks and mode marking), used by the simulated and the real test. PRD §71 still literally describes
GPU memory exhaustion, so ADR-030 and an amendment note under §71/§72 say plainly that the
acceptance scenario is the inference hang and why (ADR-018), rather than leaving the spec and the
tests in disagreement.
*Harness change (not product code):* `tests/gpu/conftest.py` starts vLLM with `HF_HUB_OFFLINE=1`
when the model is already in the cache volume. A slow `huggingface.co` made vLLM retry for ~10
minutes and the real tests time out, unrelated to anything under test.

## D-12: The guarded real fault injector (Phase 4): design before code

*Brief:* a separate `FaultInjector` (not in `DockerProvider`); one real fault (pause); strict
pre-checks; an explicit confirmation; a persisted lease; an independent recovery that works even if
the injector crashes; never arbitrary commands; never automatic.

**Separation.** `aiops/faults/` is its own package with its own container runner that allows
exactly four argv shapes (`ps` with the two project labels, `inspect <64-hex>`, `pause <64-hex>`,
`unpause <64-hex>`) and nothing else. Container resolution and the identity checks reuse the
`DockerProvider` code through a module-level helper (`docker.inspect_workload`), so there is one
definition of "this is the managed workload" (labels, exactly one match, `ps` id equals `inspect`
id) and `DockerProvider`'s public surface is unchanged (a test pins it).

**Identity.** `expected_identity = "<full container id>:<StartedAt>"`, the same lifecycle identity
the verifier uses. It is resolved twice: once for the preconditions and again immediately before the
pause (a mismatch aborts). A restart changes `StartedAt`, so recovery refuses to touch a workload
that was restarted or replaced while the fault was active.

**Crash-safe order.** (1) preconditions; (2) persist the lease (`ARMING`, with `expires_at`) BEFORE
doing anything; (3) arm an independent reaper process and verify it is alive; (4) re-check the
identity; (5) pause; (6) mark the lease `ACTIVE`. If step 3 fails nothing is paused. A crash at any
point leaves either nothing paused or a lease plus a reaper that will undo it.

**Four layers of recovery** (each independently sufficient):
1. the **reaper**, a separate detached process (`python -m aiops.faults.reaper`) that waits for the
   deadline and then recovers; it survives a control-plane crash;
2. the **control-plane monitor** thread: recovers at the deadline and re-arms a dead reaper;
3. the **startup sweep**: a control plane that starts with an outstanding or expired lease recovers it;
4. `aiops doctor` reports an outstanding lease (`fault_lease`, WARN).
Recovery is idempotent and runs under an `flock` on the lease file: it re-inspects, unpauses only
if the identity still matches AND the container is paused, records what it did, and never touches a
different container. A graceful control-plane stop recovers an active lease too.

**Audit.** The hash-chained audit is owned by the control plane, so the injector's `fault_injected`
and `fault_recovered` events are written by the monitor thread from the lease record, idempotently
(`audited_*` flags in the lease). Nothing blocks an API request on the engine lock (a remediation can
hold it for minutes), and a recovery done by the reaper while the control plane is down is audited at
the next start.

**Trigger.** `POST /api/v1/faults` (the PRD §69 shape) with `type: "pause_workload"`, `target`
(must equal the configured workload) and `duration_seconds` (bounded 15..300), plus the explicit
header `X-Aiops-Confirm: inject-fault` (a web page cannot send it). Unknown types, other targets,
unsafe durations, a missing confirmation, a second concurrent fault, and a failed precondition are all
refused. It exists only on a control plane with the Docker provider. It is never automatic.

**TUI.** Only through the command palette ("Break the real workload (pause)…") into a confirmation
dialog that says what will happen, labelled `LIVE · GPU-REAL`; Enter is ignored for 500 ms like every
other confirmation. While a fault is active a banner is on every screen with the time remaining, and
the palette offers "Resume the workload now". It is hidden while practicing and when the server does
not offer faults.

**Known limit.** After the lease expires the workload recovers and the incident the hang produced
stays pending (the ADR-019 gap); approving it would restart a healthy workload, which the operator
can see. The default duration is long enough (120 s) to review and approve.
