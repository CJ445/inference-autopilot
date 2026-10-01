"""The guarded fault injector: pause the managed workload, with a lease that always ends it.

Order is the safety property (see DECISIONS.md D-12): preconditions, then the lease is persisted,
then an independent reaper is armed and verified alive, then the identity is re-checked, and only
then is the workload paused. Whatever happens next (a crash of this process, of the control plane,
of the reaper), the lease and a recovery layer remain.
"""
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from aiops.docker import WorkloadAbsent, inspect_workload
from aiops.faults.core import (ACTIVE_STATES, DEFAULT_DURATION, FAULT_PAUSE, FAULT_TYPES,
                               MAX_DURATION, MIN_DURATION, TERMINAL_STATES, AllowlistedDocker,
                               FaultError, expired, identity_of, now_iso, provider_for, read_lease,
                               recover, update_lease, write_lease)
from aiops.kubectl import ClusterError
from aiops.watchdog import proc_start

ARM_TIMEOUT_SECONDS = 5
MONITOR_SECONDS = 1.0


def spawn_reaper(lease_path):
    """The real reaper: its own session, no stdin, importing nothing but the fault core."""
    return subprocess.Popen([sys.executable, "-m", "aiops.faults.reaper", "--lease", str(lease_path)],
                            start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def process_alive(record):
    pid = (record or {}).get("pid")
    return type(pid) is int and proc_start(pid) is not None and proc_start(pid) == record.get("proc_start")


class FaultInjector:
    def __init__(self, workload, lease_path, run=None, spawn=spawn_reaper, clock=time.time,
                 audit=None, sleep=time.sleep):
        self.workload, self.lease_path = workload, Path(lease_path)
        self._run = run or AllowlistedDocker(workload)
        self._spawn, self._clock, self._sleep = spawn, clock, sleep
        self.audit = audit                  # callable(event, data); set by the control plane
        self._lock = threading.RLock()
        self._stop, self._thread = threading.Event(), None

    # -- reading ---------------------------------------------------------------------------------

    def lease(self):
        return read_lease(self.lease_path)

    def summary(self):
        """What the API and the TUI see: whether faults are offered, and the active lease if any."""
        lease = self.lease_or_none()
        active = lease if lease and lease["status"] in ACTIVE_STATES else None
        return {"available": True,
                "active": None if active is None else {
                    "fault_id": active["fault_id"], "type": active["fault_type"],
                    "workload": active["workload"], "status": active["status"],
                    "created_at": active["created_at"], "expires_at": active["expires_at"],
                    "expires_at_iso": now_iso(active["expires_at"])}}

    def last(self):
        """The most recent finished fault (what happened and how it ended), or None."""
        lease = self.lease_or_none()
        if lease is None or lease["status"] in ACTIVE_STATES:
            return None
        return {"fault_id": lease["fault_id"], "type": lease["fault_type"], "status": lease["status"],
                "recovery": lease.get("recovery"), "failure": lease.get("failure")}

    def lease_or_none(self):
        try:
            return self.lease()
        except FaultError:
            return None

    # -- injecting ---------------------------------------------------------------------------------

    def inject(self, fault_type, target, duration):
        with self._lock:
            if fault_type not in FAULT_TYPES:
                raise FaultError("INVALID_REQUEST", f"unknown fault type {fault_type!r}")
            if target != self.workload:
                raise FaultError("INVALID_REQUEST", f"{target!r} is not the configured workload")
            if type(duration) is not int or not MIN_DURATION <= duration <= MAX_DURATION:
                raise FaultError("INVALID_REQUEST", f"duration_seconds must be an integer from "
                                                    f"{MIN_DURATION} to {MAX_DURATION}")
            existing = self.lease()                      # an unreadable lease also refuses: fail closed
            if existing and existing["status"] in ACTIVE_STATES:
                raise FaultError("FAULT_ACTIVE", "a fault is already active")

            first = self._precheck()                     # 1. preconditions
            identity = identity_of(first)
            now = self._clock()
            lease = {"fault_id": uuid.uuid4().hex[:12], "fault_type": fault_type,
                     "workload": self.workload, "container_id": first["Id"],
                     "expected_identity": identity, "status": "ARMING",
                     "created_at": now_iso(now), "created_epoch": now,
                     "expires_at": now + duration, "duration_seconds": duration,
                     "audited_injected": False, "audited_recovered": False}
            write_lease(self.lease_path, lease)          # 2. persisted BEFORE anything is paused

            try:
                self._arm(lease)                         # 3. the safety net exists and is alive
                second = self._precheck()                # 4. nothing changed in the meantime
                if identity_of(second) != identity:
                    raise FaultError("PRECONDITION_FAILED", "the workload changed while arming the fault")
                self._pause(second["Id"])                # 5. the only mutation
            except BaseException as e:
                self._abort(lease["fault_id"], str(e))
                if isinstance(e, FaultError):
                    raise
                raise FaultError("PRECONDITION_FAILED", f"could not pause the workload: {e}") from e
            update_lease(self.lease_path, lambda l: {**l, "status": "ACTIVE", "paused_at": now_iso(self._clock())})
            return self.summary()["active"]

    def _precheck(self):
        provider = provider_for(self.workload, self._run)
        try:
            container = inspect_workload(provider, self.workload)
        except WorkloadAbsent:
            raise FaultError("PRECONDITION_FAILED", "no managed workload is present") from None
        except ClusterError as e:
            raise FaultError("PRECONDITION_FAILED", f"the workload cannot be identified safely: {e}") from e
        state = container["State"]
        if not state.get("Running") or state.get("Restarting"):
            raise FaultError("PRECONDITION_FAILED", "the workload is not running")
        if state.get("Paused"):
            raise FaultError("PRECONDITION_FAILED", "the workload is already paused")
        return container

    def _arm(self, lease):
        proc = self._spawn(self.lease_path)
        deadline = self._clock() + ARM_TIMEOUT_SECONDS
        while self._clock() < deadline:
            current = self.lease()
            if current and process_alive(current.get("reaper")):
                return
            if hasattr(proc, "poll") and proc.poll() is not None:
                break
            self._sleep(0.05)
        raise FaultError("PRECONDITION_FAILED", "the recovery process did not arm; nothing was paused")

    def _pause(self, container_id):
        try:
            self._run(["docker", "pause", container_id], capture_output=True, text=True, check=True)
        except (OSError, subprocess.SubprocessError) as e:
            raise FaultError("PRECONDITION_FAILED", f"docker pause failed: {e}") from e

    def _abort(self, fault_id, why):
        """The injection did not complete: make sure nothing stays paused, then record the failure."""
        try:
            recover(self.lease_path, self._run, by="injector", clock=self._clock)
        except Exception:                      # best effort; the reaper and the monitor remain
            traceback.print_exc()

        def mark(lease):
            if lease["fault_id"] != fault_id:
                return None
            return {**lease, "status": "FAILED", "failure": why[:200]}
        try:
            update_lease(self.lease_path, mark)
        except FaultError:
            pass

    # -- ending ------------------------------------------------------------------------------------

    def cancel(self, by="operator"):
        """Resume the workload now (identity-verified, idempotent)."""
        with self._lock:
            lease = self.lease()
            if not lease or lease["status"] not in ACTIVE_STATES:
                raise FaultError("PRECONDITION_FAILED", "no fault is active")
            recover(self.lease_path, self._run, by=by, clock=self._clock)
            return self.lease()

    def sweep(self):
        """Control-plane start: end any fault whose deadline has passed (and audit what is pending)."""
        with self._lock:
            lease = self.lease_or_none()
            if lease and expired(lease, self._clock):
                recover(self.lease_path, self._run, by="control-plane", clock=self._clock)
            self._flush_audit()

    def shutdown(self):
        """Control-plane stop: a graceful stop ends the experiment."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            lease = self.lease_or_none()
            if lease and lease["status"] in ACTIVE_STATES:
                recover(self.lease_path, self._run, by="control-plane-stop", clock=self._clock)
            self._flush_audit()

    # -- monitoring and audit --------------------------------------------------------------------------

    def start_monitor(self, seconds=MONITOR_SECONDS):
        if self._thread is None:
            self._thread = threading.Thread(target=self._monitor, args=(seconds,),
                                            name="aiops-faults", daemon=True)
            self._thread.start()

    def _monitor(self, seconds):
        while not self._stop.wait(seconds):
            try:
                self.tick()
            except Exception:                    # the monitor must outlive one bad pass
                traceback.print_exc()

    def tick(self):
        with self._lock:
            lease = self.lease_or_none()
            if lease and lease["status"] in ACTIVE_STATES:
                if expired(lease, self._clock):
                    recover(self.lease_path, self._run, by="control-plane", clock=self._clock)
                elif not process_alive(lease.get("reaper")):
                    self._spawn(self.lease_path)         # the safety net died: put it back
            self._flush_audit()

    def _flush_audit(self):
        """Record the lease's events in the control plane's audit chain, once each (at-least-once:
        a failure to append is retried on the next pass)."""
        lease = self.lease_or_none()
        if lease is None or self.audit is None:
            return
        base = {"fault_id": lease["fault_id"], "type": lease["fault_type"],
                "workload": lease["workload"], "identity": lease["expected_identity"]}
        if lease.get("paused_at") and not lease["audited_injected"]:
            self.audit("fault_injected", {**base, "duration_seconds": lease["duration_seconds"],
                                          "expires_at": now_iso(lease["expires_at"])})
            lease = update_lease(self.lease_path, lambda l: {**l, "audited_injected": True})
        if lease["status"] in TERMINAL_STATES and not lease["audited_recovered"]:
            self.audit("fault_ended", {**base, "status": lease["status"],
                                       "recovery": lease.get("recovery"),
                                       "failure": lease.get("failure")})
            update_lease(self.lease_path, lambda l: {**l, "audited_recovered": True})
