# The terminal UI

The full tour of the `aiops` operator UI: what each screen is, the keys, and how deciding works.

    aiops

![A tour of the TUI: command palette, help, System](media/tui-tour.gif)

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
