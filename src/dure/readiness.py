from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .command import Runner, SubprocessRunner
from .models import CheckResult, DeploymentPlan, NodeProfile


def validate_ray_snapshot(plan: DeploymentPlan, snapshot: dict) -> CheckResult:
    resources = snapshot.get("resources", {})
    gpu_count = float(resources.get("GPU", 0))
    if gpu_count != plan.world_size:
        return CheckResult(
            "ray-cluster",
            False,
            f"GPU resources must match exactly: {gpu_count:g}/{plan.world_size}",
        )

    expected_addresses = [item.node_address for item in plan.assignments]
    expected_resources = {f"dure_node_uuid:{item.node_id}" for item in plan.assignments}
    actual_resources = {
        key
        for item in snapshot.get("nodes", [])
        if item.get("alive") and float(item.get("gpu", 0)) > 0
        for key in item.get("resources", {})
        if str(key).startswith("dure_node_uuid:")
    }
    if actual_resources and actual_resources != expected_resources:
        return CheckResult(
            "ray-cluster",
            False,
            "Ray Dure node identity mismatch: "
            f"expected={sorted(expected_resources)}, actual={sorted(actual_resources)}",
        )
    if all(expected_addresses):
        actual_addresses = [
            str(item.get("address"))
            for item in snapshot.get("nodes", [])
            if item.get("alive") and float(item.get("gpu", 0)) > 0
        ]
        if sorted(actual_addresses) != sorted(str(item) for item in expected_addresses):
            return CheckResult(
                "ray-cluster",
                False,
                "Ray GPU membership mismatch: "
                f"expected={sorted(str(item) for item in expected_addresses)}, "
                f"actual={sorted(actual_addresses)}",
            )
    return CheckResult(
        "ray-cluster",
        True,
        f"Exact GPU resources and membership verified: {gpu_count:g}/{plan.world_size}",
    )


class ReadinessVerifier:
    def __init__(self, runner: Runner | None = None, engine: str = "docker") -> None:
        self.runner = runner or SubprocessRunner()
        self.engine = engine

    def host_gpu(self, profile: NodeProfile) -> CheckResult:
        if not profile.gpus:
            return CheckResult("host-gpu", False, "No NVIDIA GPU detected")
        result = self.runner.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,driver_version,memory.total",
                "--format=csv,noheader",
            ],
            timeout=15,
        )
        return CheckResult(
            "host-gpu",
            result.ok,
            result.stdout if result.ok else result.stderr or result.stdout,
        )

    def container_gpu(self, plan: DeploymentPlan) -> CheckResult:
        name = f"dure-ray-{plan.deployment_id}"
        code = (
            "import json,torch; "
            "assert torch.cuda.is_available(); "
            "x=torch.ones((256,256),device='cuda'); "
            "y=x@x; torch.cuda.synchronize(); "
            "print(json.dumps({'gpu':torch.cuda.get_device_name(0),'value':float(y[0,0])}))"
        )
        result = self.runner.run(
            [self.engine, "exec", name, "python3", "-c", code], timeout=60
        )
        return CheckResult(
            "container-gpu",
            result.ok,
            result.stdout if result.ok else result.stderr or result.stdout,
        )

    def ray_cluster(self, plan: DeploymentPlan) -> CheckResult:
        name = f"dure-ray-{plan.deployment_id}"
        code = (
            "import json,ray; "
            f"ray.init(address='{plan.ray_head_address}',logging_level='ERROR'); "
            "print(json.dumps({'resources':ray.cluster_resources(),'nodes':["
            "{'address':n.get('NodeManagerAddress'),'alive':n.get('Alive'),"
            "'gpu':n.get('Resources',{}).get('GPU',0),'resources':n.get('Resources',{})} "
            "for n in ray.nodes()]},"
            "sort_keys=True)); ray.shutdown()"
        )
        result = self.runner.run(
            [self.engine, "exec", name, "python3", "-c", code], timeout=45
        )
        if not result.ok:
            return CheckResult("ray-cluster", False, result.stderr or result.stdout)
        try:
            snapshot = json.loads(result.stdout.splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return CheckResult("ray-cluster", False, f"Invalid Ray resource response: {result.stdout}")
        return validate_ray_snapshot(plan, snapshot)

    def api(self, url: str = "http://127.0.0.1:8000") -> CheckResult:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=10) as response:
                if not 200 <= response.status < 300:
                    return CheckResult("vllm-api", False, f"HTTP {response.status} from /health")
            with urllib.request.urlopen(f"{url}/v1/models", timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
                models = payload.get("data", [])
                ok = 200 <= response.status < 300 and bool(models)
                detail = (
                    f"HTTP {response.status}; models="
                    f"{','.join(str(item.get('id', '?')) for item in models)}"
                )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return CheckResult("vllm-api", False, str(exc))
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            return CheckResult("vllm-api", False, f"Invalid /v1/models response: {exc}")
        return CheckResult("vllm-api", ok, detail)

    def wait_api(
        self,
        url: str = "http://127.0.0.1:8000",
        *,
        timeout: float = 600,
        interval: float = 5,
    ) -> CheckResult:
        deadline = time.monotonic() + timeout
        last = CheckResult("vllm-api", False, "API has not been checked")
        while time.monotonic() < deadline:
            last = self.api(url)
            if last.ok:
                return last
            time.sleep(interval)
        return CheckResult(
            "vllm-api",
            False,
            f"API was not ready within {timeout:g}s; last error: {last.detail}",
        )
