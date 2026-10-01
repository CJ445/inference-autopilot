from pathlib import Path

import pytest

from aiops.profile import ProfileError, load_profile

DOCKER = """
profile = "docker-real-gpu"

[control_plane]
port = 8123
db = "data/aiops.db"
state_file = "run/aiops.state.json"
interval = 2

[workload]
name = "vllm"
vllm_url = "http://127.0.0.1:8001"
model = "facebook/opt-125m"

[gpu]
index = 0
uuid = "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f"
min_memory_mib = 6000

[safety]
gpu_memory_threshold_bytes = 4500000000

[verification]
timeout = 150
"""

KUBERNETES = """
profile = "kubernetes"

[workload]
name = "vllm-0"
context = "kind-aiops-test"
prometheus_url = "http://127.0.0.1:19090"

[safety]
gpu_memory_threshold_bytes = 7500000000
"""


def write(tmp_path, text, name="aiops.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_docker_profile_loads_with_defaults_and_resolves_paths_against_the_config_dir(tmp_path):
    p = load_profile(write(tmp_path, DOCKER))
    assert p["name"] == "docker-real-gpu" and p["provider"] == "docker"
    assert p["control_plane"]["port"] == 8123
    assert p["control_plane"]["db"] == str(tmp_path / "data" / "aiops.db")
    assert p["control_plane"]["state_file"] == str(tmp_path / "run" / "aiops.state.json")
    assert p["workload"]["name"] == "vllm" and p["workload"]["model"] == "facebook/opt-125m"
    assert p["gpu"] == {"index": 0, "uuid": "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f",
                        "min_memory_mib": 6000}
    s = p["safety"]
    assert (s["gpu_memory_threshold_bytes"], s["max_gpu_memory_percent"],
            s["max_temperature_c"], s["max_ram_percent"]) == (4_500_000_000, 85, 80, 90)
    assert p["verification"] == {"timeout": 150, "interval": 2.0, "stable_probes": 3,
                                 "probe_interval": 1.0}


def test_kubernetes_profile_loads_and_keeps_its_own_provider(tmp_path):
    p = load_profile(write(tmp_path, KUBERNETES))
    assert p["name"] == "kubernetes" and p["provider"] == "kubernetes"
    assert p["workload"] == {"name": "vllm-0", "namespace": "default",
                             "context": "kind-aiops-test",
                             "prometheus_url": "http://127.0.0.1:19090"}
    assert p["safety"]["error_rate_limit"] == 0.05
    assert p["control_plane"]["port"] == 8080


def test_optional_gpu_expectations_default_to_unpinned(tmp_path):
    text = DOCKER.replace('uuid = "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f"\n', "") \
                 .replace("min_memory_mib = 6000\n", "")
    assert load_profile(write(tmp_path, text))["gpu"] == {"index": 0, "uuid": None,
                                                          "min_memory_mib": None}


@pytest.mark.parametrize("edit, why", [
    (lambda t: t.replace('profile = "docker-real-gpu"', 'profile = "fake-gpu"'), "unknown profile"),
    (lambda t: t + '\n[surprise]\nx = 1\n', "unknown section"),
    (lambda t: t.replace("port = 8123", "port = 8123\nbogus = 1"), "unknown key"),
    (lambda t: t.replace('name = "vllm"', 'name = "vllm; rm -rf /"'), "bad workload name"),
    (lambda t: t.replace('name = "vllm"', 'name = "VLLM"'), "uppercase workload"),
    (lambda t: t.replace('name = "vllm"\n', ""), "missing workload name"),
    (lambda t: t.replace("gpu_memory_threshold_bytes = 4500000000", ""), "missing threshold"),
    (lambda t: t.replace("port = 8123", "port = 0"), "port too low"),
    (lambda t: t.replace("port = 8123", "port = 70000"), "port too high"),
    (lambda t: t.replace("port = 8123", 'port = "8123"'), "port wrong type"),
    (lambda t: t.replace("timeout = 150", "timeout = 0"), "timeout not positive"),
    (lambda t: t.replace("[verification]", "[verification]\nstable_probes = 0"), "no stable window"),
    (lambda t: t.replace("[safety]", "[safety]\nmax_gpu_memory_percent = 101"), "percent > 100"),
    (lambda t: t.replace("[safety]", "[safety]\nmax_temperature_c = 0"), "temperature 0"),
    (lambda t: t.replace('uuid = "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f"', 'uuid = "abc"'),
     "uuid not a GPU uuid"),
    (lambda t: t.replace("[gpu]", '[workload2]\nname = "x"\n[gpu]'), "unknown section 2"),
    (lambda t: t.replace("[gpu]", '[gpu]\nnamespace = "kube-system"'), "k8s key in gpu"),
])
def test_invalid_docker_profiles_are_rejected(tmp_path, edit, why):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, edit(DOCKER)))


@pytest.mark.parametrize("url", [
    "http://10.0.0.5:8000", "http://example.com:8000", "https://127.0.0.1:8001",
    "http://127.0.0.1", "ftp://127.0.0.1:8001", "127.0.0.1:8001", "http://0.0.0.0:8001",
    "http://127.0.0.1:8001/v1", "http://user@127.0.0.1:8001",
])
def test_the_vllm_endpoint_must_be_a_loopback_http_url_with_a_port(tmp_path, url):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, DOCKER.replace("http://127.0.0.1:8001", url)))


@pytest.mark.parametrize("url", ["http://127.0.0.1:8001", "http://localhost:9000"])
def test_loopback_endpoints_are_accepted(tmp_path, url):
    p = load_profile(write(tmp_path, DOCKER.replace("http://127.0.0.1:8001", url)))
    assert p["workload"]["vllm_url"] == url


@pytest.mark.parametrize("edit", [
    lambda t: t.replace('context = "kind-aiops-test"\n', ""),            # context is mandatory
    lambda t: t.replace('prometheus_url = "http://127.0.0.1:19090"\n', ""),
    lambda t: t.replace('name = "vllm-0"', 'name = "Vllm 0"'),
    lambda t: t.replace("[safety]", '[gpu]\nindex = 0\n[safety]'),         # docker-only section
    lambda t: t.replace('name = "vllm-0"', 'name = "vllm-0"\nvllm_url = "http://127.0.0.1:1"'),
])
def test_invalid_kubernetes_profiles_are_rejected(tmp_path, edit):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, edit(KUBERNETES)))


def test_missing_and_malformed_files_are_rejected(tmp_path):
    with pytest.raises(ProfileError, match="cannot read"):
        load_profile(tmp_path / "nope.toml")
    with pytest.raises(ProfileError, match="TOML"):
        load_profile(write(tmp_path, "this is = = not toml ["))


def test_the_shipped_example_profiles_are_valid():
    root = Path(__file__).parent.parent / "deploy" / "profiles"
    assert load_profile(root / "docker-real-gpu.toml")["provider"] == "docker"
    assert load_profile(root / "kubernetes.toml")["provider"] == "kubernetes"


def test_there_is_no_way_to_name_a_second_workload_or_container(tmp_path):
    text = DOCKER.replace('name = "vllm"', 'name = "vllm"\ncontainers = ["a", "b"]')
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, text))


# --- watchdog budgets and tuning (Slice 7) -------------------------------------------------

def test_every_profile_has_a_bounded_runtime_budget_and_watchdog_defaults(tmp_path):
    for text, name in ((DOCKER, "d.toml"), (KUBERNETES, "k.toml")):
        p = load_profile(write(tmp_path, text, name=name))
        assert p["safety"]["max_runtime_seconds"] == 86400          # finite by default
        assert p["watchdog"] == {"interval": 1.0, "term_grace_seconds": 20,
                                 "max_sensor_failures": 3}


def test_the_kubernetes_profile_gets_a_host_ram_budget(tmp_path):
    assert load_profile(write(tmp_path, KUBERNETES))["safety"]["max_ram_percent"] == 90


def test_budgets_and_watchdog_tuning_can_be_configured(tmp_path):
    text = DOCKER.replace("[safety]", "[safety]\nmax_runtime_seconds = 600") \
                 + "\n[watchdog]\ninterval = 0.5\nterm_grace_seconds = 5\nmax_sensor_failures = 1\n"
    p = load_profile(write(tmp_path, text))
    assert p["safety"]["max_runtime_seconds"] == 600
    assert p["watchdog"] == {"interval": 0.5, "term_grace_seconds": 5, "max_sensor_failures": 1}


@pytest.mark.parametrize("edit", [
    lambda t: t.replace("[safety]", "[safety]\nmax_runtime_seconds = 0"),
    lambda t: t.replace("[safety]", "[safety]\nmax_runtime_seconds = -5"),
    lambda t: t.replace("[safety]", '[safety]\nmax_runtime_seconds = "600"'),
    lambda t: t + "\n[watchdog]\ninterval = 0\n",
    lambda t: t + "\n[watchdog]\nterm_grace_seconds = 0\n",
    lambda t: t + "\n[watchdog]\nmax_sensor_failures = 0\n",
    lambda t: t + "\n[watchdog]\nmax_sensor_failures = 11\n",
    lambda t: t + "\n[watchdog]\nmax_sensor_failures = 2.5\n",
    lambda t: t + "\n[watchdog]\naction = \"restart\"\n",            # no action is configurable
    lambda t: t + "\n[watchdog]\ncommand = \"reboot\"\n",
])
def test_invalid_watchdog_configuration_is_rejected(tmp_path, edit):
    with pytest.raises(ProfileError):
        load_profile(write(tmp_path, edit(DOCKER)))
