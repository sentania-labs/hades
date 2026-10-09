"""Render-time resource budget check (issue 93, requirement 5).

Sums the CPU and memory *requests* the rendered `hades` namespace's own Deployments,
StatefulSets and Jobs carry (the control plane: api, supervisor, postgres, the migration
Job), and adds the `hades-workers` namespace's ResourceQuota `requests.cpu` /
`requests.memory`, which is deploy/kubernetes's own record of what the configured
concurrency requests at once (26, the ResourceQuota comment). That sum is what the
cluster must have unreserved for Crucible plus one attempt at every running slot before
anything is Pending. Printed always; refused only when a budget is given and exceeded,
so a target with no meaningful budget (kind, or a lab overlay still carrying its
placeholders) can still be rendered without failing this check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

# Order matters: a suffix checked before its own prefix (`Ki` before `k`) would never
# match, since both are valid trailing substrings of the same string.
_MEMORY_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    # Kubernetes' decimal SI suffixes: lowercase `k` for kilo, uppercase for the rest.
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


def parse_cpu(value: Any) -> float:
    text = str(value).strip()
    if text.endswith("m"):
        return float(text[:-1]) / 1000
    return float(text)


def parse_memory(value: Any) -> float:
    text = str(value).strip()
    for suffix, factor in _MEMORY_UNITS.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


def _request(container: dict[str, Any], key: str, parse: Any) -> float:
    """A container's request for one resource, falling back to its limit: Kubernetes
    defaults an unset request to the container's own limit, not to zero."""
    resources = container.get("resources") or {}
    requests = resources.get("requests") or {}
    if key in requests:
        return float(parse(requests[key]))
    limits = resources.get("limits") or {}
    if key in limits:
        return float(parse(limits[key]))
    return 0.0


def _pod_request(obj: dict[str, Any]) -> tuple[float, float]:
    """One Pod template's effective request: Kubernetes takes the larger of the sum of
    its app containers and any single init container, since init containers run one at
    a time before the app containers ever start."""
    template = ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}
    app_cpu = sum(_request(c, "cpu", parse_cpu) for c in template.get("containers") or [])
    app_memory = sum(_request(c, "memory", parse_memory) for c in template.get("containers") or [])
    init_cpu = max(
        (_request(c, "cpu", parse_cpu) for c in template.get("initContainers") or []), default=0.0
    )
    init_memory = max(
        (_request(c, "memory", parse_memory) for c in template.get("initContainers") or []),
        default=0.0,
    )
    return max(app_cpu, init_cpu), max(app_memory, init_memory)


def control_plane_requests(objects: list[dict[str, Any]], namespace: str) -> tuple[float, float]:
    """The CPU and memory requests of every workload in one namespace, one pod's
    request times its replica count (Deployment, StatefulSet); a Job runs one pod
    regardless of what `replicas` would mean elsewhere, so it is not multiplied."""
    cpu = 0.0
    memory = 0.0
    for obj in objects:
        if (obj.get("metadata") or {}).get("namespace") != namespace:
            continue
        if obj.get("kind") not in ("Deployment", "StatefulSet", "Job", "DaemonSet"):
            continue
        replicas = int((obj.get("spec") or {}).get("replicas") or 1) if obj["kind"] != "Job" else 1
        pod_cpu, pod_memory = _pod_request(obj)
        cpu += pod_cpu * replicas
        memory += pod_memory * replicas
    return cpu, memory


def workers_quota_requests(objects: list[dict[str, Any]], namespace: str) -> tuple[float, float]:
    """`requests.cpu` / `requests.memory` of the named ResourceQuota: the configured
    concurrency's worth of attempts, already sized for the running policy (26)."""
    for obj in objects:
        if obj.get("kind") != "ResourceQuota":
            continue
        if (obj.get("metadata") or {}).get("namespace") != namespace:
            continue
        hard = (obj.get("spec") or {}).get("hard") or {}
        cpu = parse_cpu(hard["requests.cpu"]) if "requests.cpu" in hard else 0.0
        memory = parse_memory(hard["requests.memory"]) if "requests.memory" in hard else 0.0
        return cpu, memory
    return 0.0, 0.0


def _format_memory(value: float) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.2f}Gi"
    if value >= 1024**2:
        return f"{value / 1024**2:.2f}Mi"
    return f"{value:.0f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="a rendered, fully-kustomized YAML file")
    parser.add_argument("--label", default="", help="a name for this target in the printed line")
    parser.add_argument(
        "--cpu-budget", type=float, default=None, help="cores; refuse the target above this"
    )
    parser.add_argument(
        "--memory-budget-gi", type=float, default=None, help="Gi; refuse the target above this"
    )
    args = parser.parse_args(argv)

    objects = [doc for doc in yaml.safe_load_all(args.manifest.read_text()) if doc]
    control_cpu, control_memory = control_plane_requests(objects, "hades")
    quota_cpu, quota_memory = workers_quota_requests(objects, "hades-workers")
    total_cpu = control_cpu + quota_cpu
    total_memory = control_memory + quota_memory

    label = args.label or args.manifest.name
    print(
        f"manifests: {label}: requested {total_cpu:.2f} CPU "
        f"({control_cpu:.2f} control plane + {quota_cpu:.2f} workers at configured "
        f"concurrency), {_format_memory(total_memory)} memory "
        f"({_format_memory(control_memory)} control plane + "
        f"{_format_memory(quota_memory)} workers)"
    )

    over_budget = []
    if args.cpu_budget is not None and total_cpu > args.cpu_budget:
        over_budget.append(
            f"{total_cpu:.2f} CPU requested exceeds the {args.cpu_budget:.2f} budget"
        )
    if args.memory_budget_gi is not None:
        budget_bytes = args.memory_budget_gi * 1024**3
        if total_memory > budget_bytes:
            over_budget.append(
                f"{_format_memory(total_memory)} memory requested exceeds the "
                f"{args.memory_budget_gi:.2f}Gi budget"
            )
    if over_budget:
        for line in over_budget:
            print(f"manifests: {label}: {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
