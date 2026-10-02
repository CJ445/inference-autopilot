# Architecture

An AIOps control plane for LLM inference infrastructure (Python) with an operator terminal UI
(Rust/Ratatui). The product is built around one flow, enforced by the server:

    evidence → deterministic RCA → typed proposal → policy → operator approval
             → remediation → verification → audit

`PRD.md` is the specification.


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
