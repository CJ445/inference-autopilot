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

## D-15: TUI overhaul (Phase 8): Home, Incidents, Lab, Activity, System; one story, one pipeline

*Brief:* make the TUI read as a terminal-native operator tool rather than an admin console; make
testing obvious; keep every safety rule and the backend as it is. The backend, the API and the state
machine do not change in this phase.

**Five areas, no sidebar.** Home, Incidents, Lab, Activity, System (`1`..`5`; Help on `?`). A one-line
navigation strip in the header replaces the 18-column sidebar: more room for content, less chrome.
The mode badge (`LIVE · GPU-REAL` / `LIVE` / `SIMULATION`) and the connection state stay in the header.

**The Lab.** "Practice" becomes "Recovery test" in every operator-facing word; `SIMULATION` is still
in the header badge and in an always-on banner (`RECOVERY TEST · SIMULATION`), so renaming never hides
what is simulated. The Lab lists exactly the two scenarios the backend supports: the simulated
unresponsive model (safe), and, only when the server offers it, pausing the real workload (affects
real infrastructure, behind the existing confirmation). It does not list scenarios that do not exist
(the brief's illustrative "Recovery verification [RUN]" is not a separate backend scenario).

**One key runs the whole test.** `R` asks the server for a fresh test session and, once the server
reports the stage `healthy`, the TUI asks it to inject the simulated fault. This is two existing calls
in sequence from the HTTP client; the server still enforces "only into a healthy model". Nothing is
scripted: every line of the story is derived from what the server reports (the stage, the incident,
its timeline, its recorded checks). `F` while testing stays as the manual retry if the automatic
injection was refused.

**Honest story, honest times.** Times come only from the incident timeline (server timestamps); the
audit carries none and the TUI invents none. The brief's "00:02" style offsets are not shown; clock
times are.

**One pipeline.** `Observe → Detect → Diagnose → Propose → Approve → Recover → Verify`, each stage
completed (`✓`), active/waiting (`→`), failed (`✗`), not run (`○`) or not applicable (`–`), derived
from the incident status by one function and drawn identically on Home, the incident page and the Lab.
Every state is a glyph and a word, never colour alone.

**Metrics.** Home shows only what the backend reports: probe latency, the mean end-to-end latency
(not a p95: vLLM exposes sum and count, not a percentile), requests per minute (only once two real
observations exist), GPU memory, temperature and utilisation; anything else is `N/A`.

**Keys that mean two things.** `A` and `R` decide an incident only on its page; elsewhere `R` runs a
recovery test and `A` opens Activity (on the Incidents list they still only explain). `F` opens the
real-fault confirmation (or breaks the model while testing); `I` opens Incidents; `P` is gone.
The footer is contextual, so the keys on offer are always the keys that work.

**Approval stays two-step.** The brief sketches `[A] APPROVE [R] REJECT [Esc] CANCEL` inside the
dialog. Putting both decisions in one dialog would make a single stray key a decision, so the existing
design stays: `A` or `R` on the incident page opens the request, `Enter` (ignored for the first
500 ms) confirms exactly the action that was opened, `Esc` cancels. The request is now explained
properly (what happened, the evidence, the action, why it needs your approval).

**"Recovery no longer needed".** When the server refuses an approval because the problem is gone
(D-14) the TUI shows a dialog that says nothing was restarted. It recognises that refusal by the
server's message text; a Python test pins the text so the two cannot drift apart silently. (A
dedicated error code would be cleaner but is an API change, and this phase does not change the API.)

**Update (implementation notes).**
* `F` opens the real-fault dialog on Home and the Lab only. This changes a Phase 4 choice (D-12:
  "only through the command palette"): the brief puts `[F] Inject` on screen. The dialog still sends
  nothing until `Enter` after the 500 ms guard, `Esc` cancels, and `F` anywhere else does nothing
  (tested), so the added exposure is one extra key that opens a dialog on two screens.
* Dialogs shed optional sections on a short terminal and always keep the decision line (tested with
  far more evidence than 18 rows can hold); the full explanation shows when there is room.
* Warnings (`STALE`, `telemetry stale`, `SLOW API`) are placed first in the header so a crowded row
  drops the status chips before it drops a warning (a test caught the opposite).
* Probe latency is shown only for a probe that answered; a failed probe's duration is a timeout.
* Ages are read as `31s`, `4m`, `7h`, not `26086s`.
* The proposal disappears from the API once an incident is approved, so the TUI remembers the
  action the server proposed earlier (client memory of server-reported data, cleared when the world
  changes) to keep the finished story complete.
* `tests/gpu/test_real_tui.py` asserted that no `aiops-tui` process existed anywhere on the machine
  and so failed whenever the operator had their own TUI open (the earlier unexplained flake); it now
  checks only the process it started. `scripts/clean_machine_check.sh` likewise only looks for
  processes of its own throwaway directory.

## D-16: The TUI as a terminal application: a stream, an anchored bar, a context panel (Phase 9)

*Brief:* not "the old TUI with new colours": an application one operates from inside the terminal.
*Study:* OpenCode's actual TUI (`packages/tui/src`), read for its interaction model, not its palette.

**What OpenCode's code shows (and the pattern I take from each).**

| Source | What it does | What I take |
|---|---|---|
| `routes/session/index.tsx` | The workspace is a chronological *transcript*: a `scrollbox` with `stickyScroll` to the bottom, blocks appended as things happen, 2-column padding, a blank row between blocks, a left bar per block | The recovery loop is a stream of stage blocks that appear as the server reports them; the view sticks to the newest |
| `component/prompt`, `routes/session/permission.tsx` | One *anchored action surface* at the bottom. It morphs: input, permission request (panel, left bar in the variant colour, an action row on a raised tone), reject-with-reason. The question is asked where the cursor already is | An anchored **bar** that always says what to do next in a sentence and morphs with the situation |
| `routes/session/footer.tsx` | One muted line: context on the left, small coloured dots with counts on the right (`• 3 LSP`, `△ 1 Permission`) | A quiet footer: the keys that work now on the left, status dots on the right |
| `routes/session/sidebar.tsx` | A 42-column panel on `backgroundPanel`, toggleable, an overlay when narrow; it holds *context about the subject*, never the main content | A **workload panel** (the model, the GPU, the probe) at the right, secondary, toggled with a key |
| `routes/home.tsx` | Home is calm: a centred composition around the single action surface. No widgets | Home is a narrative, not a grid of metrics |
| `component/command-palette.tsx`, `ui/dialog-select.tsx` | Every action is a registered command with a title, a category and a shortcut; the palette shows a *Suggested* group first (contextual), categories, the shortcut right-aligned, a filter; the selected row is a filled bar | A palette built the same way, with contextual suggestions |
| `ui/dialog.tsx` | A dimmed backdrop (black at alpha 150), a panel at one quarter of the height, no border, fixed widths 60/88/116 | Overlays are tonal panels over a dimmed screen |
| `ui/toast.tsx` | A panel top-right with a coloured left and right bar per variant; auto-dismisses | Notices are toasts, not a line in the footer |
| `component/spinner.tsx` | A braille spinner with muted text for work in progress; animations can be turned off and it falls back to `⋯` | The active stage shows a spinner; reduced motion falls back to `⋯` |
| `ui/border.ts` | The only border in the app is the left `┃` | One emphasis device |
| `context/theme.tsx`, `theme/index.ts` | A tonal ladder (background, panel, element); a "system" theme that derives its greys from the terminal | Tonal surfaces; the existing ANSI theme stays as the terminal-native fallback |

**The ten questions, answered.**

1. *Primary workspace?* The **stream**: the recovery loop as it happens, newest at the bottom, in the main region of every space.
2. *Primary loop?* Observe, detect, diagnose, propose, approve, recover, verify. It is the visual spine: one block per stage, appended only when the server reports it.
3. *Always visible?* The header (wordmark, the space you are in, the mode badge `SIMULATION` or `LIVE · GPU-REAL`, the connection), the anchored bar, the footer with its status dots.
4. *Appears contextually?* The anchored bar's content, the footer's keys, toasts, the evidence under a block, the workload panel on a wide terminal.
5. *In an overlay?* The command palette, the decision, the real-fault confirmation, the refusal, Help, the stop confirmation.
6. *Disappears when you move away?* The anchored bar's message and the footer's keys belong to the space; toasts fade (an error waits for a key); an expanded block collapses.
7. *Persistent spatial position?* Wordmark top-left, mode badge top-right, bar at the bottom, status dots bottom-right, the workload panel at the right.
8. *Progressively disclosed?* Finished stages collapse to one line; the active one shows its detail; `E` expands the evidence; `D` switches to the technical view (the existing global toggle).
9. *How a recovery test feels like an experience?* The Lab runs it as a live session in the same stream: the spine builds as the server reports each stage, the active stage has a spinner, the bar tells you what it needs, and it ends with a resolved block and "run again".
10. *How the user knows what to do next?* The anchored bar says it in a sentence with the key, in every situation (idle: "Run a recovery test to watch Autopilot work", waiting: "Autopilot needs your decision", and so on). It is the equivalent of OpenCode's prompt placeholder.

**One component, three places.** The incident stream (the loop for one incident, real or a test) is drawn by one function and used by Home (when something is open), the incident page and the Lab. This replaces three separate renderings (the Home rows, the detail page, the Lab story). The five spaces keep the same shell, so moving between them changes the main region and nothing else.

**Identity, not a clone.** OpenCode's neutral greys and peach accent are not used. The tonal ladder is cool and slightly blue (infrastructure, calm), the accent is a teal "signal" colour (inference, autonomy), amber is reserved for "needs you", and the signature is the **spine**: a vertical loop line of stages that fills in as the system works. No OpenCode name, wordmark or product concept is used.

**Honest limits.** Ratatui has no alpha blending: the backdrop is approximated by blending each cell toward black (truecolor) or by the DIM attribute (ANSI). It cannot query the terminal's background colour, so the tonal theme paints its own; the ANSI theme stays terminal-native and loses the tonal surfaces, keeping the left bar, the spacing and the glyphs. The spinner is driven by the event loop's 100 ms tick. Verification progress ("completion 1 / 3") is not reported by the server while it runs, so it is not shown: only the recorded checks, when they exist.

**Contrast (computed, not eyeballed).** Every text token is tested against every surface it sits on (≥ 4.5:1; small muted text ≥ 4.5:1 on the raised tone; the bar and glyph colours ≥ 3:1). OpenCode's own muted grey fails this on its raised surfaces (4.2:1 and 3.7:1), so it is not copied.

**Update (implementation notes).**
* Deviations from the first plan, from rendering the real frames: the loop at rest is the spine itself
  (each stage saying what it will do) rather than a row of glyphs, which taught nothing; the anchored bar
  is one tone lighter than the panels so it does not merge with the workload panel; toasts sit just
  above the bar and not at the top right, where they covered what they were about; the workload panel
  is shown only on Overview, the Lab and an incident's page (the list spaces get the width).
* Quit is no longer in the footer (it lists only what works here, and is the least needed); it is in the
  palette, in Help, and on `Ctrl+C`.
* Decisions work wherever the incident's stream is on screen (Overview, the incident page, the Lab's
  running test or fault) and only once its details have loaded: they are still two steps. The Lab's
  *menu* is not about any incident, so `R` there runs a test even when a real incident is waiting;
  conversely `R` on the Overview while a real incident is waiting means reject, and the footer says
  so. A footer that offered "Run test" there would have lied.
* The footer's key table is now one list with priorities: the way into the palette and Help, and the
  status dots, outlive the nice-to-have keys (a test caught Help being dropped at 72 columns, and a
  mis-sized column count that hid the dots at 76).
* Contrast is a test, not a promise: every text token on every surface in Dark and Light (a green was
  4.2:1 on the raised surface and was darkened); the light theme cannot leak a dark token; the tonal
  theme paints every cell so it never depends on the terminal's background.
* The backdrop is dimmed after the theme is applied (it blends RGB in the tonal themes and marks DIM
  in the terminal's own colours); a test covers both and the kept rectangle.
* Not done: the bar shows no per-completion verification progress ("completion 1 of 3") because the
  server does not report it while it verifies; the recorded checks appear when they exist. No animation
  other than the spinner.
