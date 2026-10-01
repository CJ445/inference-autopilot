import json
import subprocess

import pytest

from aiops.docker import DockerProvider
from aiops.kubectl import ClusterError

MANAGED = {"com.inference-autopilot.managed": "true", "com.inference-autopilot.workload": "vllm"}
SHORT = "0123456789ab"                 # what `docker ps --format {{.ID}}` prints
FULL = SHORT + "c" * 52                # what `docker inspect` reports as Id (64 hex)
PS = ["docker", "ps", "-a", "--filter", "label=com.inference-autopilot.managed=true",
      "--filter", "label=com.inference-autopilot.workload=vllm", "--format", "{{.ID}}"]


def inspect_json(cid=FULL, started="2026-10-01T10:00:00Z", running=True, health="healthy",
                 labels=MANAGED):
    state = {"Running": running, "Status": "running" if running else "exited",
             "StartedAt": started}
    if health:
        state["Health"] = {"Status": health}
    return json.dumps([{"Id": cid, "State": state, "Config": {"Labels": labels}}])


class Docker:
    """Records argv and answers from a table; fails the test on anything unexpected."""

    def __init__(self, ids=SHORT + "\n", inspect=None, fail=None):
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
            r.stdout = FULL + "\n"
        else:
            raise AssertionError(f"unexpected docker command: {argv}")
        return r

    def verbs(self):
        return [c[0][1] for c in self.calls]


def provider(d, workload="vllm"):
    return DockerProvider(workload, run=d)


def test_workload_is_resolved_by_project_labels_with_fixed_argv():
    d = Docker()
    provider(d).get_workload("vllm")
    assert d.calls[0][0] == PS
    assert not any(kw.get("shell") for _, kw in d.calls)


def test_lifecycle_id_combines_container_id_and_started_at():
    p = provider(Docker(inspect=inspect_json(cid=FULL, started="T1")))
    assert p.get_workload("vllm")["id"] == f"{FULL}:T1"


def test_docker_restart_keeps_container_id_so_started_at_must_change_the_identity():
    before = provider(Docker(inspect=inspect_json(started="T1"))).get_workload("vllm")
    after = provider(Docker(inspect=inspect_json(started="T2"))).get_workload("vllm")
    assert before["id"] != after["id"]


@pytest.mark.parametrize("kwargs, ready", [
    ({"running": True, "health": "healthy"}, True),
    ({"running": True, "health": None}, True),          # no healthcheck defined
    ({"running": True, "health": "starting"}, False),
    ({"running": True, "health": "unhealthy"}, False),
    ({"running": False, "health": None}, False),
])
def test_ready_requires_running_and_healthy_when_a_healthcheck_exists(kwargs, ready):
    assert provider(Docker(inspect=inspect_json(**kwargs))).get_workload("vllm")[
        "ready"] is ready


def test_restart_targets_the_full_container_id_with_one_fixed_docker_restart():
    d = Docker()
    assert provider(d).restart_workload("vllm") == "ok"
    restart = [c for c, _ in d.calls if c[1] == "restart"]
    assert restart == [["docker", "restart", "-t", "30", FULL]]


def test_unmanaged_workload_is_rejected_and_nothing_is_restarted():
    d = Docker(ids="")
    with pytest.raises(ClusterError, match="no managed workload"):
        provider(d).restart_workload("vllm")
    assert "restart" not in d.verbs()


def test_ambiguous_workload_is_rejected_and_nothing_is_restarted():
    d = Docker(ids=SHORT + "\n" + "ba9876543210\n")
    with pytest.raises(ClusterError, match="ambiguous"):
        provider(d).restart_workload("vllm")
    assert "restart" not in d.verbs()


@pytest.mark.parametrize("labels", [
    {},                                                                    # unlabeled
    {"com.inference-autopilot.managed": "false", "com.inference-autopilot.workload": "vllm"},
    {"com.inference-autopilot.managed": "true", "com.inference-autopilot.workload": "other"},
])
def test_labels_are_rechecked_after_inspect_and_a_mismatch_fails_closed(labels):
    d = Docker(inspect=inspect_json(labels=labels))
    with pytest.raises(ClusterError, match="labels"):
        provider(d).restart_workload("vllm")
    assert "restart" not in d.verbs()


@pytest.mark.parametrize("bad", ["not json", "[]", "[{}]", json.dumps([{"Id": FULL}])])
def test_unexpected_inspect_output_fails_closed(bad):
    d = Docker(inspect=bad)
    with pytest.raises(ClusterError):
        provider(d).restart_workload("vllm")
    assert "restart" not in d.verbs()


@pytest.mark.parametrize("error", [
    FileNotFoundError("docker"),
    subprocess.CalledProcessError(1, ["docker"], stderr="Cannot connect to the Docker daemon"),
    subprocess.TimeoutExpired(["docker"], 10),
])
def test_docker_unavailable_fails_closed(error):
    with pytest.raises(ClusterError):
        provider(Docker(fail=error)).get_workload("vllm")
    with pytest.raises(ClusterError):
        provider(Docker(fail=error)).restart_workload("vllm")


@pytest.mark.parametrize("name", ["vllm; rm -rf /", "$(reboot)", "a b", "", "VLLM", "x" * 100,
                                  "-rf", "vllm\nvllm"])
def test_invalid_workload_names_never_reach_docker(name):
    d = Docker()
    with pytest.raises(ClusterError):
        provider(d).restart_workload(name)
    assert d.calls == []


def test_provider_exposes_no_generic_docker_interface():
    public = {n for n in dir(DockerProvider) if not n.startswith("_")}
    assert public == {"get_workload", "restart_workload"}


# --- identity hardening: only the explicitly configured workload, only well-formed IDs ----

def test_provider_is_bound_to_one_workload_and_refuses_any_other_name_without_docker_calls():
    d = Docker()
    p = provider(d, workload="vllm")
    for call in (p.get_workload, p.restart_workload):
        with pytest.raises(ClusterError, match="not the configured workload"):
            call("other")           # a valid name, even if a managed "other" container exists
    assert d.calls == []


@pytest.mark.parametrize("workload", ["", "VLLM", "vllm; reboot", "-x", None])
def test_provider_cannot_be_constructed_without_a_valid_workload(workload):
    with pytest.raises((ClusterError, TypeError)):
        DockerProvider(workload, run=Docker())


@pytest.mark.parametrize("ps_output", [
    "--privileged\n", "abc;rm\n", "0123456789AB\n", "0123\n", "0123456789ab extra\n",
    "-rf0123456789\n",
])
def test_malformed_container_ids_from_docker_fail_closed(ps_output):
    d = Docker(ids=ps_output)
    with pytest.raises(ClusterError):
        provider(d).restart_workload("vllm")
    assert d.verbs().count("restart") == 0 and "inspect" not in d.verbs()


@pytest.mark.parametrize("full_id", ["f" * 64, "tooshort", SHORT, FULL.upper(), FULL + "0"])
def test_inspect_reporting_a_different_or_malformed_container_fails_closed(full_id):
    d = Docker(inspect=inspect_json(cid=full_id))
    with pytest.raises(ClusterError, match="identity"):
        provider(d).restart_workload("vllm")
    assert "restart" not in d.verbs()


def test_a_stopped_workload_is_never_started_by_a_restart():
    d = Docker(inspect=inspect_json(running=False, health=None))
    with pytest.raises(ClusterError, match="not running"):
        provider(d).restart_workload("vllm")
    assert all(argv[1] != "restart" for argv, _ in d.calls)       # `docker restart` would start it


def test_a_paused_workload_is_still_restartable():
    paused = json.loads(inspect_json())
    paused[0]["State"]["Paused"] = True                            # Running stays true while paused
    d = Docker(inspect=json.dumps(paused))
    assert provider(d).restart_workload("vllm") == "ok"
    assert any(argv[1] == "restart" for argv, _ in d.calls)
