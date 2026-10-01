"""`aiops doctor`: read-only diagnosis of configuration and runtime prerequisites.

Never mutates infrastructure: every command goes through ReadOnlyRun, which refuses anything
that is not an allowlisted read. Never fixes anything.
"""
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from aiops.docker import MANAGED_LABEL, WORKLOAD_LABEL, DockerProvider
from aiops.gpu import read_gpu
from aiops.kubectl import ClusterError, KubernetesProvider
from aiops.prometheus import PrometheusAdapter, TelemetryError
from aiops.runtime import QUERIES
from aiops.store import AuditTampered, Store, StoreUnavailable
from aiops.vllm import VllmClient
from aiops.watchdog import _ram_percent

PASS, WARN, FAIL, NOT_APPLICABLE = "PASS", "WARN", "FAIL", "NOT_APPLICABLE"

DOCKER_ONLY = ["docker_cli", "docker_daemon", "nvidia_driver", "gpu", "gpu_expectations",
               "cuda_compat", "gpu_headroom", "vllm_endpoint", "vllm_metrics", "vllm_probe"]
KUBERNETES_ONLY = ["kubectl", "kube_context", "prometheus"]
RECOMMENDED_MAX = {"max_temperature_c": 85, "max_gpu_memory_percent": 90, "max_ram_percent": 95}


class ReadOnlyViolation(Exception):
    pass


def _kubectl_verb(argv):
    it = iter(argv[1:])
    for tok in it:
        if tok in ("--context", "-n"):
            next(it, None)
        elif not tok.startswith("-"):
            return tok
    return None


def _allowed(argv):
    tool = argv[0]
    if tool == "docker":
        return argv[1:2] in (["--version"], ["info"], ["ps"], ["inspect"]) or argv[1:3] == [
            "image", "inspect"]
    if tool == "nvidia-smi":
        return all(a == "-i" or a.isdigit() or a.startswith(("--query-gpu=", "--format="))
                   for a in argv[1:])
    if tool == "kubectl":
        verb = _kubectl_verb(argv)
        return verb == "get" or (verb == "config" and "get-contexts" in argv)
    return False


class ReadOnlyRun:
    def __init__(self, run):
        self._run = run

    def __call__(self, argv, **kw):
        if not _allowed(argv):
            raise ReadOnlyViolation(f"doctor may not run: {argv}")
        return self._run(argv, **kw)


def blocking_failures(results):
    return [r for r in results if r["status"] == FAIL and r["blocking"]]


def format_results(results):
    lines = []
    for r in results:
        note = "" if r["blocking"] or r["status"] != FAIL else "  (workload health; does not block start)"
        lines.append(f"{r['status']:<14} {r['check']:<16} {r['detail']}{note}")
    return "\n".join(lines)


def _version(text):
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


class _Doctor:
    def __init__(self, profile, run, which):
        self.p, self.run, self.which = profile, run, which
        self.w, self.safety = profile["workload"], profile["safety"]
        self.cache = {}

    # -- plumbing ---------------------------------------------------------------------------
    def _out(self, *argv):
        return self.run(list(argv), capture_output=True, text=True, check=True, timeout=15).stdout

    def _gpu(self):
        if "gpu" not in self.cache:
            try:
                self.cache["gpu"] = read_gpu(run=self.run, gpu_index=self.p["gpu"]["index"])
            except TelemetryError as e:
                self.cache["gpu"] = e
        return self.cache["gpu"]

    def _driver_cuda(self):
        try:
            m = re.search(r"CUDA Version:\s*([\d.]+)", self._out("nvidia-smi"))
        except Exception:
            return None
        return m.group(1) if m else None

    # -- common checks ----------------------------------------------------------------------
    def python(self):
        v = sys.version_info
        ok = v >= (3, 11)
        return (PASS if ok else FAIL), f"Python {v.major}.{v.minor}.{v.micro}" + (
            "" if ok else " (3.11+ required for tomllib)")

    def database(self):
        db = Path(self.p["control_plane"]["db"])
        if not db.exists():
            return self._creatable(db, "database")
        if not os.access(db, os.W_OK):
            return FAIL, f"database {db} is not writable: control-plane state could not persist"
        try:
            incidents, pending, audit = Store(db, readonly=True).load()
        except AuditTampered:
            return FAIL, f"audit hash chain in {db} does not verify (tampered or corrupt)"
        except StoreUnavailable as e:
            return FAIL, f"database {db} is unreadable or corrupt: {e}"
        return PASS, (f"{len(incidents)} incidents, {len(pending)} pending proposals, "
                      f"audit chain valid ({len(audit.events)} events)")

    def state_dir(self):
        return self._creatable(Path(self.p["control_plane"]["state_file"]), "state file")

    @staticmethod
    def _creatable(path, what):
        ancestor = path.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK):
            return PASS, f"{what} {path} does not exist yet; it can be created at start"
        return FAIL, f"cannot create {what} {path}: {ancestor} is not writable"

    def safety_limits(self):
        loose = [f"{k}={self.safety[k]} (recommended <= {v})" for k, v in RECOMMENDED_MAX.items()
                 if self.safety.get(k, 0) > v]
        if loose:
            return WARN, "looser than recommended: " + ", ".join(loose)
        return PASS, "safety limits are within the recommended range"

    # -- docker-real-gpu --------------------------------------------------------------------
    def docker_cli(self):
        path = self.which("docker")
        return (PASS, f"docker CLI at {path}") if path else (FAIL, "docker CLI not found on PATH")

    def docker_daemon(self):
        try:
            return PASS, f"docker daemon {self._out('docker', 'info', '--format', '{{.ServerVersion}}').strip()}"
        except Exception as e:
            return FAIL, f"docker daemon unavailable: {getattr(e, 'stderr', None) or e}"

    def workload(self):
        try:
            state = DockerProvider(self.w["name"], run=self.run).get_workload(self.w["name"])
        except ClusterError as e:
            return FAIL, str(e)
        who = f"managed container {self.w['name']!r} identified ({state['id'][:19]}...)"
        if not state["ready"]:
            return WARN, who + "; not ready (not running, paused, or health is not healthy)"
        return PASS, who + ", running and ready"

    def nvidia_driver(self):
        try:
            driver = self._out("nvidia-smi", "--query-gpu=driver_version",
                               "--format=csv,noheader").strip()
        except Exception as e:
            return FAIL, f"NVIDIA driver unavailable: {e}"
        cuda = self._driver_cuda()
        return PASS, f"driver {driver}, " + (f"supports CUDA up to {cuda}" if cuda
                                             else "CUDA version not reported")

    def gpu(self):
        g = self._gpu()
        if isinstance(g, Exception):
            return FAIL, f"gpu unreadable: {g}"
        return PASS, (f"{g['gpu_uuid']}, {g['gpu_memory_total_bytes'] / 2**20:.0f} MiB total, "
                      f"{g['gpu_temperature_c']:.0f} C")

    def gpu_expectations(self):
        pin, min_mib = self.p["gpu"]["uuid"], self.p["gpu"]["min_memory_mib"]
        if pin is None and min_mib is None:
            return NOT_APPLICABLE, "no GPU expectations configured"
        g = self._gpu()
        if isinstance(g, Exception):
            return FAIL, f"cannot verify GPU expectations: gpu unreadable: {g}"
        if pin and g["gpu_uuid"] != pin:
            return FAIL, f"gpu uuid {g['gpu_uuid']} does not match the pinned {pin}"
        total = g["gpu_memory_total_bytes"] / 2**20
        if min_mib and total < min_mib:
            return FAIL, f"gpu memory {total:.0f} MiB is below the expected minimum {min_mib} MiB"
        return PASS, "GPU matches the configured expectations"

    def cuda_compat(self):
        try:
            image = self._out("docker", "ps", "-a", "--filter", f"label={MANAGED_LABEL}=true",
                              "--filter", f"label={WORKLOAD_LABEL}={self.w['name']}",
                              "--format", "{{.Image}}").split()
        except Exception as e:
            return NOT_APPLICABLE, f"cannot inspect the managed workload's image: {e}"
        if len(image) != 1:
            return NOT_APPLICABLE, "managed workload not found (see the workload check)"
        image = image[0]
        driver = self._driver_cuda()
        try:
            env = self._out("docker", "image", "inspect", image, "--format",
                            "{{range .Config.Env}}{{println .}}{{end}}")
        except Exception as e:
            return WARN, f"cannot read the CUDA version of image {image}: {e}"
        m = re.search(r"^CUDA_VERSION=([\d.]+)", env, re.M)
        if not m:
            return WARN, f"image {image} declares no CUDA_VERSION; compatibility unknown"
        img, d = m.group(1), _version(driver)
        if d is None:
            return WARN, (f"image {image} needs CUDA {img} but the driver's CUDA version is "
                          "not reported")
        i = _version(img)
        facts = f"image {image} is built for CUDA {img}; driver supports CUDA {driver}"
        if i[0] > d[0]:
            return FAIL, facts + ": newer major version, the CUDA runtime will not initialise"
        if i[0] == d[0] and i[1] > d[1]:
            return WARN, facts + ": newer minor version (minor-version compatibility, unproven)"
        return PASS, facts

    def gpu_headroom(self):
        g = self._gpu()
        if isinstance(g, Exception):
            return FAIL, f"cannot read GPU headroom: gpu unreadable: {g}"
        s, total = self.safety, g["gpu_memory_total_bytes"]
        problems = []
        if g["gpu_temperature_c"] > s["max_temperature_c"]:
            problems.append(f"gpu temperature {g['gpu_temperature_c']:.0f} C is above the "
                            f"configured maximum {s['max_temperature_c']} C")
        used_pct = 100 * g["gpu_memory_used_bytes"] / total
        if used_pct > s["max_gpu_memory_percent"]:
            problems.append(f"gpu memory {used_pct:.0f}% is above the configured maximum "
                            f"{s['max_gpu_memory_percent']}%")
        ram = _ram_percent()
        if ram > s["max_ram_percent"]:
            problems.append(f"RAM {ram:.0f}% is above the configured maximum "
                            f"{s['max_ram_percent']}%")
        if s["gpu_memory_threshold_bytes"] >= total:
            problems.append(f"gpu_memory_threshold_bytes ({s['gpu_memory_threshold_bytes']}) is "
                            f"not below total GPU memory ({total:.0f}); the detector could never fire")
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, (f"temperature {g['gpu_temperature_c']:.0f} C, gpu memory {used_pct:.0f}%, "
                      f"RAM {ram:.0f}% are within the configured limits")

    def vllm_endpoint(self):
        try:
            with urllib.request.urlopen(self.w["vllm_url"] + "/health", timeout=3) as r:
                return PASS, f"{self.w['vllm_url']}/health answered {r.status}"
        except Exception as e:
            return FAIL, f"{self.w['vllm_url']}/health unreachable: {e}"

    def _vllm(self):
        return VllmClient(self.w["vllm_url"], self.w["model"], timeout=3)

    def vllm_metrics(self):
        try:
            return PASS, f"{len(self._vllm().metrics())} vLLM signals readable"
        except TelemetryError as e:
            return FAIL, str(e)

    def vllm_probe(self):
        p = self._vllm().probe()
        return (PASS, f"real completion in {p['latency_ms']:.0f} ms") if p["ok"] else (
            FAIL, f"inference probe failed: {p['error']}")

    # -- kubernetes -------------------------------------------------------------------------
    def kubectl(self):
        path = self.which("kubectl")
        return (PASS, f"kubectl at {path}") if path else (FAIL, "kubectl not found on PATH")

    def kube_context(self):
        try:
            names = self._out("kubectl", "config", "get-contexts", "-o", "name").split()
        except Exception as e:
            return FAIL, f"cannot list kubectl contexts: {e}"
        ctx = self.w["context"]
        if ctx in names:
            return PASS, f"kubectl context {ctx!r} exists"
        return FAIL, f"kubectl context {ctx!r} not found (available: {', '.join(names) or 'none'})"

    def kube_workload(self):
        try:
            state = KubernetesProvider(self.w["namespace"], run=self.run,
                                       context=self.w["context"]).get_workload(self.w["name"])
        except ClusterError as e:
            return FAIL, str(e)
        return (PASS, f"pod {self.w['name']!r} found and ready") if state["ready"] else (
            WARN, f"pod {self.w['name']!r} found but not ready")

    def prometheus(self):
        try:
            PrometheusAdapter(self.w["prometheus_url"], QUERIES).metrics()
        except TelemetryError as e:
            return FAIL, f"prometheus telemetry unavailable: {e}"
        return PASS, f"{self.w['prometheus_url']} answers every required query"


# (check name, method, blocking). vLLM health checks report FAIL honestly but never block
# `start`: an unhealthy workload is what the control plane exists to detect and remediate,
# and refusing to start would strand a pending approval after a crash.
COMMON = [("python", "python", True), ("database", "database", True),
          ("state_dir", "state_dir", True), ("safety_limits", "safety_limits", False)]
DOCKER = [("docker_cli", "docker_cli", True), ("docker_daemon", "docker_daemon", True),
          ("workload", "workload", True), ("nvidia_driver", "nvidia_driver", True),
          ("gpu", "gpu", True), ("gpu_expectations", "gpu_expectations", True),
          ("cuda_compat", "cuda_compat", True), ("gpu_headroom", "gpu_headroom", True),
          ("vllm_endpoint", "vllm_endpoint", False), ("vllm_metrics", "vllm_metrics", False),
          ("vllm_probe", "vllm_probe", False)]
KUBERNETES = [("kubectl", "kubectl", True), ("kube_context", "kube_context", True),
              ("workload", "kube_workload", True), ("prometheus", "prometheus", True)]


def run_doctor(profile, run=subprocess.run, which=shutil.which):
    d = _Doctor(profile, run if isinstance(run, ReadOnlyRun) else ReadOnlyRun(run), which)
    plan = COMMON + (DOCKER if profile["provider"] == "docker" else KUBERNETES)
    results = []
    for name, method, blocking in plan:
        try:
            status, detail = getattr(d, method)()
        except Exception as e:  # a check that cannot run is a failed check, never a pass
            status, detail = FAIL, f"check could not run: {type(e).__name__}: {e}"
        results.append({"check": name, "status": status, "detail": detail, "blocking": blocking})
    ran = {r["check"] for r in results}
    for name in DOCKER_ONLY + KUBERNETES_ONLY:
        if name not in ran:
            results.append({"check": name, "status": NOT_APPLICABLE, "blocking": False,
                            "detail": f"not applicable to the {profile['name']} profile"})
    return results
