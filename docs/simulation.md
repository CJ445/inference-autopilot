# Try the failure loop safely (SIMULATION)

![Recovery test in the TUI](media/tui-recovery.gif)

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

![aiops demo --approve](media/demo-approve.gif)

    aiops demo --approve        # or --reject; with neither, it asks when run in a terminal

Rejecting instead restarts nothing and ends the incident without a recovery:

![aiops demo --reject](media/demo-reject.gif)

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
about your real hardware; the real golden scenario in [real-hardware.md](real-hardware.md) does.
