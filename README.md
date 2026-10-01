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

`aiops` is the Python package's command. Use a virtual environment and an editable install, so
the command keeps pointing at this checkout (and the TUI built above):

    python3 -m venv .venv
    . .venv/bin/activate
    pip install -e .

pip puts the executable in the environment's `bin/` directory (`.venv/bin/aiops`), which
activating the environment adds to `PATH`. Without activating it, call `.venv/bin/aiops`, or add
the directory yourself for the current shell:

    export PATH="$PWD/.venv/bin:$PATH"

To make that permanent, put the same line (with the absolute path of the checkout) in your shell
configuration, e.g. `~/.bashrc`. Nothing in the application hard-codes a home directory.

The Rust binary is **not** the `aiops` command and `cargo install` is not the install path.

### 3. Verify

    which aiops
    aiops --help

### 4. Configure

`aiops` reads a runtime profile (`aiops.toml` in the current directory, or `--config PATH`).
Examples are in `deploy/profiles/` (`docker-real-gpu.toml`, `kubernetes.toml`). A profile names
exactly one managed workload; unknown keys are errors.

## Using it

    aiops

That is the normal way in. It looks for a running control plane for the profile:

* **already running** → attaches to it and opens the TUI. No second control plane is started.
* **not running** → runs the existing `aiops start` detached (its own session, output in
  `<state_file>.log`), shows the lifecycle's own startup lines, waits until the API answers,
  then opens the TUI.
* **cannot start** (a blocking prerequisite failed, the watchdog could not arm, the port is
  taken, the state file is unreadable, or it did not answer in time) → prints the reason and
  the last lines of its output and exits non-zero. The TUI is not opened over a broken start.

Quitting the TUI (`Q`) leaves the control plane running. To stop it, use the TUI's *Control
Plane* screen (`4`, then `X`, then Enter) or, from a shell, `aiops stop`.

Inside the TUI: `Ctrl+P` opens the command palette; `?` opens Help. Screens: Overview,
Incidents, Audit, Control Plane, Settings, Diagnostics, About, Help (`1`–`7`, `?`, or `Tab`).
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
