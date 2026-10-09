#!/bin/sh
set -eu
for tool in helm kubectl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "chart-sync-check: missing required tool: $tool" >&2; exit 2; }
done
# Guard the component layout before comparing the complete multi-document render.
"${UV:-uv}" run pytest tests/unit/test_chart_structure.py -q
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
kubectl kustomize deploy/kubernetes/base > "$tmp/kustomize.yaml"
helm template hades charts/hades -f tools/chart/values-base.yaml > "$tmp/helm.yaml"
"${UV:-uv}" run python tools/chart/sync.py "$tmp/kustomize.yaml" "$tmp/helm.yaml"
# The chart's own defaults are the same base with Hades's BuildKit left off
# (`buildkit.enabled: false`), and nothing else different.
helm template hades charts/hades > "$tmp/helm-default.yaml"
"${UV:-uv}" run python tools/chart/sync.py "$tmp/kustomize.yaml" "$tmp/helm-default.yaml" \
  --without-namespace hades-buildkit
