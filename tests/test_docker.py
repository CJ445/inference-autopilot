import json
import subprocess

import pytest

from aiops.docker import DockerProvider
from aiops.kubectl import ClusterError

MANAGED = {"com.inference-autopilot.managed": "true", "com.inference-autopilot.workload": "vllm"}
PS = ["docker", "ps", "-a", "--filter", "label=com.inference-autopilot.managed=true",
      "--filter", "label=com.inference-autopilot.workload=vllm", "--format", "{{.ID}}"]


def inspect_json(cid="abc123", started="2026-10-01T10:00:00Z", running=True, health="healthy",
                 labels=MANAGED):
    state = {"Running": running, "Status": "running" if running else "exited",
             "StartedAt": started}
    if health:
        state["Health"] = {"Status": health}
    return json.dumps([{"Id": cid, "State": state, "Config": {"Labels": labels}}])


class Docker:
    """Records argv and answers from a table; fails the test on anything unexpected."""

    def __init__(self, ids="abc123\n", inspect=None, fail=None):
        self.calls, self.ids = [], ids
        self.inspect = inspect if inspect is not None else inspect_json()
        self.fail = fail

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.fail:
            raise self.fail

        class R:
            returncode = 0
        r = R()
        if argv[:2] == ["docker", "ps"]:
            r.stdout = self.ids
        elif argv[:2] == ["docker", "inspect"]:
            r.stdout = self.inspect
        elif argv[:2] == ["docker", "restart"]:
            r.stdout = "abc123\n"
        else:
            raise AssertionError(f"unexpected docker command: {argv}")
        return r

    def verbs(self):
        return [c[0][1] for c in self.calls]


def test_workload_is_resolved_by_project_labels_with_fixed_argv():
    d = Docker()
    DockerProvider(run=d).get_workload("vllm")
    assert d.calls[0][0] == PS
    assert not any(kw.get("shell") for _, kw in d.calls)


def test_lifecycle_id_combines_container_id_and_started_at():
    p = DockerProvider(run=Docker(inspect=inspect_json(cid="abc123", started="T1")))
    assert p.get_workload("vllm")["id"] == "abc123:T1"


def test_docker_restart_keeps_container_id_so_started_at_must_change_the_identity():
    before = DockerProvider(run=Docker(inspect=inspect_json(started="T1"))).get_workload("vllm")
    after = DockerProvider(run=Docker(inspect=inspect_json(started="T2"))).get_workload("vllm")
    assert before["id"] != after["id"]


@pytest.mark.parametrize("kwargs, ready", [
    ({"running": True, "health": "healthy"}, True),
    ({"running": True, "health": None}, True),          # no healthcheck defined
    ({"running": True, "health": "starting"}, False),
    ({"running": True, "health": "unhealthy"}, False),
    ({"running": False, "health": None}, False),
])
def test_ready_requires_running_and_healthy_when_a_healthcheck_exists(kwargs, ready):
    assert DockerProvider(run=Docker(inspect=inspect_json(**kwargs))).get_workload("vllm")[
        "ready"] is ready


def test_restart_is_a_single_fixed_docker_restart_of_the_resolved_container():
    d = Docker()
    assert DockerProvider(run=d).restart_workload("vllm") == "ok"
    restart = [c for c, _ in d.calls if c[1] == "restart"]
    assert restart == [["docker", "restart", "-t", "30", "abc123"]]


def test_unmanaged_workload_is_rejected_and_nothing_is_restarted():
    d = Docker(ids="")
    with pytest.raises(ClusterError, match="no managed workload"):
        DockerProvider(run=d).restart_workload("vllm")
    assert "restart" not in d.verbs()


def test_ambiguous_workload_is_rejected_and_nothing_is_restarted():
    d = Docker(ids="abc123\ndef456\n")
    with pytest.raises(ClusterError, match="ambiguous"):
        DockerProvider(run=d).restart_workload("vllm")
    assert "restart" not in d.verbs()


@pytest.mark.parametrize("labels", [
    {},                                                                    # unlabeled
    {"com.inference-autopilot.managed": "false", "com.inference-autopilot.workload": "vllm"},
    {"com.inference-autopilot.managed": "true", "com.inference-autopilot.workload": "other"},
])
def test_labels_are_rechecked_after_inspect_and_a_mismatch_fails_closed(labels):
    d = Docker(inspect=inspect_json(labels=labels))
    with pytest.raises(ClusterError, match="labels"):
        DockerProvider(run=d).restart_workload("vllm")
    assert "restart" not in d.verbs()


@pytest.mark.parametrize("bad", ["not json", "[]", "[{}]", json.dumps([{"Id": "x"}])])
def test_unexpected_inspect_output_fails_closed(bad):
    d = Docker(inspect=bad)
    with pytest.raises(ClusterError):
        DockerProvider(run=d).restart_workload("vllm")
    assert "restart" not in d.verbs()


@pytest.mark.parametrize("error", [
    FileNotFoundError("docker"),
    subprocess.CalledProcessError(1, ["docker"], stderr="Cannot connect to the Docker daemon"),
    subprocess.TimeoutExpired(["docker"], 10),
])
def test_docker_unavailable_fails_closed(error):
    with pytest.raises(ClusterError):
        DockerProvider(run=Docker(fail=error)).get_workload("vllm")
    with pytest.raises(ClusterError):
        DockerProvider(run=Docker(fail=error)).restart_workload("vllm")


@pytest.mark.parametrize("name", ["vllm; rm -rf /", "$(reboot)", "a b", "", "VLLM", "x" * 100,
                                  "-rf", "vllm\nvllm"])
def test_invalid_workload_names_never_reach_docker(name):
    d = Docker()
    with pytest.raises(ClusterError):
        DockerProvider(run=d).restart_workload(name)
    assert d.calls == []


def test_provider_exposes_no_generic_docker_interface():
    public = {n for n in dir(DockerProvider) if not n.startswith("_")}
    assert public == {"get_workload", "restart_workload"}
