from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
from pathlib import Path

from .command import Runner, SubprocessRunner
from .models import GPUProfile, NetworkProfile, NodeProfile, RuntimeProfile


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
    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or SubprocessRunner()

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
            issues=issues,
        )

    def _probe_gpus(self, issues: list[str]) -> list[GPUProfile]:
        if not self.runner.exists("nvidia-smi"):
            if self.runner.exists("lspci"):
                pci = self.runner.run(["lspci"], timeout=5)
                if "NVIDIA" in pci.stdout:
                    issues.append("NVIDIA hardware is visible on PCI but nvidia-smi is unavailable")
            return []

        query = self.runner.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,uuid,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=10,
        )
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
            if len(parts) != 5:
                continue
            try:
                index = int(parts[0])
                memory_mib = int(float(parts[4]))
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
