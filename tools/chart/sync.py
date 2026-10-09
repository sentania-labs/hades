#!/usr/bin/env python3
"""Compare Helm and kustomize objects after removing renderer metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def objects(path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    result = {}
    for item in yaml.safe_load_all(path.read_text()):
        if not item:
            continue
        metadata = item.get("metadata", {})
        key = (item["kind"], metadata.get("namespace", ""), metadata["name"])
        metadata.pop("creationTimestamp", None)
        labels = metadata.get("labels", {})
        for name in list(labels):
            if name.startswith("helm.sh/") or name.startswith("app.kubernetes.io/managed-by"):
                labels.pop(name)
        if item["kind"] == "ConfigMap" and metadata["name"] == "crucible-settings":
            data = item.get("data", {})
            for name in list(data):
                if name.startswith("CRUCIBLE_ROOMS__") or name in {
                    "CRUCIBLE_KUBERNETES__IMAGE_REPOSITORIES",
                    "CRUCIBLE_KUBERNETES__PROBE_IMAGE",
                }:
                    data.pop(name)
        _normalise_latest_images(item)
        result[key] = item
    return result


def _normalise_latest_images(value: Any) -> None:
    if isinstance(value, dict):
        image = value.get("image")
        if isinstance(image, str) and "/" in image and ":" not in image.rsplit("/", 1)[-1]:
            value["image"] = f"{image}:latest"
        for child in value.values():
            _normalise_latest_images(child)
    elif isinstance(value, list):
        for child in value:
            _normalise_latest_images(child)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kustomize", type=Path)
    parser.add_argument("helm", type=Path)
    args = parser.parse_args()
    left, right = objects(args.kustomize), objects(args.helm)
    if left == right:
        print(f"chart-sync-check: {len(left)} objects match")
        return 0
    all_keys = sorted(set(left) | set(right))
    for key in all_keys:
        if left.get(key) != right.get(key):
            print(f"chart-sync-check: drift in {'/'.join(key)}")
            print("kustomize:", json.dumps(left.get(key), sort_keys=True, indent=2))
            print("helm:", json.dumps(right.get(key), sort_keys=True, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
