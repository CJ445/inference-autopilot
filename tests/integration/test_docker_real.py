"""DockerProvider against the real Docker daemon (no GPU). Opt in: AIOPS_INTEGRATION=1."""
import json
import os
import subprocess

import pytest

from aiops.docker import MANAGED_LABEL, WORKLOAD_LABEL, DockerProvider
from aiops.kubectl import ClusterError

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_INTEGRATION") != "1", reason="set AIOPS_INTEGRATION=1 to run")

IMAGE = "python:3.12-slim"
MANAGED = "aiops-test-managed"
UNMANAGED = "aiops-test-unmanaged"


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


def started_at(container):
    return json.loads(docker("inspect", container))[0]["State"]["StartedAt"]


@pytest.fixture
def containers():
    for name in (MANAGED, UNMANAGED):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    try:
        docker("run", "-d", "--name", MANAGED, "--label", f"{MANAGED_LABEL}=true",
               "--label", f"{WORKLOAD_LABEL}={MANAGED}", IMAGE, "sleep", "120")
        docker("run", "-d", "--name", UNMANAGED, IMAGE, "sleep", "120")  # no project labels
        yield
    finally:
        for name in (MANAGED, UNMANAGED):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_real_restart_keeps_container_id_but_changes_lifecycle_identity(containers):
    provider = DockerProvider()
    before = provider.get_workload(MANAGED)
    assert before["ready"] is True

    assert provider.restart_workload(MANAGED) == "ok"

    after = provider.get_workload(MANAGED)
    assert after["ready"] is True
    assert before["id"].split(":")[0] == after["id"].split(":")[0]  # same container ID
    assert before["id"] != after["id"]                              # but a genuinely new run


def test_real_unlabeled_container_is_refused_and_not_restarted(containers):
    before = started_at(UNMANAGED)
    with pytest.raises(ClusterError, match="no managed workload"):
        DockerProvider().restart_workload(UNMANAGED)
    assert started_at(UNMANAGED) == before


def test_real_unknown_workload_fails_closed(containers):
    with pytest.raises(ClusterError, match="no managed workload"):
        DockerProvider().get_workload("aiops-test-does-not-exist")
