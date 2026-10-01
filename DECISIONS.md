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

**Known limit (closed in D-14).** After the lease expires the workload recovers and the incident the
hang produced stays listed; approving it is now refused and closes it as CLEARED (D-14). The default
duration is long enough (120 s) to review and approve.

## D-13: Plain language by default, technical detail one key away (Phase 5)

*Brief:* the TUI should be understandable to a human operator first and an engineer second; move the
technical information behind `D`; consolidate to four areas; never make a stronger claim than the
telemetry supports.

**Layering, not removal.** The current technical views are kept exactly as they are and become the
"details" view (`D`, a global toggle on Overview, Incidents, the incident page and Activity). The new
default views are written from the same model data through one module of phrase mappings
(`tui/src/plain.rs`) so wording lives in one place and is unit-tested. An unknown category, state or
event falls back to its raw name, never to an invented sentence.

**Where I depart from the brief's wording, and why** (the brief's own rule: no claim stronger than the
evidence):
* `GPU_MEMORY_PRESSURE` reads "GPU memory is above its limit", not "The GPU is running out of memory":
  the detector fires on used memory above a configured threshold, which is not the same claim.
* "Answered N test requests in a row" takes N from the server. The incident detail now carries
  `verification.required_completions` (the engine's configured `stable_probes`, absent when the engine
  has none); without it the page says "the model answers again" and never a number the TUI made up.
* The Activity view shows no times: audit events carry none (the hash chain stores event, data, prev,
  hash). The incident page's timeline, which does have times, keeps them.
* `INFERENCE_UNRESPONSIVE` is explained as "The model stopped responding to test requests", which is
  what the detector observed (a failed inference probe), and the evidence rows say "did not complete in
  time" only when the probe error is literally a timeout.

**Trust line, not trust repetition.** The header carries one concise indicator (Safety guard on /
Activity log intact, or the specific problem). The plain Overview does not repeat it; the technical
safety detail (watchdog identity and budgets, audit event count) is under Details and System.

**Four areas.** Overview, Incidents, Activity (the audit), System (control plane, diagnostics,
read-only configuration, about, and the confirmed stop). `?` is Help. The old Settings, Diagnostics,
About and Control Plane screens become sections of System; no information or action is dropped. `D` on
System keeps meaning "run diagnostics again" (details are always shown there).

**Accessibility (measured, WCAG contrast).** On a white background the old fixed RGB palette gave:
secondary text 3.6:1, green 2.0:1, amber 1.9:1, accent blue 2.5:1 (4.5:1 is the bar for normal text);
the `DIM` modifier used for stale values gave about 2.1:1 on black and 1.8:1 on white. On a dark
background the same colours are fine (5.8 to 11:1) except the border grey (2.1:1, decoration only).
So the default is now the terminal's own named colours (`--theme terminal`), which follow the user's
light or dark theme; secondary text is the terminal's normal text colour, never a fixed grey; rules
and borders are the only dark grey (decoration, never information). `--theme dark` keeps the old fixed
palette. `NO_COLOR` (non-empty, per no-color.org) or `--theme mono` removes every colour: state is
already a word plus a glyph, the selected row has a `›` marker, and the banners and the mode badge are
reverse video, which survives without colour. The theme is applied to the finished frame
(`theme::apply`), so no widget picks a colour for a theme; `App::new` stays on the semantic palette so
the existing render tests keep inspecting the constants. Stale values are not dimmed any more: they
stay at full contrast under an explicit line, "Stale · last values observed Ns ago". An error notice
stays until the next key press (a success notice still fades after 10 s). A skipped pipeline stage had
used the border grey for real text; it now uses the secondary text colour.

**Mode badge, and a deviation.** The header always carries `LIVE · GPU-REAL`, `LIVE` or `SIMULATION`
as a reverse-video chip. The brief also names `LOCAL-REAL`; telling a local container from anything
else would require the TUI to compare the provider's name against "docker", which the static safety
scan (`tui/tests/safety.rs`) forbids anywhere in the TUI source, and that scan is not weakened.
`GPU-REAL` rests on the data instead: a GPU reading (`gpu_uuid`) from the real telemetry is present.

**Screens.** `--screen` accepts `system` and `activity`; the old `control`, `settings`, `diagnostics`
and `about` are aliases for `system`. Keys `5`..`7` no longer exist (four areas; Help is `?`).

## D-14: An approval is checked against the current picture; a restart never starts a workload (Phase 7)

*Found by the final self-review (questions 5 and 8), not by a failing test.* Two gaps:
(1) `DockerProvider.restart_workload` re-checked labels and identity but not that the container was
running, and `docker restart` STARTS a stopped container: approving after the operator had stopped
the workload would have started it, an automatic workload start. (2) A proposal was not tied to the
picture it was made from: after a fault ended, the incident stayed pending and approving it restarted
a healthy model (the ADR-019 gap), a restart that interrupts inference and has no rollback.

*Decision.* (1) `restart_workload` refuses unless the container is running (a paused container is
still running, so the fault scenario is unaffected). (2) `Engine.approve` consults the latest
observation before consuming the proposal and refuses, when the condition behind it is gone: the
model answered since (INFERENCE_UNRESPONSIVE), memory is back under its limit (GPU_MEMORY_PRESSURE),
the workload is stopped or absent, or there is no observation at all (fail closed). Nothing is
restarted; the approval is NOT recorded as granted; an `approval_refused` event with the reason is
audited; the incident closes as CLEARED (new `POLICY_CHECK -> CLEARED` transition: nothing was
remediated or verified, so it is not RESOLVED); the API answers 409 POLICY_DENIED "The problem is no
longer present; nothing was restarted."; if the refusal cannot be saved it is rolled back like any
other write. Judged from the last observation (a tick old at most), not a fresh probe, so the
simulation's deterministic transcript is unchanged.

*Not done, on purpose.* A pending incident is not cleared automatically by a tick when its condition
goes away: the operator may be reading it, and a flapping model would reopen and clear it repeatedly.
The cost is a stale incident staying listed until acted on; it can no longer cause a restart.
