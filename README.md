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
