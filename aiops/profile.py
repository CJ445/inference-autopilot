"""Runtime profiles: explicit, validated, fail-closed configuration (TOML, stdlib only).

A profile names ONE managed workload. There is no discovery and no way to name a second
container; every unknown section or key is an error.
"""
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from aiops.docker import NAME as DOCKER_NAME
from aiops.policy import K8S_NAME

REQUIRED = object()
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
CONTEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,252}")
GPU_UUID = re.compile(r"GPU-[0-9a-fA-F-]{8,}")


class ProfileError(Exception):
    pass


def _int(lo, hi):
    def check(v):
        if type(v) is not int or not lo <= v <= hi:
            raise ValueError(f"must be an integer between {lo} and {hi}")
        return v
    return check


def _num(lo, strict=True):
    def check(v):
        if type(v) not in (int, float) or (v <= lo if strict else v < lo):
            raise ValueError(f"must be a number {'>' if strict else '>='} {lo}")
        return v
    return check


def _fraction(v):
    if type(v) not in (int, float) or not 0 < v <= 1:
        raise ValueError("must be a number in (0, 1]")
    return v


def _regex(pattern, what):
    def check(v):
        if not isinstance(v, str) or not pattern.fullmatch(v):
            raise ValueError(f"must be a valid {what}")
        return v
    return check


def _string(v):
    if not isinstance(v, str) or not v.strip():
        raise ValueError("must be a non-empty string")
    return v


def _loopback_url(v):
    try:
        u = urlsplit(v)
        port = u.port
    except (ValueError, TypeError, AttributeError):
        raise ValueError("must be an http URL") from None
    if (u.scheme != "http" or u.hostname not in LOOPBACK or port is None or u.username
            or u.password or u.path not in ("", "/") or u.query or u.fragment):
        raise ValueError("must be http://<loopback host>:<port> with no path")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"http://{host}:{port}"


def _optional(check):
    return lambda v: None if v is None else check(v)


CONTROL_PLANE = {"port": (_int(1, 65535), 8080), "db": (_string, "aiops.db"),
                 "state_file": (_string, "aiops.state.json"), "interval": (_num(0), 5.0)}

# The independent watchdog's tuning. There is deliberately NO action or command key: the
# watchdog's one action (terminate the protected process) is fixed in code.
WATCHDOG = {"interval": (_num(0), 1.0), "term_grace_seconds": (_num(0), 20),
            "max_sensor_failures": (_int(1, 10), 3)}

SCHEMAS = {
    "docker-real-gpu": {
        "provider": "docker",
        "control_plane": CONTROL_PLANE,
        "workload": {"name": (_regex(DOCKER_NAME, "workload name"), REQUIRED),
                     "vllm_url": (_loopback_url, REQUIRED), "model": (_string, REQUIRED)},
        "gpu": {"index": (_int(0, 64), 0),
                "uuid": (_optional(_regex(GPU_UUID, "GPU uuid")), None),
                "min_memory_mib": (_optional(_int(1, 10**7)), None)},
        "safety": {"gpu_memory_threshold_bytes": (_int(1, 10**13), REQUIRED),
                   "max_gpu_memory_percent": (_int(1, 100), 85),
                   "max_temperature_c": (_int(1, 120), 80),
                   "max_ram_percent": (_int(1, 100), 90),
                   "max_runtime_seconds": (_int(1, 10**7), 86400)},
        "verification": {"timeout": (_num(0), 60), "interval": (_num(0), 2.0),
                         "stable_probes": (_int(1, 20), 3),
                         "probe_interval": (_num(0, strict=False), 1.0)},
        "watchdog": WATCHDOG,
    },
    "kubernetes": {
        "provider": "kubernetes",
        "control_plane": CONTROL_PLANE,
        "workload": {"name": (_regex(K8S_NAME, "workload name"), REQUIRED),
                     "namespace": (_regex(DNS_LABEL, "namespace"), "default"),
                     "context": (_regex(CONTEXT, "kubectl context"), REQUIRED),
                     "prometheus_url": (_loopback_url, REQUIRED)},
        "safety": {"gpu_memory_threshold_bytes": (_int(1, 10**13), REQUIRED),
                   "error_rate_limit": (_fraction, 0.05),
                   "max_ram_percent": (_int(1, 100), 90),
                   "max_runtime_seconds": (_int(1, 10**7), 86400)},
        "verification": {"timeout": (_num(0), 60), "interval": (_num(0), 2.0)},
        "watchdog": WATCHDOG,
    },
}


def load_profile(path):
    path = Path(path)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ProfileError(f"cannot read profile {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"invalid TOML in {path}: {e}") from e

    name = raw.get("profile")
    if name not in SCHEMAS:
        raise ProfileError(f"unknown profile {name!r}; expected one of {sorted(SCHEMAS)}")
    schema = SCHEMAS[name]
    sections = [k for k in schema if k != "provider"]
    for key in raw:
        if key != "profile" and key not in sections:
            raise ProfileError(f"unknown section [{key}] for profile {name!r}")

    profile = {"name": name, "provider": schema["provider"], "config_path": str(path.resolve())}
    for section in sections:
        values = raw.get(section, {})
        if not isinstance(values, dict):
            raise ProfileError(f"[{section}] must be a table")
        spec = schema[section]
        for key in values:
            if key not in spec:
                raise ProfileError(f"unknown key {key!r} in [{section}]")
        out = {}
        for key, (check, default) in spec.items():
            if key in values:
                try:
                    out[key] = check(values[key])
                except ValueError as e:
                    raise ProfileError(f"[{section}] {key}: {e}") from e
            elif default is REQUIRED:
                raise ProfileError(f"[{section}] {key} is required")
            else:
                out[key] = default
        profile[section] = out

    base = path.resolve().parent
    for key in ("db", "state_file"):
        p = Path(profile["control_plane"][key])
        profile["control_plane"][key] = str(p if p.is_absolute() else base / p)
    return profile
