# Inference Autopilot

[![ci](https://github.com/CJ445/inference-autopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/CJ445/inference-autopilot/actions/workflows/ci.yml)

An AIOps control plane for LLM inference servers, with a terminal UI. When the model server stops
answering, it finds the cause, proposes a fix, **waits for your approval**, applies it, and checks
that the model really recovered. No LLM is in the loop, and nothing runs without your OK.

![Recovery test in the TUI: detect, diagnose, approve, verify](docs/media/tui-recovery.gif)

    evidence → deterministic RCA → typed proposal → policy → your approval → remediation → verification → audit

## Quickstart (2 minutes, no GPU, Docker or Rust needed)

Requires Python ≥ 3.11 and `git`. Everything below is a **simulation**: it runs the real control
loop over a synthetic workload and cannot touch anything on your machine.

    git clone https://github.com/CJ445/inference-autopilot && cd inference-autopilot
    python3 -m venv .venv && . .venv/bin/activate && pip install -e .
    aiops demo --approve

![aiops demo --approve](docs/media/demo-approve.gif)

Prefer a permanent command on your `PATH`? `uv tool install --editable .` instead of the venv line.

## The terminal UI

The UI is a small Rust program, so this step needs [Rust](https://rustup.rs) (`cargo`):

    cargo build --release --manifest-path tui/Cargo.toml
    aiops

The first time, `aiops` offers to create `~/.config/aiops/aiops.toml` (it asks, and never
overwrites). It then starts the control plane in the background and opens the UI. To try the failure
loop with no hardware: press **`3`** (Lab), **`Enter`** to run the recovery test, **`A`** then
**`Enter`** to approve the simulated restart. `Q` quits and leaves the control plane running;
`aiops stop` ends it. The full tour is in [docs/tui-guide.md](docs/tui-guide.md).

> The default profile expects an NVIDIA GPU and Docker, because `aiops` checks them at start. On a
> machine without them `aiops` will say which prerequisite failed; `aiops demo` still works.

## Commands

| Command | What it does |
|---|---|
| `aiops` | Open the UI, starting the control plane first if needed |
| `aiops demo --approve` / `--reject` | Run the simulated failure loop in the terminal, no setup |
| `aiops status` | Observed state of the control plane and workload |
| `aiops doctor` | Read-only check of prerequisites |
| `aiops stop` | Stop the control plane (also disarms the watchdog) |
| `aiops start` | Run the control plane in the foreground (scripts, debugging) |
| `aiops tui` | Attach the UI only; never starts a control plane |

## With a real GPU

Point it at your own vLLM container and it manages that one container only: it never creates,
starts or stops it, and a restart happens only after your approval. Setup, the Docker-pause
scenario and the guarded fault test are in [docs/real-hardware.md](docs/real-hardware.md).

## Documentation

| | |
|---|---|
| [The terminal UI](docs/tui-guide.md) | Screens, keys, how deciding works, themes |
| [Simulation](docs/simulation.md) | What `aiops demo` and the in-UI recovery test do, and what they cannot prove |
| [Real hardware](docs/real-hardware.md) | vLLM container, the real scenario, the guarded fault |
| [Safety boundaries](docs/safety.md) | What the system will and will not do, and the tests that enforce it |
| [Architecture](docs/architecture.md) | How the launcher, control plane, TUI and watchdog fit together |
| [Development](docs/development.md) | Tests, CI, the clean-machine check, re-recording the GIFs |
| [PRD](PRD.md), [Decisions](DECISIONS.md) | Specification and design decisions |

## Troubleshooting

* **`the operator TUI is not built`**: run the `cargo build` line above (or set `AIOPS_TUI_BIN`).
* **`aiops opens an interactive terminal UI; this is not a terminal`**: use `aiops start | stop |
  status | doctor` in scripts.
* **`aiops` cannot start (GPU or Docker prerequisite)**: run `aiops doctor` for the exact failing
  check; the output of the failed start is in `~/.config/aiops/aiops.state.json.log`.
* **Port in use**: change `port` under `[control_plane]` in `~/.config/aiops/aiops.toml`.
* **`aiops: command not found`**: activate the venv (`. .venv/bin/activate`) or use `uv tool install`.

## Status

Exercised on one hardware configuration (one consumer GPU, one model, one container runtime) and
one real fault type. Not a production-grade guarantee; see [Safety boundaries](docs/safety.md#honest-limits).
