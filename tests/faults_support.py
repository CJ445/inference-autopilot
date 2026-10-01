"""A stand-in container runtime for the fault injector tests: containers with labels and a real
lifecycle (running / paused / restarted / replaced), and a record of every command it was asked to run."""
import json
import os
import subprocess

from aiops.docker import MANAGED_LABEL, WORKLOAD_LABEL
from aiops.faults.core import update_lease
from aiops.watchdog import proc_start


def cid(n):
    return (f"{n:x}" * 64)[:64]          # distinct 12-char prefixes: 111..., 222..., 777...


class Container:
    def __init__(self, full_id, workload="vllm", labelled=True, started="2026-10-02T10:00:00Z"):
        self.id, self.started = full_id, started
        self.running, self.paused, self.restarting = True, False, False
        self.labels = ({MANAGED_LABEL: "true", WORKLOAD_LABEL: workload} if labelled else {})


class FakeDocker:
    """`docker ps/inspect/pause/unpause` over an in-memory set of containers."""

    def __init__(self, workload="vllm"):
        self.workload = workload
        self.calls, self.containers = [], {}
        self.before_pause = None          # a hook that runs at the moment `docker pause` is called
        self.fail_pause, self.pause_then_fail = False, False
        self.unpause_delay = 0.0          # widen the window in which two recoveries could overlap
        self.inspect_id_override = None   # make `inspect` report a different id than `ps`
        self.add(Container(cid(1), workload))

    def add(self, container):
        self.containers[container.id] = container
        return container

    @property
    def main(self):
        return self.containers[cid(1)]

    def verbs(self):
        return [c[1] for c in self.calls]

    def restart(self, container, started):
        container.started, container.paused, container.running = started, False, True

    def replace(self, new_id, started="2026-10-02T11:00:00Z"):
        self.containers.pop(cid(1), None)
        return self.add(Container(new_id, self.workload, started=started))

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        verb = argv[1]

        class R:
            returncode, stdout, stderr = 0, "", ""
        r = R()
        if verb == "ps":
            match = [c for c in self.containers.values()
                     if c.labels.get(MANAGED_LABEL) == "true"
                     and c.labels.get(WORKLOAD_LABEL) == self.workload]
            r.stdout = "".join(c.id[:12] + "\n" for c in match)
        elif verb == "inspect":
            c = next((c for c in self.containers.values() if c.id.startswith(argv[2])), None)
            if c is None:
                raise subprocess.CalledProcessError(1, argv, stderr="no such container")
            r.stdout = json.dumps([{"Id": self.inspect_id_override or c.id,
                                    "State": {"Running": c.running, "Paused": c.paused,
                                              "Restarting": c.restarting, "StartedAt": c.started},
                                    "Config": {"Labels": c.labels}}])
        elif verb == "pause":
            if self.before_pause:
                self.before_pause(self)
            c = self.containers[argv[2]]
            if self.fail_pause:
                raise subprocess.CalledProcessError(1, argv, stderr="cannot pause")
            if not c.running or c.paused:
                raise subprocess.CalledProcessError(1, argv, stderr="not pausable")
            c.paused = True
            if self.pause_then_fail:
                raise subprocess.TimeoutExpired(argv, 30)       # it paused, but we were not told
        elif verb == "unpause":
            c = self.containers[argv[2]]
            if self.unpause_delay:
                import time
                time.sleep(self.unpause_delay)
            c.paused = False
        else:                                                   # a verb the injector must never run
            raise AssertionError(f"unexpected docker command {argv}")
        return r


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds

    def sleep(self, seconds):
        self.t += seconds


def live_reaper(path):
    """A spawner for tests: the 'reaper' is this live process (it publishes its identity)."""
    update_lease(path, lambda l: {**l, "reaper": {"pid": os.getpid(), "proc_start": proc_start(os.getpid()),
                                                    "armed_at": "x"}})

    class Proc:
        def poll(self):
            return None
    return Proc()


def dead_reaper(path):
    """A spawner whose process never arms (it 'exits' at once)."""
    class Proc:
        def poll(self):
            return 1
    return Proc()
