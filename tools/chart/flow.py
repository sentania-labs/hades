#!/usr/bin/env python3
"""Check that a chart render's names, namespaces and settings follow its values.

`make chart` runs this on renders with overrides (hades #601): a nameOverride that
renamed `metadata.name` but left the ConfigMap naming the default objects would deploy
a service that looks for an account, a claim and Secrets that do not exist.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

# The builder's namespace when buildkit.namespace is left alone. It never follows the
# names; KUBERNETES__BUILDKIT_NAMESPACE hands it to the provider.
BUILDKIT_NAMESPACE = "hades-buildkit"
# Object name prefixes a render must not carry once its names are something else: the
# earlier default and the current one (hades #609 step 2).
KNOWN_PREFIXES = ("crucible", "hades")
HARNESSES = ("claude_code", "codex", "agy", "hermes")


def _load(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _find(docs: list[dict[str, Any]], kind: str, namespace: str, name: str) -> dict[str, Any]:
    for doc in docs:
        metadata = doc.get("metadata", {})
        if (
            doc.get("kind") == kind
            and metadata.get("namespace", "") == namespace
            and metadata.get("name") == name
        ):
            return doc
    raise LookupError(f"{kind} {namespace}/{name}")


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = doc["spec"]["template"]["spec"]
    return spec


def problems(
    docs: list[dict[str, Any]],
    *,
    name: str,
    namespace: str,
    workers_namespace: str,
    settings: dict[str, str],
    pull_secret: str = "",
    buildkit: bool | None = None,
    buildkit_namespace: str = BUILDKIT_NAMESPACE,
) -> list[str]:
    """Every way the render fails to follow the given names and settings."""
    found: list[str] = []
    in_buildkit = [
        doc
        for doc in docs
        if doc.get("metadata", {}).get("namespace") == buildkit_namespace
        or (doc.get("kind") == "Namespace" and doc["metadata"]["name"] == buildkit_namespace)
    ]
    if buildkit is False and in_buildkit:
        found.append(f"buildkit is off but {len(in_buildkit)} objects render for it")
    if buildkit is True and not in_buildkit:
        found.append("buildkit is on but nothing renders for it")
    for doc in in_buildkit:
        if doc.get("kind") != "NetworkPolicy":
            continue
        for rule in doc["spec"].get("ingress", []):
            for peer in rule.get("from", []):
                labels = peer.get("namespaceSelector", {}).get("matchLabels", {})
                if labels.get("kubernetes.io/metadata.name") != workers_namespace:
                    found.append(f"the builder admits {labels}, not {workers_namespace}")
    allowed = {namespace, workers_namespace, buildkit_namespace}
    for doc in docs:
        kind = doc.get("kind", "")
        metadata = doc.get("metadata", {})
        object_name = metadata.get("name", "")
        where = metadata.get("namespace", "")
        if kind == "Namespace":
            if object_name not in allowed:
                found.append(f"Namespace {object_name} is none of {sorted(allowed)}")
            continue
        if where not in allowed:
            found.append(f"{kind} {object_name} is in {where or 'no namespace'}")
        for prefix in KNOWN_PREFIXES:
            if (
                name != prefix
                and where != buildkit_namespace
                and object_name.startswith(f"{prefix}-")
            ):
                found.append(f"{kind} {where}/{object_name} still carries the {prefix} name")

    try:
        config = _find(docs, "ConfigMap", namespace, f"{name}-settings")
    except LookupError as missing:
        return [*found, f"no {missing}"]
    data: dict[str, str] = config.get("data", {})
    expected = {
        "CRUCIBLE_KUBERNETES__NAMESPACE": namespace,
        "CRUCIBLE_KUBERNETES__WORKERS_NAMESPACE": workers_namespace,
        "CRUCIBLE_KUBERNETES__SERVICE_ACCOUNT": f"{name}-worker",
        "CRUCIBLE_KUBERNETES__CACHE_CLAIM": f"{name}-reference-cache",
        "CRUCIBLE_KUBERNETES__BUILDKIT_NAMESPACE": buildkit_namespace,
        "CRUCIBLE_ROOMS__API_NAMESPACE": namespace,
        **settings,
    }
    for key, value in expected.items():
        if data.get(key) != value:
            found.append(f"setting {key} is {data.get(key)!r}, expected {value!r}")
    secrets = json.loads(data.get("CRUCIBLE_KUBERNETES__CREDENTIAL_SECRETS", "{}"))
    for harness in HARNESSES:
        wanted = f"{name}-harness-{harness.replace('_', '-')}"
        if secrets.get(harness) != wanted:
            found.append(f"credential Secret for {harness} is {secrets.get(harness)!r}")

    references = [
        ("ServiceAccount", workers_namespace, f"{name}-worker"),
        ("ServiceAccount", namespace, f"{name}-supervisor"),
        ("PersistentVolumeClaim", workers_namespace, f"{name}-reference-cache"),
        ("PersistentVolumeClaim", namespace, f"{name}-artifacts"),
        ("Role", workers_namespace, f"{name}-supervisor"),
        ("ResourceQuota", workers_namespace, f"{name}-workers"),
    ]
    for kind, where, object_name in references:
        try:
            _find(docs, kind, where, object_name)
        except LookupError as missing:
            found.append(f"no {missing}")
    try:
        binding = _find(docs, "RoleBinding", workers_namespace, f"{name}-supervisor")
        subjects = binding.get("subjects", [])
        if subjects != [
            {"kind": "ServiceAccount", "name": f"{name}-supervisor", "namespace": namespace}
        ]:
            found.append(f"RoleBinding {name}-supervisor binds {subjects}")
    except LookupError as missing:
        found.append(f"no {missing}")
    for component in ("api", "supervisor"):
        try:
            spec = _pod_spec(_find(docs, "Deployment", namespace, f"{name}-{component}"))
        except LookupError as missing:
            found.append(f"no {missing}")
            continue
        pulls = [entry.get("name") for entry in spec.get("imagePullSecrets", [])]
        if pulls != ([pull_secret] if pull_secret else []):
            found.append(f"{component} pulls with {pulls}")
        if spec.get("serviceAccountName") != f"{name}-supervisor":
            found.append(f"{component} runs as {spec.get('serviceAccountName')}")
        for container in spec.get("containers", []):
            for source in container.get("envFrom", []):
                if source.get("configMapRef", {}).get("name") != f"{name}-settings":
                    found.append(f"{component} reads settings from {source}")
    return found


def _setting(text: str) -> tuple[str, str]:
    key, separator, value = text.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key, value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("render", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--workers-namespace", required=True)
    parser.add_argument("--setting", type=_setting, action="append", default=[])
    parser.add_argument("--pull-secret", default="")
    parser.add_argument("--buildkit", choices=("on", "off"))
    parser.add_argument("--buildkit-namespace", default=BUILDKIT_NAMESPACE)
    args = parser.parse_args()
    found = problems(
        _load(args.render),
        name=args.name,
        namespace=args.namespace,
        workers_namespace=args.workers_namespace,
        settings=dict(args.setting),
        pull_secret=args.pull_secret,
        buildkit=None if args.buildkit is None else args.buildkit == "on",
        buildkit_namespace=args.buildkit_namespace,
    )
    for problem in found:
        print(f"chart-flow: {args.render.name}: {problem}")
    if found:
        return 1
    print(f"chart-flow: {args.render.name}: names and settings follow the values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
