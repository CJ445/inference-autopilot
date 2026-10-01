"""Practice mode: one SIMULATION session hosted by the running control plane.

The TUI can only speak HTTP to the control plane, so the control plane hosts the simulation. The
session is a completely separate object graph (its own world, engine, store and lock): nothing in
it is shared with the real engine, and it is built only from `aiops.sim`, which cannot reach
Docker, NVIDIA, a subprocess or the network.
"""
import os
import tempfile
import threading
import traceback
from pathlib import Path

from aiops.sim.session import SimSession

TICK_SECONDS = 1.0


class PracticeStateError(Exception):
    """The request does not fit where the practice session is."""


class PracticeHost:
    def __init__(self, tick_seconds=TICK_SECONDS, verify_timeout=5):
        self.lock = threading.RLock()
        self.session = None
        self._scratch = None
        self._tick_seconds, self._verify_timeout = tick_seconds, verify_timeout
        self._stop, self._thread = threading.Event(), None

    @property
    def info(self):
        s = self.session
        return {"profile": "practice", "provider": "simulation",
                "workload": s.world.workload, "model": s.world.model}

    def summary(self):
        s = self.session
        return {"active": s is not None, "mode": "SIMULATION",
                "stage": s.stage if s else None}

    def start(self):
        """A fresh session (any previous one is discarded). It starts healthy."""
        with self.lock:
            self._discard()
            self._scratch = tempfile.TemporaryDirectory(prefix="aiops-practice-")
            self.session = SimSession(Path(self._scratch.name) / "practice.db",
                                      verify_timeout=self._verify_timeout)
            self.session.tick()                 # an observation exists from the first poll
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="aiops-practice",
                                                daemon=True)
                self._thread.start()

    def fault(self):
        with self.lock:
            if self.session is None:
                raise PracticeStateError("no practice session is running")
            if self.session.stage != "healthy":
                raise PracticeStateError("the fault can only be injected while the model is healthy")
            self.session.inject_fault()

    def stop(self):
        with self.lock:
            self._discard()

    def shutdown(self):
        """Control plane stopping: end the session and the ticker."""
        self._stop.set()
        with self.lock:
            self._discard()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _discard(self):
        self.session = None
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    def _loop(self):
        while not self._stop.wait(self._tick_seconds):
            with self.lock:
                if self.session is None:
                    continue
                try:
                    self.session.tick()
                except Exception:      # a bad simulated tick must not end the ticker
                    traceback.print_exc()
