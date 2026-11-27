"""Real-Docker acceptance checks for the Runner's isolation boundary.

Only short-lived, unprivileged sibling containers created by ``Sandbox`` are used.
Docker/image absence is an explicit skip in the offline lane; no fake daemon attrs
are substituted for this acceptance evidence.
"""

import json
import threading
from uuid import uuid4

import pytest
from reproagent_runner.config import RunnerSettings
from reproagent_runner.sandbox import Sandbox, cleanup_run_dir, make_client
from reproagent_schemas import CodeFile, ErrorCategory, ResourceBudget, RunRequest, RunStatus


@pytest.fixture(scope="module")
def docker_sandbox(tmp_path_factory):
    work_dir = tmp_path_factory.mktemp("runner-real-sandbox")
    settings = RunnerSettings(work_dir=work_dir, _env_file=None)
    try:
        client = make_client(settings)
        client.ping()
        client.images.get(settings.exec_image)
    except Exception as exc:  # noqa: BLE001 - external acceptance dependency
        pytest.skip(f"real Docker sandbox unavailable: {exc}")
    return client, Sandbox(client, settings), settings


def request_for(code: str, budget: ResourceBudget) -> RunRequest:
    return RunRequest(
        experiment_id=uuid4(),
        experiment_plan_id=uuid4(),
        code_files=[CodeFile(path="inspect.py", content=code)],
        entrypoint="inspect.py",
        dependencies=[],
        resource_budget=budget,
    )


def execute(client, sandbox, settings, request, observe=None):
    run_dir = settings.work_dir / str(uuid4())
    captured = []

    def on_container(container_id):
        captured.append(container_id)
        if observe is not None:
            observe(client.containers.get(container_id).attrs, run_dir)

    try:
        result = sandbox.execute(request, run_dir, threading.Event(), on_container=on_container)
        assert len(captured) == 1
        gone_ids = {container.id for container in client.containers.list(all=True)}
        assert captured[0] not in gone_ids
        return result
    finally:
        cleanup_run_dir(run_dir)


def test_real_container_has_all_security_and_resource_parameters(docker_sandbox):
    client, sandbox, settings = docker_sandbox
    budget = ResourceBudget(max_seconds=15, memory_mb=128, cpu_count=1, pids_limit=64, disk_mb=32)
    observed = {}

    def observe(attrs, run_dir):
        observed.update(attrs)
        observed["run_dir"] = run_dir

    code = (
        "import json, os, socket\n"
        "blocked = False\n"
        "try:\n"
        "    socket.create_connection(('192.0.2.1', 80), timeout=1)\n"
        "except OSError:\n"
        "    blocked = True\n"
        "print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), "
        "'rootfs_writable': os.access('/', os.W_OK), "
        "'network_blocked': blocked, "
        "'environment_names': sorted(os.environ), "
        "'docker_socket': os.path.exists('/var/run/docker.sock')}))\n"
        "open('/output/metrics.json', 'w').write(json.dumps({"
        "'schema_version':'1.0','target_metric':'accuracy','metric_value':1.0,"
        "'accuracy':1.0,'epochs_completed':0,'train_samples':0,"
        "'test_samples':0,'seed':42,'runtime_seconds':0.0}))\n"
    )
    result = execute(client, sandbox, settings, request_for(code, budget), observe)
    assert result.status is RunStatus.SUCCEEDED, result.stderr
    inside = json.loads(result.stdout)
    host = observed["HostConfig"]
    config = observed["Config"]

    assert host["NetworkMode"] == "none"
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["ReadonlyRootfs"] is True
    assert host["Memory"] == budget.memory_mb * 1024 * 1024
    assert host["MemorySwap"] == host["Memory"]
    assert host["NanoCpus"] == budget.cpu_count * 1_000_000_000
    assert host["PidsLimit"] == budget.pids_limit
    assert host["LogConfig"]["Config"] == {"max-size": "1m", "max-file": "1"}
    assert host["Tmpfs"]["/tmp"] == "size=64m,mode=1777"
    assert host["StorageOpt"]["size"] == f"{budget.disk_mb}m"
    assert config["User"] == "1000:1000"
    assert inside["uid"] != 0 and inside["gid"] != 0
    assert inside["rootfs_writable"] is False
    assert inside["docker_socket"] is False
    assert inside["network_blocked"] is True
    assert not {"QWEN_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SERVICE_ROLE"} & set(
        inside["environment_names"]
    )
    binds = host["Binds"]
    # Docker preserves Windows drive-letter colons in the host source. Only the
    # container target and mode are unambiguous trailing fields.
    assert sorted(bind.rsplit(":", 2)[-2:] for bind in binds) == [
        ["/output", "rw"],
        ["/work/code", "ro"],
    ]
    assert {bind.rsplit(":", 2)[0] for bind in binds} == {
        str(observed["run_dir"] / "code"),
        str(observed["run_dir"] / "output"),
    }
    assert not any("docker.sock" in bind for bind in binds)
    assert all(not bind.startswith(("/root/", "/home/", "/etc/")) for bind in binds)
    assert not any(
        key in config["Env"]
        for key in (
            "QWEN_API_KEY=",
            "SUPABASE_SERVICE_ROLE_KEY=",
            "AWS_SECRET_ACCESS_KEY=",
        )
    )


def test_real_hard_timeout_kills_and_removes_container(docker_sandbox):
    client, sandbox, settings = docker_sandbox
    result = execute(
        client,
        sandbox,
        settings,
        request_for("import time\ntime.sleep(30)\n", ResourceBudget(max_seconds=1)),
    )
    assert result.status is RunStatus.TIMEOUT
    assert result.error_category is ErrorCategory.TIMEOUT
    assert result.execution_time < 10
