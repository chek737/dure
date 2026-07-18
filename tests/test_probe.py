import unittest

from dure.command import CommandResult
from dure.probe import NodeProbe

from .helpers import FakeRunner


class ProbeTests(unittest.TestCase):
    def test_parses_nvidia_smi_and_runtime(self):
        gpu_query = (
            "nvidia-smi",
            "--query-gpu=index,name,uuid,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        )
        cap_query = (
            "nvidia-smi",
            "--query-gpu=index,compute_cap",
            "--format=csv,noheader,nounits",
        )
        runner = FakeRunner(
            executables={"nvidia-smi", "docker", "ray"},
            responses={
                gpu_query: CommandResult(
                    gpu_query,
                    0,
                    "0, NVIDIA GeForce RTX 3090, GPU-123, 610.43.02, 24576",
                ),
                cap_query: CommandResult(cap_query, 0, "0, 8.6"),
                ("docker", "version"): CommandResult(("docker", "version"), 0, "ok"),
                ("docker", "info", "--format", "{{json .Runtimes}}"): CommandResult(
                    ("docker", "info"), 0, '{"runc":{},"nvidia":{}}'
                ),
                ("ray", "--version"): CommandResult(
                    ("ray", "--version"), 0, "ray, version 2.56.1"
                ),
            },
        )

        result = NodeProbe(runner).collect()

        self.assertEqual(len(result.gpus), 1)
        self.assertEqual(result.gpus[0].memory_mib, 24576)
        self.assertEqual(result.gpus[0].compute_capability, "8.6")
        self.assertTrue(result.runtime.engine_ready)
        self.assertTrue(result.runtime.nvidia_runtime)
        self.assertTrue(result.runtime.ray_available)

    def test_reports_missing_gpu(self):
        result = NodeProbe(FakeRunner()).collect()
        self.assertEqual(result.gpus, [])
        self.assertIn("No CUDA-capable NVIDIA GPU detected", result.issues)


if __name__ == "__main__":
    unittest.main()

