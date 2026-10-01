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
