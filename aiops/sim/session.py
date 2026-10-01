"""A simulation session: the production Engine and the production verifier over a SimWorld."""
from aiops.engine import Engine
from aiops.sim.world import SimProvider, SimTelemetry, SimWorld
from aiops.store import Store

MODE = "SIMULATION"
GPU_THRESHOLD = 4_500_000_000
CONFIG = {"service": "vllm", "gpu_threshold": GPU_THRESHOLD, "timeout": 5, "interval": 0.0,
          "stable_probes": 3, "probe_interval": 0.0}   # no sleeping: deterministic and instant


class SimSession:
    """One isolated simulation. `db_path` must be a path nobody uses for real state."""

    def __init__(self, db_path, world=None, verify_timeout=5):
        self.world = world or SimWorld()
        self.store = Store(db_path, mode=MODE)         # refuses any database that is not a sim one
        self.engine = Engine(
            SimTelemetry(self.world), SimProvider(self.world),
            {**CONFIG, "workload": self.world.workload, "timeout": verify_timeout},
            store=self.store,
            presence=lambda: "running" if self.world.running else "absent", mode=MODE)

    def tick(self):
        return self.engine.tick()

    def inject_fault(self):
        """The simulated fault, recorded in the (SIMULATION) audit chain like any other event."""
        self.world.inject_unresponsive()
        self.engine.audit.append("fault_injected", {"fault": "inference_unresponsive",
                                                    "workload": self.world.workload})
        self.store.save(self.engine.incidents, self.engine.pending, self.engine.audit)

    @property
    def incident(self):
        return self.engine.incidents[-1] if self.engine.incidents else None

    def approve(self):
        self.engine.approve(self.incident.incident_id)

    def reject(self):
        self.engine.reject(self.incident.incident_id)

    @property
    def stage(self):
        """Where the loop is, derived from the engine's real state, never scripted."""
        inc = self.incident
        if inc is None:
            return "detecting" if not self.world.inference_ok else "healthy"
        return {"POLICY_CHECK": "awaiting_approval", "APPROVED": "recovering",
                "EXECUTING": "recovering", "VERIFYING": "recovering",
                "RESOLVED": "resolved"}.get(inc.status, inc.status.lower())
