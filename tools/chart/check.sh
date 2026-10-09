#!/bin/sh
set -eu
for tool in helm kubeconform; do
  command -v "$tool" >/dev/null 2>&1 || { echo "chart: missing required tool: $tool" >&2; exit 2; }
done
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
helm lint charts/hades
helm template hades charts/hades > "$tmp/default.yaml"
helm template hades charts/hades -f charts/hades/values-lab-example.yaml > "$tmp/lab.yaml"
kubeconform -strict -summary "$tmp/default.yaml"
kubeconform -strict -summary "$tmp/lab.yaml"

