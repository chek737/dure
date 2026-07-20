from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

from .command import Runner, SubprocessRunner
from .models import (
    GPUProfile,
    InstalledModelProfile,
    NetworkProfile,
    NodeProfile,
    RuntimeProfile,
    WorkloadProfile,
)


DEFAULT_MODEL_ROOTS = (
    Path("/var/lib/dure/models"),
    Path.home() / ".cache" / "huggingface" / "hub",
)
MAX_DISCOVERED_MODELS = 100
MAX_ARTIFACT_FILES = 1000
LLM_RUNTIME_MARKERS = {
    "vllm": "vllm",
    "ollama": "ollama",
    "text-generation-inference": "tgi",
    "text_generation_inference": "tgi",
    "llama.cpp": "llama.cpp",
    "llama-cpp": "llama.cpp",
}


def _read_key_values(path: Path, separator: str = "=") -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if separator not in raw_line:
                continue
            key, raw_value = raw_line.split(separator, 1)
            values[key.strip()] = raw_value.strip().strip('"')
    except OSError:
        pass
    return values


def _memory_info(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^(\w+):\s+(\d+)\s+kB$", line)
            if match:
                values[match.group(1)] = int(match.group(2)) // 1024
    except OSError:
        pass
    return values


def _cpu_model(path: Path = Path("/proc/cpuinfo")) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


class NodeProbe:
    def __init__(
        self,
        runner: Runner | None = None,
        *,
        model_roots: tuple[Path, ...] | list[Path] | None = None,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        if model_roots is not None:
            roots = list(model_roots)
        else:
            roots = list(DEFAULT_MODEL_ROOTS)
            roots.extend(
                Path(item)
                for item in os.environ.get("DURE_MODEL_ROOTS", "").split(os.pathsep)
                if item
            )
        self.model_roots = tuple(dict.fromkeys(roots))

    def collect(self) -> NodeProfile:
        os_release = _read_key_values(Path("/etc/os-release"))
        memory = _memory_info()
        disk = shutil.disk_usage("/")
        hostname = socket.gethostname()
        issues: list[str] = []

        virtualization = None
        if self.runner.exists("systemd-detect-virt"):
            result = self.runner.run(["systemd-detect-virt"], timeout=3)
            if result.ok and result.stdout and result.stdout != "none":
                virtualization = result.stdout.splitlines()[0]

        gpus = self._probe_gpus(issues)
        runtime = self._probe_runtime()
        network = self._probe_network()
        installed_models = self._probe_models()
        workloads = self._probe_workloads(runtime)

        if not gpus:
            issues.append("No CUDA-capable NVIDIA GPU detected")
        if gpus and not runtime.nvidia_runtime:
            issues.append("NVIDIA container runtime was not detected")
        if "CUDA_VISIBLE_DEVICES" in os.environ and not os.environ["CUDA_VISIBLE_DEVICES"]:
            issues.append("CUDA_VISIBLE_DEVICES is explicitly set to an empty value")
        if memory.get("SwapTotal", 0) == 0:
            issues.append("Swap is disabled")

        return NodeProfile(
            node_id=hostname,
            hostname=hostname,
            os_name=os_release.get("PRETTY_NAME", platform.system()),
            os_version=os_release.get("VERSION_ID", "unknown"),
            kernel=platform.release(),
            architecture=platform.machine(),
            virtualization=virtualization,
            cpu_model=_cpu_model(),
            cpu_count=os.cpu_count() or 1,
            memory_mib=memory.get("MemTotal", 0),
            memory_available_mib=memory.get("MemAvailable", 0),
            swap_mib=memory.get("SwapTotal", 0),
            disk_total_mib=disk.total // (1024 * 1024),
            disk_free_mib=disk.free // (1024 * 1024),
            gpus=gpus,
            network=network,
            runtime=runtime,
            installed_models=installed_models,
            workloads=workloads,
            issues=issues,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _model_config(path: Path) -> dict:
        try:
            if path.stat().st_size > 1024 * 1024:
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _model_size_mib(self, path: Path) -> int | None:
        if not self.runner.exists("du"):
            return None
        result = self.runner.run(["du", "-sm", "--", str(path)], timeout=30)
        if not result.ok:
            return None
        try:
            return int(result.stdout.split()[0])
        except (IndexError, ValueError):
            return None

    def artifact_state(self, path: Path) -> dict[str, int | str | bool | None]:
        index_path = path / "model.safetensors.index.json"
        expected_paths: list[Path] = []
        verification = "metadata-only"
        invalid_index = False
        if index_path.is_file():
            try:
                if index_path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("model index is too large")
                value = json.loads(index_path.read_text(encoding="utf-8"))
                weight_map = value.get("weight_map", {}) if isinstance(value, dict) else {}
                names = sorted(set(weight_map.values())) if isinstance(weight_map, dict) else []
                relative_names = [Path(str(name)) for name in names]
                if len(relative_names) > MAX_ARTIFACT_FILES or any(
                    name.is_absolute() or ".." in name.parts for name in relative_names
                ):
                    raise ValueError("model index contains unsafe or excessive paths")
                expected_paths = [path / name for name in relative_names]
                verification = "index-read"
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                invalid_index = True
                verification = "invalid-index"
        if not expected_paths and not invalid_index:
            try:
                expected_paths = sorted(
                    (
                        item
                        for item in path.iterdir()
                        if item.is_file()
                        and (
                            item.name.endswith(".safetensors")
                            or item.name.endswith(".gguf")
                            or item.name == "pytorch_model.bin"
                        )
                    ),
                    key=lambda item: item.name,
                )[:MAX_ARTIFACT_FILES]
                if expected_paths:
                    verification = "file-read"
            except OSError:
                expected_paths = []

        present: list[Path] = []
        sizes: list[int] = []
        for item in expected_paths:
            if self.runner.exists("stat"):
                result = self.runner.run(
                    ["stat", "-Lc", "%s", "--", str(item)], timeout=3
                )
                if result.ok:
                    present.append(item)
                    try:
                        sizes.append(int(result.stdout.splitlines()[-1]))
                    except (IndexError, ValueError):
                        pass
                continue
            try:
                if item.is_file():
                    present.append(item)
                    sizes.append(item.stat().st_size)
            except OSError:
                pass
        logical_size_mib = sum(sizes) // (1024 * 1024) if sizes else None
        readable = 0
        for item in present:
            if self.runner.exists("dd"):
                result = self.runner.run(
                    ["dd", f"if={item}", "of=/dev/null", "bs=1", "count=1", "status=none"],
                    timeout=3,
                )
                if result.ok:
                    readable += 1
                continue
            try:
                with item.open("rb") as handle:
                    handle.read(1)
                readable += 1
            except OSError:
                pass

        storage = None
        if expected_paths:
            local = 0
            base = os.path.abspath(path)
            for item in expected_paths:
                try:
                    if self.runner.exists("readlink"):
                        link = self.runner.run(["readlink", "--", str(item)], timeout=3)
                        if link.returncode == 1:
                            local += 1
                            continue
                        if not link.ok:
                            continue
                        target = link.stdout
                    else:
                        if not item.is_symlink():
                            local += 1
                            continue
                        target = os.readlink(item)
                    resolved = os.path.abspath(
                        target if os.path.isabs(target) else os.path.join(item.parent, target)
                    )
                    if os.path.commonpath((base, resolved)) == base:
                        local += 1
                except (OSError, ValueError):
                    pass
            storage = "local" if local == len(expected_paths) else "external" if local == 0 else "mixed"
        complete = bool(expected_paths) and len(present) == len(expected_paths) and readable == len(expected_paths)
        return {
            "complete": complete,
            "expected_files": len(expected_paths) if expected_paths else None,
            "present_files": len(present) if expected_paths else None,
            "readable_files": readable if expected_paths else None,
            "storage": storage,
            "verification": verification,
            "size_mib": logical_size_mib,
        }

    @staticmethod
    def _quantization(config: dict) -> str | None:
        value = config.get("quantization_config")
        if isinstance(value, dict):
            method = value.get("quant_method") or value.get("quantization_method")
            return str(method) if method else None
        return None

    def _probe_dure_models(self, root: Path) -> list[InstalledModelProfile]:
        try:
            candidates = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name)
        except OSError:
            return []
        if (root / "config.json").is_file():
            candidates.insert(0, root)
        models: list[InstalledModelProfile] = []
        for candidate in candidates[:MAX_DISCOVERED_MODELS]:
            config_path = candidate / "config.json"
            config = self._model_config(config_path) if config_path.is_file() else {}
            artifact = self.artifact_state(candidate)
            size_values = [
                item
                for item in (self._model_size_mib(candidate), artifact["size_mib"])
                if isinstance(item, int)
            ]
            configured_name = config.get("_name_or_path") or config.get("name_or_path")
            model_id = (
                str(configured_name)
                if configured_name and not str(configured_name).startswith("/")
                else candidate.name
            )
            models.append(
                InstalledModelProfile(
                    source="dure",
                    model_id=model_id,
                    path=str(candidate),
                    quantization=self._quantization(config),
                    size_mib=max(size_values) if size_values else None,
                    complete=bool(config_path.is_file() and artifact["complete"]),
                    expected_files=artifact["expected_files"],
                    present_files=artifact["present_files"],
                    readable_files=artifact["readable_files"],
                    storage=artifact["storage"],
                    verification=str(artifact["verification"]),
                )
            )
        return models

    def _probe_huggingface_models(self, root: Path) -> list[InstalledModelProfile]:
        try:
            repositories = sorted(
                (item for item in root.iterdir() if item.is_dir() and item.name.startswith("models--")),
                key=lambda item: item.name,
            )
        except OSError:
            return []
        models: list[InstalledModelProfile] = []
        for repository in repositories[:MAX_DISCOVERED_MODELS]:
            model_id = repository.name.removeprefix("models--").replace("--", "/")
            snapshots_root = repository / "snapshots"
            try:
                snapshots = sorted(
                    (item for item in snapshots_root.iterdir() if item.is_dir()),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                snapshots = []
            snapshot = snapshots[0] if snapshots else None
            config_path = snapshot / "config.json" if snapshot else None
            config = self._model_config(config_path) if config_path and config_path.is_file() else {}
            artifact = self.artifact_state(snapshot) if snapshot else {
                "complete": False,
                "expected_files": None,
                "present_files": None,
                "readable_files": None,
                "storage": None,
                "verification": "metadata-only",
                "size_mib": None,
            }
            size_values = [
                item
                for item in (self._model_size_mib(repository), artifact["size_mib"])
                if isinstance(item, int)
            ]
            models.append(
                InstalledModelProfile(
                    source="huggingface-cache",
                    model_id=model_id,
                    path=str(snapshot or repository),
                    revision=snapshot.name if snapshot else None,
                    quantization=self._quantization(config),
                    size_mib=max(size_values) if size_values else None,
                    complete=bool(snapshot and config_path and config_path.is_file() and artifact["complete"]),
                    expected_files=artifact["expected_files"],
                    present_files=artifact["present_files"],
                    readable_files=artifact["readable_files"],
                    storage=artifact["storage"],
                    verification=str(artifact["verification"]),
                )
            )
        return models

    def _probe_ollama_models(self) -> list[InstalledModelProfile]:
        if not self.runner.exists("ollama"):
            return []
        result = self.runner.run(["ollama", "list"], timeout=15)
        if not result.ok:
            return []
        models: list[InstalledModelProfile] = []
        for line in result.stdout.splitlines()[1:MAX_DISCOVERED_MODELS + 1]:
            parts = line.split()
            if not parts:
                continue
            models.append(InstalledModelProfile(source="ollama", model_id=parts[0]))
        return models

    def _probe_models(self) -> list[InstalledModelProfile]:
        models: list[InstalledModelProfile] = []
        for root in self.model_roots:
            if root.name == "hub":
                models.extend(self._probe_huggingface_models(root))
            else:
                models.extend(self._probe_dure_models(root))
            if len(models) >= MAX_DISCOVERED_MODELS:
                break
        if len(models) < MAX_DISCOVERED_MODELS:
            models.extend(self._probe_ollama_models())
        unique: dict[tuple[str, str, str | None], InstalledModelProfile] = {}
        for model in models[:MAX_DISCOVERED_MODELS]:
            unique[(model.source, model.model_id, model.path)] = model
        return list(unique.values())

    @staticmethod
    def _labels(value: str) -> dict[str, str]:
        labels: dict[str, str] = {}
        for item in value.split(","):
            key, separator, label_value = item.partition("=")
            if separator and key:
                labels[key] = label_value
        return labels

    def _probe_container_workloads(self, runtime: RuntimeProfile) -> list[WorkloadProfile]:
        if runtime.engine != "docker" or not runtime.engine_ready:
            return []
        result = self.runner.run(
            ["docker", "ps", "--all", "--format", "{{json .}}"], timeout=15
        )
        if not result.ok:
            return []
        workloads: list[WorkloadProfile] = []
        for line in result.stdout.splitlines()[:200]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            labels = self._labels(str(item.get("Labels", "")))
            name = str(item.get("Names", ""))
            image = str(item.get("Image", ""))
            haystack = f"{name} {image}".lower()
            runtime_name = (
                "ray"
                if name.startswith("dure-ray-")
                else next(
                    (value for marker, value in LLM_RUNTIME_MARKERS.items() if marker in haystack),
                    "unknown",
                )
            )
            dure_managed = "dure.deployment" in labels
            if not dure_managed and runtime_name == "unknown":
                continue
            workloads.append(
                WorkloadProfile(
                    name=name,
                    runtime=runtime_name,
                    image=image,
                    status=str(item.get("Status", "unknown")),
                    deployment_id=labels.get("dure.deployment"),
                    generation=labels.get("dure.generation"),
                    model_id=labels.get("dure.model"),
                    dure_managed=dure_managed,
                    source="container",
                )
            )
        return workloads

    def _probe_host_workloads(self) -> list[WorkloadProfile]:
        workloads: list[WorkloadProfile] = []
        if self.runner.exists("nvidia-smi"):
            command = [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ]
            result = self.runner.run(command, timeout=10)
            if result.ok:
                for line in result.stdout.splitlines()[:200]:
                    parts = [part.strip() for part in line.split(",", 3)]
                    if len(parts) != 4:
                        continue
                    haystack = parts[2].lower()
                    runtime_name = next(
                        (value for marker, value in LLM_RUNTIME_MARKERS.items() if marker in haystack),
                        "ray" if "ray::" in haystack else "gpu-process",
                    )
                    try:
                        pid = int(parts[1])
                        gpu_memory_mib = int(float(parts[3]))
                    except ValueError:
                        continue
                    workloads.append(
                        WorkloadProfile(
                            name=parts[2],
                            runtime=runtime_name,
                            image="",
                            status="running",
                            source="nvidia-smi",
                            pid=pid,
                            gpu_uuid=parts[0],
                            gpu_memory_mib=gpu_memory_mib,
                        )
                    )
        if self.runner.exists("ps"):
            command = ["ps", "-eo", "pid=,comm="]
            result = self.runner.run(command, timeout=8)
            if result.ok:
                process_markers = {
                    "raylet": "ray",
                    "gcs_server": "ray",
                    "vllm": "vllm",
                    "ollama": "ollama",
                    "text-generation": "tgi",
                }
                for line in result.stdout.splitlines()[:1000]:
                    fields = line.strip().split(None, 1)
                    if len(fields) != 2:
                        continue
                    runtime_name = next(
                        (value for marker, value in process_markers.items() if marker in fields[1].lower()),
                        None,
                    )
                    if runtime_name is None:
                        continue
                    try:
                        pid = int(fields[0])
                    except ValueError:
                        continue
                    workloads.append(
                        WorkloadProfile(
                            name=fields[1],
                            runtime=runtime_name,
                            image="",
                            status="running",
                            source="host-process",
                            pid=pid,
                        )
                    )
        unique: dict[tuple[str, int | None, str], WorkloadProfile] = {}
        for workload in workloads:
            unique[(workload.source, workload.pid, workload.runtime)] = workload
        return list(unique.values())

    def _probe_workloads(self, runtime: RuntimeProfile) -> list[WorkloadProfile]:
        return self._probe_container_workloads(runtime) + self._probe_host_workloads()

    def _probe_gpus(self, issues: list[str]) -> list[GPUProfile]:
        if not self.runner.exists("nvidia-smi"):
            if self.runner.exists("lspci"):
                pci = self.runner.run(["lspci"], timeout=5)
                if "NVIDIA" in pci.stdout:
                    issues.append("NVIDIA hardware is visible on PCI but nvidia-smi is unavailable")
            return []

        extended_command = [
            "nvidia-smi",
            "--query-gpu=index,name,uuid,driver_version,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        query = self.runner.run(extended_command, timeout=10)
        extended = query.ok
        if not query.ok:
            basic_command = [
                "nvidia-smi",
                "--query-gpu=index,name,uuid,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ]
            query = self.runner.run(basic_command, timeout=10)
        if not query.ok:
            issues.append(f"nvidia-smi failed: {query.stderr or query.stdout}")
            return []

        compute_caps: dict[int, str] = {}
        cap_result = self.runner.run(
            ["nvidia-smi", "--query-gpu=index,compute_cap", "--format=csv,noheader,nounits"],
            timeout=10,
        )
        if cap_result.ok:
            for line in cap_result.stdout.splitlines():
                parts = [part.strip() for part in line.split(",", 1)]
                if len(parts) == 2 and parts[0].isdigit():
                    compute_caps[int(parts[0])] = parts[1]

        gpus: list[GPUProfile] = []
        for line in query.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != (7 if extended else 5):
                continue
            try:
                index = int(parts[0])
                memory_mib = int(float(parts[4]))
                memory_used_mib = int(float(parts[5])) if extended and parts[5] not in {"N/A", "[N/A]"} else None
                utilization = int(float(parts[6])) if extended and parts[6] not in {"N/A", "[N/A]"} else None
            except ValueError:
                continue
            gpus.append(
                GPUProfile(
                    index=index,
                    name=parts[1],
                    uuid=parts[2],
                    driver_version=parts[3],
                    memory_mib=memory_mib,
                    compute_capability=compute_caps.get(index),
                    memory_used_mib=memory_used_mib,
                    utilization_percent=utilization,
                )
            )
        return gpus

    def _probe_runtime(self) -> RuntimeProfile:
        engine = next((name for name in ("docker", "podman") if self.runner.exists(name)), None)
        engine_ready = False
        nvidia_runtime = False
        if engine:
            version = self.runner.run([engine, "version"], timeout=8)
            engine_ready = version.ok
            if engine == "docker" and engine_ready:
                info = self.runner.run(
                    ["docker", "info", "--format", "{{json .Runtimes}}"], timeout=8
                )
                nvidia_runtime = info.ok and "nvidia" in info.stdout.lower()
            elif engine == "podman" and engine_ready:
                nvidia_runtime = self.runner.exists("nvidia-ctk") or Path(
                    "/etc/cdi/nvidia.yaml"
                ).exists()

        ray_available = self.runner.exists("ray")
        ray_version = None
        if ray_available:
            result = self.runner.run(["ray", "--version"], timeout=5)
            if result.ok:
                ray_version = result.stdout.splitlines()[-1] if result.stdout else None

        return RuntimeProfile(
            engine=engine,
            engine_ready=engine_ready,
            nvidia_runtime=nvidia_runtime,
            ray_available=ray_available,
            ray_version=ray_version,
        )

    def _probe_network(self) -> NetworkProfile:
        addresses: list[str] = []
        default_interface = None
        if self.runner.exists("ip"):
            address_result = self.runner.run(["ip", "-j", "address", "show"], timeout=5)
            if address_result.ok:
                try:
                    for interface in json.loads(address_result.stdout):
                        for info in interface.get("addr_info", []):
                            if info.get("family") == "inet" and info.get("local") != "127.0.0.1":
                                addresses.append(info["local"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            route_result = self.runner.run(["ip", "-j", "route", "show", "default"], timeout=5)
            if route_result.ok:
                try:
                    routes = json.loads(route_result.stdout)
                    if routes:
                        default_interface = routes[0].get("dev")
                except (json.JSONDecodeError, TypeError):
                    pass
        return NetworkProfile(default_interface=default_interface, addresses=addresses)
