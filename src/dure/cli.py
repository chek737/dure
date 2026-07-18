from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .models import CheckResult, NodeProfile
from .orchestrator import InitOrchestrator
from .planner import build_plan, classify_node, recommend_local_model
from .probe import NodeProbe
from .readiness import ReadinessVerifier
from .runtime import read_plan, write_plan
from .state import StateStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dure", description="Community LLM node bootstrapper")
    parser.add_argument("--version", action="version", version=f"dure {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect node hardware and runtime")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    doctor.add_argument("--output", type=Path, help="Write the profile to a JSON file")

    plan = subparsers.add_parser("plan", help="Create a deployment plan")
    plan.add_argument("--profile", type=Path, action="append", default=[])
    plan.add_argument("--model", default="auto")
    plan.add_argument("--image", default="vllm/vllm-openai:latest")
    plan.add_argument("--network-interface")
    plan.add_argument("--output", type=Path, required=True)

    init = subparsers.add_parser("init", help="Initialize and optionally provision this node")
    init.add_argument("--plan", type=Path)
    init.add_argument("--apply", action="store_true")
    init.add_argument("--accept-model-download", action="store_true")
    init.add_argument("--pull", action="store_true")
    init.add_argument("--allow-unpinned-image", action="store_true")
    init.add_argument("--replace", action="store_true")
    init.add_argument("--serve", action="store_true")
    init.add_argument("--state-file", type=Path)
    init.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="Show persisted node state")
    status.add_argument("--state-file", type=Path)
    status.add_argument("--json", action="store_true")

    verify = subparsers.add_parser("verify", help="Verify an applied deployment")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--api", action="store_true")

    return parser


def _print_checks(checks: list[CheckResult]) -> None:
    for check in checks:
        marker = "✓" if check.ok else ("✗" if check.blocking else "!")
        print(f"{marker} {check.name}: {check.detail}")


def _doctor(args: argparse.Namespace) -> int:
    profile = NodeProbe().collect()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(profile.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
        return 0

    role, capabilities = classify_node(profile)
    print(f"Node: {profile.node_id}")
    print(f"OS: {profile.os_name} / {profile.kernel}")
    print(f"CPU: {profile.cpu_model} ({profile.cpu_count} cores)")
    print(
        f"Memory: {profile.memory_available_mib / 1024:.1f}/{profile.memory_mib / 1024:.1f} GiB available"
    )
    print(f"Disk: {profile.disk_free_mib / 1024:.1f}/{profile.disk_total_mib / 1024:.1f} GiB free")
    if profile.gpus:
        for gpu in profile.gpus:
            print(
                f"GPU {gpu.index}: {gpu.name}, {gpu.memory_mib / 1024:.1f} GiB, "
                f"driver {gpu.driver_version}, compute {gpu.compute_capability or 'unknown'}"
            )
    else:
        print("GPU: no CUDA-capable NVIDIA GPU")
    print(
        f"Runtime: {profile.runtime.engine or 'none'}, "
        f"NVIDIA runtime={'yes' if profile.runtime.nvidia_runtime else 'no'}, "
        f"Ray={'yes' if profile.runtime.ray_available else 'no'}"
    )
    print(f"Recommended role: {role}")
    print(f"Capabilities: {', '.join(capabilities)}")
    model = recommend_local_model(profile)
    if model:
        print(f"Recommended local model: {model.model_id}")
    for issue in profile.issues:
        print(f"! {issue}")
    return 0


def _load_profiles(paths: list[Path]) -> list[NodeProfile]:
    if not paths:
        return [NodeProbe().collect()]
    return [NodeProfile.from_dict(json.loads(path.read_text(encoding="utf-8"))) for path in paths]


def _plan(args: argparse.Namespace) -> int:
    profiles = _load_profiles(args.profile)
    try:
        deployment = build_plan(
            profiles,
            model_id=args.model,
            image=args.image,
            network_interface=args.network_interface,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if deployment is None:
        print("No eligible GPU deployment could be planned", file=sys.stderr)
        return 2
    write_plan(args.output, deployment)
    print(f"Wrote {deployment.model.model_id} deployment plan to {args.output}")
    print(
        f"PP={deployment.pipeline_parallel_size}, TP={deployment.tensor_parallel_size}, "
        f"world_size={deployment.world_size}"
    )
    for assignment in deployment.assignments:
        print(
            f"- {assignment.node_id}: rank {assignment.rank}, PP {assignment.pipeline_rank}, "
            f"layers {assignment.layer_start}-{assignment.layer_end}"
        )
    for warning in deployment.warnings:
        print(f"! {warning}")
    return 0


def _init(args: argparse.Namespace) -> int:
    deployment = read_plan(args.plan) if args.plan else None
    profile, plan, checks = InitOrchestrator(state_path=args.state_file).run(
        plan=deployment,
        apply=args.apply,
        accept_model_download=args.accept_model_download,
        pull=args.pull,
        allow_unpinned_image=args.allow_unpinned_image,
        replace=args.replace,
        serve=args.serve,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "profile": profile.to_dict(),
                    "plan": plan.to_dict() if plan else None,
                    "checks": [check.to_dict() for check in checks],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_checks(checks)
        print(f"State: {StateStore(args.state_file).load().phase}")
    return 0 if all(check.ok or not check.blocking for check in checks) else 1


def _status(args: argparse.Namespace) -> int:
    state = StateStore(args.state_file).load()
    if args.json:
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Node: {state.node_id or 'uninitialized'}")
        print(f"Phase: {state.phase}")
        print(f"Role: {state.role or '-'}")
        print(f"Deployment: {state.deployment_id or '-'}")
        print(f"Generation: {state.generation}")
        print(f"Updated: {state.updated_at}")
        if state.detail:
            print(f"Detail: {state.detail}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    plan = read_plan(args.plan)
    profile = NodeProbe().collect()
    verifier = ReadinessVerifier(engine=profile.runtime.engine or "docker")
    checks = [verifier.host_gpu(profile), verifier.container_gpu(plan), verifier.ray_cluster(plan)]
    if args.api:
        checks.append(verifier.api())
    _print_checks(checks)
    return 0 if all(check.ok for check in checks) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {
        "doctor": _doctor,
        "plan": _plan,
        "init": _init,
        "status": _status,
        "verify": _verify,
    }
    try:
        return handlers[args.command](args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

