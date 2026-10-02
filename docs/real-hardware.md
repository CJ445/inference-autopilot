# Real hardware: GPU, Docker and vLLM

Everything here needs an NVIDIA GPU, Docker and a vLLM container you provision yourself. None of it is needed to try the project: see [simulation.md](simulation.md) and the README quickstart.

## The vLLM container (operator-controlled)



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

## The real golden scenario (Docker pause)


This is the project's official end-to-end demonstration on real hardware. It uses a real GPU, a real
vLLM and your real approval; nothing is simulated. It needs the container above (`aiops doctor` clean
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
