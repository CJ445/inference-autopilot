# Safety boundaries


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
shown only by the real golden scenario in [real-hardware.md](real-hardware.md), on a machine that has the hardware.

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
