import json

from aiops.kubectl import KubernetesProvider


class Recorder:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))

        class R:
            returncode, stdout = 0, self.stdout
        return R()


def test_restart_pod_is_a_fixed_argv_without_shell():
    run = Recorder()
    KubernetesProvider(namespace="vllm", run=run).restart_workload("vllm-0")
    argv, kw = run.calls[0]
    assert argv == ["kubectl", "-n", "vllm", "delete", "pod", "vllm-0", "--wait=true"]
    assert not kw.get("shell")


def test_get_workload_reads_lifecycle_id_and_readiness_from_the_api():
    pod = {"metadata": {"uid": "abc"},
           "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    run = Recorder(json.dumps(pod))
    state = KubernetesProvider(namespace="vllm", run=run).get_workload("vllm-0")
    assert state == {"id": "abc", "ready": True}
    assert run.calls[0][0] == ["kubectl", "-n", "vllm", "get", "pod", "vllm-0", "-o", "json"]


def test_metrics_delegate_to_the_telemetry_source():
    class Source:
        def metrics(self):
            return {"error_rate": 0.0}

    assert KubernetesProvider("vllm", metrics_source=Source()).metrics() == {"error_rate": 0.0}


def test_metrics_without_a_source_raise_rather_than_fabricate():
    import pytest
    with pytest.raises(RuntimeError):
        KubernetesProvider("vllm").metrics()


def test_context_pins_every_call_to_one_cluster():
    run = Recorder()
    KubernetesProvider("vllm", run=run, context="kind-aiops-test").restart_workload("vllm-0")
    assert run.calls[0][0][:3] == ["kubectl", "--context", "kind-aiops-test"]


def test_failed_kubectl_call_raises_typed_cluster_error():
    import subprocess

    import pytest

    from aiops.kubectl import ClusterError

    def failing(argv, **kw):
        raise subprocess.CalledProcessError(1, argv, stderr='pods "vllm-0" not found')

    with pytest.raises(ClusterError, match="not found"):
        KubernetesProvider("vllm", run=failing).get_workload("vllm-0")
