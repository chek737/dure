from __future__ import annotations

import unittest
from pathlib import Path

from speed.async_pipeline_optimizer import (
    detected_gpu_count,
    launch_detected_gpus,
    launcher_command,
)


class FakeCuda:
    def __init__(self, count: int) -> None:
        self.count = count

    def is_available(self) -> bool:
        return self.count > 0

    def device_count(self) -> int:
        return self.count


class FakeTorch:
    def __init__(self, count: int) -> None:
        self.cuda = FakeCuda(count)


class AsyncPipelineLauncherTests(unittest.TestCase):
    def test_detected_gpu_count_builds_matching_torchrun_command(self):
        self.assertEqual(detected_gpu_count(FakeTorch(3)), 3)
        command = launcher_command(3, Path("optimizer.py"))
        self.assertIn("--nproc-per-node=3", command)
        self.assertEqual(command[-1], "optimizer.py")

    def test_launcher_runs_once_with_all_visible_gpus(self):
        calls = []

        class Completed:
            returncode = 0

        def run(command, *, check):
            calls.append((command, check))
            return Completed()

        self.assertEqual(
            launch_detected_gpus(torch_module=FakeTorch(2), process_runner=run), 0
        )
        self.assertIn("--nproc-per-node=2", calls[0][0])
        self.assertFalse(calls[0][1])

    def test_launcher_rejects_a_host_without_visible_cuda_gpus(self):
        with self.assertRaisesRegex(RuntimeError, "No CUDA GPU"):
            launch_detected_gpus(torch_module=FakeTorch(0))


if __name__ == "__main__":
    unittest.main()
