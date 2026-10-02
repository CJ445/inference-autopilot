# Inference Autopilot

An AIOps control plane for LLM inference infrastructure (Python) with an operator terminal UI
(Rust/Ratatui). The product is built around one flow, enforced by the server:

    evidence → deterministic RCA → typed proposal → policy → operator approval
             → remediation → verification → audit

`PRD.md` is the specification.

## Architecture in one picture

    aiops  (Python launcher: the one command you type)
      │  1. is a control plane running?  (lifecycle state file + process identity)
      │  2. if not: run the existing `aiops start`, detached  (doctor, watchdog, claim, API)
      │  3. wait until the API really answers
      └─ 4. hand the terminal to the Rust TUI
                │ HTTP only, loopback
                ▼
         Python control plane ── detector · RCA · policy · remediation · verification · audit
                │
         watchdog (independent process)

The TUI is a pure HTTP client. It cannot start processes, touch the container runtime, the
cluster, the GPU, the inference server or the database. The control plane is **not** a child of
the TUI: quitting the TUI leaves it running.

## Setup

Prerequisites: Python ≥ 3.11 (standard library only, no runtime dependencies) and a Rust
toolchain (`cargo`) for the TUI.

### 1. Build the TUI

    cargo build --release --manifest-path tui/Cargo.toml

The binary is `tui/target/release/aiops-tui`. `aiops` finds it there (or, failing that, in
`tui/target/debug/`). Set `AIOPS_TUI_BIN=/path/to/aiops-tui` to use another one.

### 2. Install the `aiops` command

`aiops` is the Python package's command. Install it once as a user-level tool, so it is on your
`PATH` in every shell and directory (the same way `claude` and `opencode` are), with an editable
install so it keeps pointing at this checkout and the TUI built above:

    uv tool install --editable .        # puts a symlink in ~/.local/bin

`~/.local/bin` must be on your `PATH` (`uv tool update-shell` adds it). Upgrade or remove with
`uv tool upgrade inference-autopilot` / `uv tool uninstall inference-autopilot`.

Without `uv`, use a virtual environment (the command then exists only while it is activated, or
by its full path `.venv/bin/aiops`):

    python3 -m venv .venv && . .venv/bin/activate && pip install -e .

The Rust binary is **not** the `aiops` command and `cargo install` is not the install path.

### 3. Verify

    which aiops
    aiops --help

### 4. Configure

`aiops` reads a runtime profile: `--config PATH`, else `./aiops.toml`, else
`~/.config/aiops/aiops.toml`. **On first run, if none exists, `aiops` offers to create
`~/.config/aiops/aiops.toml` from the default profile** (docker-real-gpu: an NVIDIA GPU, Docker
and one vLLM container you provision yourself). It asks first, never overwrites an existing file,
and never offers when you passed `--config`. The Kubernetes profile has no safe default (it needs a
kubectl context), so copy it from `deploy/profiles/` yourself.

Relative paths in a profile (`db`, `state_file`) resolve next to the profile file, so the state
and the startup log live in `~/.config/aiops/`. A profile names exactly one managed workload;
unknown keys are errors.

### 5. The vLLM container (optional, operator-controlled)

`aiops` never needs a workload to start. The control plane and TUI run with **zero** workloads;
the TUI shows `No workload running` and no workload telemetry. The managed container is entirely
yours: `aiops` never creates, pulls, starts, stops or restarts it (ADR-021). The control plane
notices it by its labels, so when you start a correctly labelled container its telemetry appears,
and when you stop or remove it the TUI returns to the unavailable state. (Remediation only ever
acts on that one container, through an approved incident.) Create one, for example:

    docker run -d --name aiops-vllm --gpus device=0 \
      --label com.inference-autopilot.managed=true \
      --label com.inference-autopilot.workload=vllm \
      --memory 8g --cpus 4 --shm-size 1g \
      -p 127.0.0.1:8001:8000 -v aiops-hf-cache:/hf -e HF_HOME=/hf \
      --health-cmd "python3 -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/health',timeout=3)\"" \
      --health-interval 5s --health-timeout 5s --health-retries 3 --health-start-period 240s \
      vllm/vllm-openai:v0.10.0 --model facebook/opt-125m \
      --gpu-memory-utilization 0.35 --max-model-len 512 --enforce-eager

The label value (`workload=vllm`) must equal `[workload] name` in the profile; the container
name does not matter. Give it a minute or two to load the model (`docker ps` shows `healthy`;
while Docker reports it `starting`, a failing probe is shown but not treated as an incident).
Use no Docker restart policy: the project is never a permanent background service.

Missing, stopped and absent are not prerequisites, but a labelling problem still is:
two containers with the labels, or a container whose labels do not match, blocks start. A
workload that is running and does not answer is an incident, as before. A graceful
`docker stop` looks like a hang for a few seconds, so it can open an incident. If the workload is
stopped or the model answers again before you approve, approving restarts nothing (see
"Safety boundaries").

## Using it

    aiops

That is the normal way in. It looks for a running control plane for the profile:

* **already running** → attaches to it and opens the TUI. No second control plane is started.
* **not running** → runs the existing `aiops start` detached (its own session, output in
  `<state_file>.log`), shows the lifecycle's own startup lines, waits until the API answers,
  then opens the TUI.
* **cannot start** (a blocking prerequisite failed, such as an unreadable GPU or a Docker
  daemon that is down; the watchdog could not arm; the port is taken; the state file is unreadable, or it did not answer in time) → prints the reason and
  the last lines of its output and exits non-zero. The TUI is not opened over a broken start.

Quitting the TUI (`Q`, also in the palette and Help) leaves the control plane running. To stop it,
use the *System* space (`5`, then `X`, then Enter) or, from a shell, `aiops stop`.

**What the screen is.** The TUI is an application you operate, not a dashboard. Its centre is a
**stream**: the recovery loop for an incident as it happens, one block per stage
(`DETECTED`, `DIAGNOSED`, `RECOVERY READY`, `APPROVED`, `RECOVERING`, `VERIFYING`, `VERIFIED`), each with a
bar at its left edge. A block appears only when the server reports that stage; finished ones collapse
to a line, the one the loop is at is open, and the view sticks to the newest. Below it an **anchored
bar** always says, in a sentence, what the situation is and what to do next ("Autopilot needs your
decision. Restart the model server? A approves, R rejects, E shows the evidence."). Under that, the
footer lists only the keys that work right now, with small dots at the right for what protects you
(`• watchdog  • audit`). At the right of the stream on a wide terminal is the **workload panel**: the
model, probe latency, GPU and VRAM, and whether Autopilot is watching (`W` shows or hides it).
Notices are toasts above the bar; an error waits for a key.

The header names the space you are in and which world you are in: `LIVE · GPU-REAL` (a real GPU
reading is present), `LIVE`, or `SIMULATION`. Five spaces (`←`/`→`, `Tab`, or `1`–`5`):

| Space | What it is for |
|---|---|
| **1 Overview** | A sentence on what is going on and the loop at rest, explaining each stage; when something needs you, the stream takes over |
| **2 Incidents** | What happened, why, what Autopilot proposes, and what needs your OK |
| **3 Lab** | Test the autopilot: a list of tests you move through and run (a safe recovery test, and, when offered, pausing the real workload); a running test is a live stream |
| **4 Activity** | What the system did and decided, in order |
| **5 System** | The control plane and its stop, the same checks as `aiops doctor`, the validated active configuration, and About; all read-only except the confirmed stop |

**Keys.** `Ctrl+P` opens the command palette (a *Suggested* group for the moment you are in, then
Test, Go to, View and System; the shortcut at the right of each row), `?` Help, `D` the technical view
of the same screen (exact categories, raw states, evidence sources, GPU and vLLM metrics, audit
hashes; `aiops-tui --once --details` prints that frame for scripts), `E` the evidence under every
stage, `I` Incidents, `R` run a recovery test, `F` open the real-fault dialog (Overview and Lab only),
`A` Activity. Where an incident's stream is on screen and it is waiting for you, `A` and `R` are its
decision keys instead, and the footer says so.

**Deciding.** Open the incident (`Enter`) or stay on Overview, where it is already the page. The
decision keys work only once the incident's full details have loaded (until then the stream says
"Loading the full details…"). The request that opens explains itself: the problem, the server's
reason, the evidence, the proposed action, and that the action is allowlisted and runs only after you
approve; a restart interrupts inference and has no rollback. It is two steps on purpose: `A` or `R`
opens it, `Enter` confirms exactly that action (ignored for the first half second so a held key cannot
confirm it), `Esc` cancels. While the server works the bar says so. If the problem has already gone
by the time you confirm, the server refuses, nothing is restarted, and a dialog says "RECOVERY NO
LONGER NEEDED".

**Looks.** On a truecolor terminal the default is a cool tonal palette: four surfaces (page, panel,
element, raised), one teal accent for "the loop is here", amber only for "this needs you", green and
coral for state. Every text colour is tested at 4.5:1 or better on every surface it sits on (body
text 7:1). `--theme light` is its light twin, `--theme terminal` (the default when `COLORTERM` does not
say truecolor) uses your terminal's own colours with no surfaces, and `NO_COLOR` (or `--theme mono`)
removes colour entirely: structure survives as bars, glyphs and weight, and state is always a glyph
plus a word. `--no-animation` (or `AIOPS_NO_ANIMATION=1`) makes the spinner a still `⋯`.

Restarting the control plane is not done from the TUI; run `aiops` again after a stop.

The explicit commands remain for scripts and debugging:

    aiops start      # run the control plane in the foreground
    aiops stop       # graceful stop (also disarms the watchdog)
    aiops status     # observed state of the control plane and workload
    aiops doctor     # read-only prerequisite check
    aiops tui        # attach the TUI only; never starts a control plane

## Try the failure loop safely (SIMULATION)

In the TUI: open the **Lab** (`3`), pick *Model becomes unresponsive* and press `Enter` (or press
**`R`** on the Overview, or `Ctrl+P` then "Run a recovery test"). One key runs the whole test: a
fresh simulated session starts, and as soon as the server reports the simulated model healthy the TUI
asks it to break the model. A persistent amber banner, `RECOVERY TEST · SIMULATION`, appears on every
space, the header badge says `SIMULATION`, and the workload panel says its values are simulated.

The Lab then becomes the live stream of the test, built only from what the server reports, with the
server's own timestamps: `TEST STARTED`, `FAULT INJECTED`, `DETECTED` (the detector needs the problem
on consecutive checks), `DIAGNOSED`, `RECOVERY READY` (with the evidence and why it needs your OK),
`APPROVED`, `RECOVERED`, `VERIFIED` (with the recorded checks) and `✓ INCIDENT RESOLVED`. The decision
is made right there: `A` opens the request, `Enter` confirms. `R` runs it again; `Esc` leaves the
test and returns to the real system. The request says "A simulated restart: nothing real is
restarted." The real system, its incidents, its watchdog and its workload are never touched, and
nothing from the test is ever shown as real (or the reverse): the test has its own routes
(`/api/v1/practice/...`), its own engine, store and lock, and the TUI discards any poll that belongs
to the other one.

From the command line, without the TUI:

    aiops demo --approve        # or --reject; with neither, it asks when run in a terminal

This runs the **real** control loop (the same engine, detector, evidence, deterministic RCA,
proposal, policy, approval, verification and hash-chained audit) over a **synthetic, in-memory
workload**. It needs no config, no GPU, no Docker and no container, and it cannot touch any of
them: the simulation package may not import anything that reaches Docker, NVIDIA, a subprocess or
the network (a test enforces it, and another runs the whole scenario with those doors patched
shut). Every audit event, incident and piece of evidence is marked `SIMULATION`
(`source_type: simulation`, sources `simulated-*`), and a simulation database cannot be opened by
the real store or the reverse.

The scenario, `model-unresponsive-recovery`:

    the model answers -> fault injected -> 1st failed probe (no incident yet) ->
    2nd consecutive failed probe -> incident -> evidence -> root cause -> restart proposal ->
    policy: approval required -> your decision -> simulated restart (lifecycle generation 1 -> 2) ->
    verification from the observed state (identity changed, GPU and metrics readable, 3 successful
    completions in a row) -> RESOLVED -> audit chain intact

A restart that does not bring the model back is `UNRESOLVED`, never `RESOLVED`. Simulation is also
the portable test harness: no GPU or Docker is needed to run it in CI. It cannot prove anything
about your real hardware; the real golden scenario below does.

## The real golden scenario (Docker pause)

This is the project's official end-to-end demonstration on real hardware. It uses a real GPU, a real
vLLM and your real approval; nothing is simulated. It needs the setup above (`aiops doctor` clean
and the labelled vLLM container running and healthy).

1. In one terminal run `aiops` and stay on the Overview (everything healthy).
2. In another terminal pause the workload (a bounded, reversible fault):

        docker pause aiops-vllm          # use your container's name

3. Within about 10 seconds the real inference probe has failed twice in a row and an incident
   appears: **INFERENCE_UNRESPONSIVE**, with evidence from the real GPU and the failed probe, a
   deterministic root cause, a `restart_workload` proposal and "approval required". Nothing has been
   restarted: the container is still paused.
4. Open the incident (`Enter`), review it, approve (`A`, then `Enter`). The server restarts the
   workload; the TUI shows the server's state as it verifies.
5. About a minute later: `RESOLVED`. That means Docker reports a new lifecycle identity, the GPU and
   metrics are readable, and three consecutive real completions succeeded. The audit chain records
   the whole loop.

If you abort, `docker unpause aiops-vllm` undoes the fault (approving the restart also clears it).
The restart is the only thing that ever acts on your container, and only after your approval.

**Why a hang and not GPU memory exhaustion:** vLLM preallocates its VRAM, so a bounded allocator-cap
stressor never made it fail (zero failures in ~163 probes), and a larger one would breach the 85%
watchdog limit (ADR-018). A paused workload is bounded, reversible and reliably produces exactly the
condition the detector, RCA and verifier were built for (ADR-019, ADR-030).

**How it is tested.** `tests/gpu/test_real_golden.py` runs this against the real machine
(`AIOPS_GPU=1 python -m pytest tests/gpu/test_real_golden.py`; run it alone) and is judged by
`tests/golden_acceptance.py`, which encodes the PRD §72 acceptance criteria. The *same* judge holds
the simulated scenario to the same semantics (`tests/test_golden_parity.py`, no hardware), and its
own tests prove it can fail: an unchanged lifecycle identity, an unresolved incident, checks that
differ from the audit record, missing or misordered audit events, no root cause or too little
evidence, a simulation that looks real (or the reverse), and a remediation that never executed.
Recovery is observed independently of the control plane (`docker inspect` for the identity and the
paused flag, and direct inference requests). The test was run 4 times in a row on the RTX 4060
(94 s, 88 s, 81 s, 81 s). If the model is already in the `aiops-hf-cache` volume the harness starts
vLLM offline (`HF_HUB_OFFLINE=1`): without that, a slow or unreachable `huggingface.co` made vLLM
retry for about 10 minutes before falling back to the cache.

## Breaking the real workload on purpose (guarded fault)

For testing the real loop without typing `docker pause` yourself, the TUI can pause your real
workload for a bounded time. It is a testing tool, never part of normal operation, and it can only
do one thing.

In the TUI: the **Lab** (`3`) lists it under *Real infrastructure* ("affects the running workload"),
and `F` on the Overview or the Lab (or `Ctrl+P`, then **"Inject a real fault (pause the workload)…"**) opens
a dialog. `F` works only on those two screens, so a stray key elsewhere does nothing, and it only
opens the dialog: nothing is sent until you press `Enter` (ignored for the first half second).
The dialog says what will happen (`LIVE · GPU-REAL`: the managed workload stops answering for up to
2 minutes, then resumes by itself), the expected flow, and the safety mechanisms (checked
identity, a lease saved before anything is paused, an automatic timer, and a separate process that
resumes it even if this one dies). While the fault is active a red banner on every screen shows the
time left, and the Lab shows the fault and the loop it triggers; `Ctrl+P`, then **"Resume the
workload now"** ends it sooner. It is not offered during a recovery test, while a fault is already
active, or on a control plane without the Docker provider (the Lab says why).

What makes it safe (ADR-031, DECISIONS.md D-12):

* **It can run four commands and nothing else:** `ps` (with the two project labels), `inspect`,
  `pause` and `unpause`, the last two only for a full 64-hex container id. Any other verb or shape
  is refused before a process starts. It is a separate component from the remediation provider.
* **It refuses unless the workload is exactly right:** exactly one container with both project
  labels, running and not already paused, with its identity (`id:StartedAt`) checked twice, the
  second time immediately before the pause.
* **It cannot leave the workload paused forever.** The lease (fault id, workload, identity,
  `expires_at`) is persisted *before* anything is paused, and an independent reaper process is armed
  and verified alive first; if it cannot arm, nothing is paused. Four layers each end the fault:
  the reaper process (survives a control-plane crash), the control plane's own monitor, the next
  `aiops start`, and `aiops doctor` flags an outstanding lease (`fault_lease`). A graceful stop ends
  it too.
* **It never touches a different workload.** Recovery unpauses only if the identity still matches
  *and* the container is paused. If the workload was restarted (for example by an approved
  remediation) or replaced meanwhile, it records `IDENTITY_CHANGED` and does nothing.
* **It is explicit and recorded.** `POST /api/v1/faults` needs the `X-Aiops-Confirm: inject-fault`
  header (a web page cannot send it) and refuses unknown fault types, any target but the configured
  workload, durations outside 15 to 300 seconds, a second fault, and failed preconditions.
  `fault_injected` and `fault_ended` are written to the hash-chained audit exactly once each.

If you do nothing, the workload resumes when the lease ends and the hang clears; the incident it
produced stays listed, but approving it is refused ("The problem is no longer present; nothing was
restarted.") and it closes as *Cleared*. The default 2 minutes is enough to review and approve.

## Safety boundaries

What the system does and does not do, and where each claim is enforced and tested:

* **Nothing runs without your approval.** A restart is proposed by the policy and executed only after
  you approve it on the incident page. There is no automatic path (`tests/test_engine.py`,
  `tests/test_simulation.py`: no decision, no restart; rejection, no restart).
* **An approval is only as good as the picture it was made from.** If, when you approve, the model
  answers again, GPU memory is back under its limit, the workload is stopped or absent, or there is
  no current observation, nothing is restarted: the approval is *not* recorded as granted, an
  `approval_refused` event is audited with the reason, and the incident closes as *Cleared*
  (`tests/test_stale_approval.py`; on real hardware `test_real_fault_injector.py`).
* **The workload is never started for you.** The only container operations the control plane has are
  `ps`, `inspect` and `restart` of the one labelled, configured workload, and `restart` is refused
  unless the container is already running (a restart would start a stopped one). Starting it is your
  decision. The fault injector can additionally `pause` and `unpause`, nothing else
  (`tests/test_docker.py`, `tests/test_faults*.py`).
* **The operator UI cannot act on infrastructure.** It is an HTTP client: a static scan
  (`tui/tests/safety.rs`) fails if its source can start a process or mention the container runtime,
  the GPU, or the database.
* **There is no LLM in the control loop.** The cause is a deterministic function of the evidence
  (`aiops/rca.py`); nothing generates or executes commands.
* **Recovery is judged from observation, never from the restart's own success.** `RESOLVED` needs a
  non-empty set of checks that are *all* exactly true: the lifecycle identity changed, the GPU is
  readable, the model's metrics are readable, and a run of consecutive real completions succeeded.
  An empty, partial, crashing or timed-out verification is `UNRESOLVED` (`tests/test_remediate.py`,
  `tests/test_verify.py`).
* **A fault always ends.** The injector persists its lease before pausing, arms an independent
  reaper, and recovers by the deadline even if the control plane is killed (verified with `SIGKILL`
  on real hardware); it never touches a workload that was restarted or replaced.
* **Simulation cannot be mistaken for the real system, and cannot reach it.** Every simulated audit
  event, incident, evidence record and database is marked `SIMULATION`; a real store refuses a
  simulation database and the reverse; the header says `SIMULATION` and an amber banner is on every
  screen. The `aiops.sim` package is forbidden by an AST test from importing anything that can reach
  Docker, NVIDIA, a subprocess or the network, and the scenario is also run with those doors closed.
* **The control plane runs with zero workloads** and says "No workload connected", which is a valid
  healthy state. Unavailable values are shown as "N/A", never as zero.

### What simulation can and cannot do

It runs the *production* engine, detector, deterministic RCA, policy, approval and verifier over a
synthetic world, so the control-loop semantics are the real ones, deterministic and runnable
anywhere (`aiops demo`, the recovery test in the TUI, and the portable CI suites). It cannot tell you that
your GPU, your driver, Docker, vLLM or your network behave: the restart changes a synthetic
lifecycle identity, not a process. It is one scenario (an unresponsive model). Real behaviour is
shown only by the real golden scenario above, on a machine that has the hardware.

### Verification semantics, and why GPU OOM is not the canonical scenario

A restart "succeeding" proves nothing; the verifier observes the workload again. vLLM preallocates
GPU memory at start, so a bounded memory stressor never made it fail (no failures in ~163 probes) and
a larger one breaches the watchdog's own memory limit (ADR-018). A paused container is bounded,
reversible and reliably produces the condition the detector was built for (ADR-019, ADR-030).

### Honest limits

Exercised on one hardware configuration (one consumer GPU, one model, one container runtime); one
real fault type; one practice session at a time; the practice boundary is an in-process object graph,
not a process boundary (DECISIONS D-1); a crashed or exited container looks like an operator stop;
Kubernetes is a stand-in path, not a validated one. None of this is a production-grade guarantee.

## Development

    cargo test  --manifest-path tui/Cargo.toml     # Rust tests (render, app logic, HTTP, safety)
    python -m pytest -m "unit or simulation_golden or tui"   # the portable suites
    scripts/clean_machine_check.sh                 # first-run check from a clean install

### Test suites

Every test belongs to exactly one suite, by where it lives (`tests/conftest.py`):

| Suite | Marker | Needs |
|---|---|---|
| unit | `unit` | Python only |
| simulation golden | `simulation_golden` | Python only (the SIMULATION scenario, its isolation, the practice API) |
| tui | `tui` | `cargo` (the fixture always rebuilds the binary, so it is never stale) |
| real local | `real_local` | `AIOPS_INTEGRATION=1`, a real Docker / kind environment |
| real GPU | `real_gpu` | `AIOPS_GPU=1`, an NVIDIA GPU, a container runtime and the vLLM image |

The first three are **portable**: no GPU, Docker, Kubernetes or network service. CI
(`.github/workflows/ci.yml`) runs them on Python 3.11 and 3.13 with `AIOPS_FAIL_ON_SKIP=1`, so a
skipped test fails the run instead of passing silently, and runs the clean-machine check. The
portable suites were also run here with `docker`, `nvidia-smi`, `kubectl` and `kind` removed from
`PATH`: 791 passed, none skipped.

### Hardware and integration tests

    AIOPS_GPU=1 python -m pytest -m real_gpu             # run each file on its own: they share
    AIOPS_INTEGRATION=1 python -m pytest -m real_local   # one container name, `aiops-vllm`

A skipped test has not passed. Run those suites only on a machine that actually has the
environment, and report them as run only if they ran (`python -m pytest -rs` shows why a test was
skipped).

### Clean-machine procedure

`scripts/clean_machine_check.sh` (also a CI job) does what a first-time user does, in a throwaway
`HOME` and working directory: a non-editable install into a fresh virtualenv, every module
importable, `aiops demo` resolving the SIMULATION scenario with no configuration, `aiops doctor`,
`status` and `stop` saying "no configuration found" (non-zero, no traceback), the operator UI
reporting an unreachable control plane with exit status 3, and nothing left behind (no state, PID,
socket, database, fault lease or running process). Bare `aiops` on a machine with no configuration
offers the default profile when run in a terminal and does not start anything otherwise.
