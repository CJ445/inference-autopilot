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
    provider = DockerProvider(MANAGED)
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
        DockerProvider(UNMANAGED).restart_workload(UNMANAGED)   # bound to it: labels must refuse
    assert started_at(UNMANAGED) == before


def test_real_unknown_workload_fails_closed(containers):
    with pytest.raises(ClusterError, match="no managed workload"):
        DockerProvider("aiops-test-does-not-exist").get_workload("aiops-test-does-not-exist")


def test_real_provider_is_bound_to_its_workload_even_if_another_managed_one_exists(containers):
    other = "aiops-test-other-wl"
    docker("run", "-d", "--name", "aiops-test-other", "--label", f"{MANAGED_LABEL}=true",
           "--label", f"{WORKLOAD_LABEL}={other}", IMAGE, "sleep", "120")
    try:
        before = started_at("aiops-test-other")
        provider = DockerProvider(MANAGED)
        for call in (provider.get_workload, provider.restart_workload):
            with pytest.raises(ClusterError, match="not the configured workload"):
                call(other)
        assert started_at("aiops-test-other") == before
    finally:
        subprocess.run(["docker", "rm", "-f", "aiops-test-other"], capture_output=True)


@pytest.mark.parametrize("duplicate_state", ["running", "stopped"])
def test_real_duplicate_labeled_containers_are_ambiguous_and_neither_is_restarted(
        containers, duplicate_state):
    dup = "aiops-test-dup"
    subprocess.run(["docker", "rm", "-f", dup], capture_output=True)
    docker("run", "-d", "--name", dup, "--label", f"{MANAGED_LABEL}=true",
           "--label", f"{WORKLOAD_LABEL}={MANAGED}", IMAGE, "sleep", "120")
    if duplicate_state == "stopped":
        docker("kill", dup)   # a leftover stopped container still counts: `docker ps -a`
    try:
        before = {name: started_at(name) for name in (MANAGED, dup)}
        with pytest.raises(ClusterError, match="ambiguous"):
            DockerProvider(MANAGED).restart_workload(MANAGED)
        with pytest.raises(ClusterError, match="ambiguous"):
            DockerProvider(MANAGED).get_workload(MANAGED)
        assert {name: started_at(name) for name in (MANAGED, dup)} == before
    finally:
        subprocess.run(["docker", "rm", "-f", dup], capture_output=True)
