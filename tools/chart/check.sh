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

# Exercise independent credential mounts and external database configuration as well.
helm template hades charts/hades --is-upgrade \
  --set postgres.bundled=false \
  --set secrets.githubApp=lab-app \
  --set secrets.githubAppPrivateKey=lab-key \
  --set secrets.githubAppWebhook=lab-webhook \
  --set secrets.firstRunToken=lab-bootstrap > "$tmp/custom.yaml"
kubeconform -strict -summary "$tmp/custom.yaml"
