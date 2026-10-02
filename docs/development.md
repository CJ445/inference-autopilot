# Development


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

### Re-recording the README demos

The GIFs in `docs/media/` are generated, not hand-made. `scripts/record_demos.py` drives the real
`aiops` in a pseudo-terminal (a throwaway `HOME`, a spare port, the SIMULATION only), writes
asciinema casts and renders GIFs with [`agg`](https://github.com/asciinema/agg):

    pip install pexpect pyte                  # plus `agg` on PATH or in ~/.local/bin
    cargo build --release --manifest-path tui/Cargo.toml
    scripts/record_demos.py                   # all four, or name some: tui-recovery demo-approve

Re-run it after a UI change that alters what a demo shows.
