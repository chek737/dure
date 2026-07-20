from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .models import ModelSpec, NodeProfile
from .planner import MODELS, matching_installed_model


OBJECTIVES = {"quality", "balanced", "throughput", "reuse-first"}


def _complete_model_ids(profile: NodeProfile) -> list[str]:
    return sorted({item.model_id for item in profile.installed_models if item.complete})


def _model_is_cached(profile: NodeProfile, model: ModelSpec) -> bool:
    return matching_installed_model(profile, model) is not None


def _profile_age_seconds(item: dict[str, Any], profile: NodeProfile | None) -> int | None:
    value = item.get("profile_updated_at") or (profile.observed_at if profile else None)
    if not value:
        return None
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - observed).total_seconds()))
    except ValueError:
        return None


def _gpu_state(profile: NodeProfile, *, profile_age_seconds: int | None) -> tuple[str, list[str]]:
    blockers: list[str] = []
    healthy = [item for item in profile.gpus if item.healthy]
    if not healthy:
        return "none", blockers
    if profile_age_seconds is not None and profile_age_seconds > 900:
        blockers.append(f"node profile is stale ({profile_age_seconds} seconds old)")
        return "stale", blockers
    gpu = max(healthy, key=lambda item: item.memory_mib)
    gpu_workloads = [
        item for item in profile.workloads if item.gpu_memory_mib and item.gpu_memory_mib > 512
    ]
    if gpu.memory_used_mib is None:
        blockers.append("GPU memory usage was not observed; upgrade and refresh the Agent")
        return "unknown", blockers
    if gpu.memory_used_mib > 1024 or gpu_workloads:
        blockers.append(f"GPU is already using {gpu.memory_used_mib} MiB")
        if any(not item.dure_managed for item in gpu_workloads):
            blockers.append("an unmanaged GPU workload must not be stopped automatically")
        return "occupied", blockers
    if not profile.runtime.engine_ready or not profile.runtime.nvidia_runtime:
        blockers.append("Dure's container and NVIDIA runtime are not ready")
        return "blocked", blockers
    return "available", blockers


def _node_view(item: dict[str, Any]) -> tuple[dict[str, Any], NodeProfile | None]:
    profile_data = item.get("profile")
    profile = NodeProfile.from_dict(profile_data) if isinstance(profile_data, dict) else None
    profile_age_seconds = _profile_age_seconds(item, profile)
    if profile is not None:
        profile.node_id = str(item.get("id") or profile.node_id)
        gpu_state, blockers = _gpu_state(
            profile, profile_age_seconds=profile_age_seconds
        )
    else:
        gpu_state, blockers = "unknown", ["no node profile is available"]
    return (
        {
            "node_id": item.get("id"),
            "hostname": item.get("hostname"),
            "approved": bool(item.get("approved")),
            "connectivity": item.get("connectivity"),
            "profile_schema_version": profile.profile_schema_version if profile else None,
            "profile_age_seconds": profile_age_seconds,
            "gpu_state": gpu_state,
            "gpu_count": len(profile.gpus) if profile else 0,
            "gpu_memory_gib": round(profile.total_gpu_memory_mib / 1024, 2) if profile else 0,
            "cpu_count": profile.cpu_count if profile else 0,
            "memory_available_gib": round(profile.memory_available_mib / 1024, 2) if profile else 0,
            "disk_free_gib": round(profile.disk_free_mib / 1024, 2) if profile else 0,
            "complete_models": _complete_model_ids(profile) if profile else [],
            "workloads": [
                {
                    "name": workload.name,
                    "runtime": workload.runtime,
                    "source": workload.source,
                    "dure_managed": workload.dure_managed,
                    "gpu_memory_mib": workload.gpu_memory_mib,
                }
                for workload in (profile.workloads if profile else [])
            ],
            "blockers": blockers,
        },
        profile,
    )


def _layout_specs(objective: str, gpu_count: int, profiles: list[NodeProfile]) -> list[tuple[str, int]]:
    if gpu_count <= 0:
        return []
    if objective == "throughput":
        return [("qwen2.5-14b-awq", 1)] * gpu_count
    if objective == "quality" and gpu_count >= 7:
        return [("qwen3-235b-a22b-awq", 7)]
    if objective == "reuse-first":
        qwen3 = MODELS["qwen3-235b-a22b-awq"]
        qwen72 = MODELS["qwen2.5-72b-awq"]
        if gpu_count >= 7 and any(_model_is_cached(item, qwen3) for item in profiles):
            return [(qwen3.model_id, 7)]
        if gpu_count >= 3 and any(_model_is_cached(item, qwen72) for item in profiles):
            result = [(qwen72.model_id, 3)]
            return result + [("qwen2.5-14b-awq", 1)] * (gpu_count - 3)

    result: list[tuple[str, int]] = []
    remaining = gpu_count
    if remaining >= 3:
        replicas = remaining // 3 if objective == "balanced" else 1
        for _ in range(replicas):
            result.append(("qwen2.5-72b-awq", 3))
            remaining -= 3
    result.extend(("qwen2.5-14b-awq", 1) for _ in range(remaining))
    return result


def _assign_layout(
    objective: str,
    profiles: list[NodeProfile],
) -> list[dict[str, Any]]:
    remaining = list(profiles)
    deployments: list[dict[str, Any]] = []
    for replica, (model_id, stages) in enumerate(
        _layout_specs(objective, len(profiles), profiles), start=1
    ):
        model = MODELS[model_id]
        ranked = sorted(
            remaining,
            key=lambda item: (
                not _model_is_cached(item, model),
                -max((gpu.memory_free_mib or 0) for gpu in item.gpus),
                item.node_id,
            ),
        )
        selected = ranked[:stages]
        selected_ids = {item.node_id for item in selected}
        remaining = [item for item in remaining if item.node_id not in selected_ids]
        deployments.append(
            {
                "name": f"{model_id}-replica-{replica}",
                "model_id": model_id,
                "repository": model.repository,
                "strategy": "pipeline-parallel" if stages > 1 else "single-gpu",
                "pipeline_parallel_size": stages,
                "node_ids": [item.node_id for item in selected],
                "cached_node_ids": [item.node_id for item in selected if _model_is_cached(item, model)],
                "checkpoint_gib": model.checkpoint_gib,
                "prerequisites": (
                    [
                        "all stages must expose the same readable model path and immutable revision",
                        "validate RTT, bandwidth, NCCL and exact Ray membership before start",
                    ]
                    if stages > 1
                    else ["reserve KV-cache headroom and benchmark the selected context length"]
                ),
            }
        )
    return deployments


def _cpu_roles(profile: NodeProfile, gpu_state: str) -> list[str]:
    if profile.gpus:
        roles = ["ray-control", "artifact-read-verification"]
        if gpu_state == "occupied":
            roles.append("latency-safe-lightweight-services-only")
        return roles
    roles = ["node-agent"]
    if profile.disk_free_mib >= 51200:
        roles.extend(["artifact-cache", "download-and-checksum"])
    if profile.cpu_count >= 4 and profile.memory_mib >= 6144:
        roles.append("quantized-embedding-worker")
    if profile.memory_mib >= 3072:
        roles.extend(["api-gateway", "ingest-or-queue"])
    if profile.memory_mib < 8192:
        roles.append("qdrant-on-disk-development-only")
    return roles


def build_capacity_plan(
    inventory: dict[str, Any],
    *,
    objective: str = "balanced",
    reserve_gpus: int = 0,
) -> dict[str, Any]:
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown capacity objective: {objective}")
    if reserve_gpus < 0:
        raise ValueError("reserve_gpus cannot be negative")

    selected: list[tuple[dict[str, Any], NodeProfile]] = []
    views: list[dict[str, Any]] = []
    for item in inventory.get("nodes", []):
        view, profile = _node_view(item)
        views.append(view)
        if (
            profile is not None
            and item.get("approved")
            and item.get("connectivity") == "online"
        ):
            selected.append((view, profile))

    gpu_profiles = [(view, profile) for view, profile in selected if profile.has_healthy_gpu]
    available = [profile for view, profile in gpu_profiles if view["gpu_state"] == "available"]
    available.sort(key=lambda item: item.node_id)
    if reserve_gpus > len(available):
        raise ValueError(
            f"cannot reserve {reserve_gpus} GPU node(s); only {len(available)} are available"
        )
    spare = available[-reserve_gpus:] if reserve_gpus else []
    deployable = available[:-reserve_gpus] if reserve_gpus else available
    deployments = _assign_layout(objective, deployable)

    total_cpu = sum(profile.cpu_count for _, profile in selected)
    total_memory = sum(profile.memory_mib for _, profile in selected)
    gpu_states = {
        state: 0 for state in ("available", "occupied", "blocked", "unknown", "stale")
    }
    for view, _ in gpu_profiles:
        gpu_states[view["gpu_state"]] = gpu_states.get(view["gpu_state"], 0) + 1

    warnings: list[str] = []
    if any(view["gpu_state"] == "unknown" for view, _ in gpu_profiles):
        warnings.append("Some GPU nodes lack dynamic usage data and were excluded from placement")
    if any(view["gpu_state"] == "stale" for view, _ in gpu_profiles):
        warnings.append("Some GPU profiles are stale and were excluded from placement")
    if any(view["gpu_state"] == "occupied" for view, _ in gpu_profiles):
        warnings.append("Existing GPU workloads were preserved and excluded from new placement")
    if not deployments:
        warnings.append("No new GPU deployment is currently admissible")

    capacity_revision = hashlib.sha256(
        json.dumps(
            {
                "objective": objective,
                "reserve_gpus": reserve_gpus,
                "nodes": views,
                "deployments": deployments,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    return {
        "schema_version": 1,
        "capacity_revision": capacity_revision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory_generated_at": inventory.get("generated_at"),
        "objective": objective,
        "summary": {
            "approved_online_nodes": len(selected),
            "gpu_nodes": len(gpu_profiles),
            "available_gpu_nodes": len(available),
            "deployable_gpu_nodes": len(deployable),
            "reserved_gpu_nodes": len(spare),
            "occupied_gpu_nodes": gpu_states.get("occupied", 0),
            "unknown_gpu_nodes": gpu_states.get("unknown", 0),
            "stale_gpu_nodes": gpu_states.get("stale", 0),
            "cpu_cores": total_cpu,
            "memory_gib": round(total_memory / 1024, 2),
        },
        "nodes": views,
        "recommended_deployments": deployments,
        "reserved_node_ids": [item.node_id for item in spare],
        "cpu_recommendations": [
            {
                "node_id": profile.node_id,
                "hostname": profile.hostname,
                "roles": _cpu_roles(profile, view["gpu_state"]),
            }
            for view, profile in selected
        ],
        "elasticity": {
            "policy": "generation-based; never resize a live pipeline in place",
            "on_gpu_join": [
                "keep the node quarantined until PROBE, artifact, network and GPU checks pass",
                "recompute candidates and stage a new replica or deployment generation",
                "switch traffic only after readiness succeeds and an operator approves",
            ],
            "on_gpu_leave": [
                "remove the node from new scheduling immediately",
                "if it belongs to a pipeline pod, mark that whole pod unavailable and route to a ready replica",
                "recompute a smaller generation; never silently shrink pipeline_parallel_size",
            ],
            "capacity_bands": [
                {"gpu_nodes": "0", "balanced_layout": "CPU utility workloads only"},
                {"gpu_nodes": "1-2", "balanced_layout": "one 14B single-GPU replica per GPU"},
                {"gpu_nodes": "3-5", "balanced_layout": "one 72B PP=3 pod plus 14B replicas"},
                {"gpu_nodes": "6", "balanced_layout": "two independent 72B PP=3 pods"},
                {"gpu_nodes": "7+", "balanced_layout": "two 72B pods plus 14B replicas or a 7-GPU 235B generation"},
            ],
        },
        "warnings": warnings,
    }


def render_capacity_plan(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = [
        f"Capacity objective: {plan['objective']}",
        (
            f"Nodes: {summary['approved_online_nodes']} approved online; "
            f"GPUs: {summary['available_gpu_nodes']} available, "
            f"{summary['occupied_gpu_nodes']} occupied, {summary['unknown_gpu_nodes']} unknown, "
            f"{summary['stale_gpu_nodes']} stale"
        ),
        "",
        "Recommended deployments:",
    ]
    if not plan["recommended_deployments"]:
        lines.append("- None")
    for deployment in plan["recommended_deployments"]:
        lines.append(
            f"- {deployment['name']}: {deployment['strategy']} on "
            f"{', '.join(deployment['node_ids'])}"
        )
    lines.extend(["", "Elasticity policy:", f"- {plan['elasticity']['policy']}"])
    for warning in plan["warnings"]:
        lines.append(f"! {warning}")
    return "\n".join(lines)
