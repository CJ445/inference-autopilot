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
`docker stop` looks like a hang for a few seconds, so it can open an incident that stays pending
until you reject it.

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

Quitting the TUI (`Q`) leaves the control plane running. To stop it, use the TUI's *Control
Plane* screen (`4`, then `X`, then Enter) or, from a shell, `aiops stop`.

Deciding an incident: open it (`Enter`) and read its evidence; `A` (approve) or `R` (reject) work
only on that page, and only once the incident's details have loaded. The confirmation shows the
incident, the server's reason, and the effect (a restart interrupts inference and has no
rollback); `Enter` is ignored for the first half second so a held key cannot confirm it. While the
server executes and verifies, an inline banner shows the server's current state for that incident.

Inside the TUI: `Ctrl+P` opens the command palette; `?` opens Help. Screens: Overview,
Incidents, Audit, Control Plane, Settings, Diagnostics, About, Help (`←`/`→`, `Tab`, `1`–`7` or `?`).
Settings and Diagnostics are read-only views of what the server reports (the validated active
configuration; the same checks as `aiops doctor`). Restarting the control plane is not done from
the TUI; run `aiops` again after a stop.

The explicit commands remain for scripts and debugging:

    aiops start      # run the control plane in the foreground
    aiops stop       # graceful stop (also disarms the watchdog)
    aiops status     # observed state of the control plane and workload
    aiops doctor     # read-only prerequisite check
    aiops tui        # attach the TUI only; never starts a control plane

## Try the failure loop safely (SIMULATION)

In the TUI: press **`P`** (or `Ctrl+P` then "Practice an incident"). A persistent amber banner,
`PRACTICE · SIMULATION`, appears on every screen, and the Overview tells you what to do next from
the stage the server reports: **`F`** breaks the (simulated) model, the detector opens an incident
after two failed checks in a row, you open it (`Enter`), review it and approve (`A`), a simulated
restart runs, and the production verifier judges recovery from the simulated state. `P` practices
again; `Esc` on the Overview leaves practice and returns to the real system. The confirmation says
"A simulated restart: nothing real is restarted." The real system, its incidents, its watchdog and
its workload are never touched, and nothing from the practice is ever shown as real (or the
reverse): the practice has its own routes (`/api/v1/practice/...`), its own engine, store and lock,
and the TUI discards any poll that belongs to the other one.

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

In the TUI: `Ctrl+P`, then **"Break the real workload (pause)…"**. There is deliberately no plain
key for it. A dialog says exactly what will happen (`LIVE · GPU-REAL`: the workload stops answering
until it is automatically resumed in 2 minutes, or the recovery flow restarts it) and nothing is
sent until you press `Enter` (ignored for the first half second). While the fault is active a red
banner on every screen shows the time left; `Ctrl+P`, then **"Resume the workload now"** ends it
sooner. It is not offered while practicing, while a fault is already active, or on a control plane
without the Docker provider.

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

One thing to know: if you do nothing, the workload resumes when the lease ends, the hang clears,
and the incident it produced stays pending (the known ADR-019 gap); approving it would restart a
healthy workload. The default 2 minutes is enough to review and approve.

## Development

    cargo build --manifest-path tui/Cargo.toml     # debug build of the TUI
    cargo test  --manifest-path tui/Cargo.toml     # Rust tests (render, app logic, HTTP, safety)
    python -m pytest                               # Python tests

The Python PTY tests run the **built** TUI binary. Rebuild it after changing `tui/` or they will
exercise a stale one:

    cargo build --release --manifest-path tui/Cargo.toml

### Hardware and integration tests

Some tests need real hardware or infrastructure and are skipped unless you opt in:

    AIOPS_GPU=1 python -m pytest          # needs an NVIDIA GPU, a driver, a container runtime
                                          # and the vLLM image
    AIOPS_INTEGRATION=1 python -m pytest  # needs the integration environment

A skipped test has not passed. Run those suites only on a machine that actually has the
environment, and report them as run only if they ran. The pytest summary shows how many were
skipped (`python -m pytest -rs` shows why).
