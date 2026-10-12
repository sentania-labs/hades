#!/bin/sh
set -eu
for tool in helm kubeconform; do
  command -v "$tool" >/dev/null 2>&1 || { echo "chart: missing required tool: $tool" >&2; exit 2; }
done
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
flow() { "${UV:-uv}" run python tools/chart/flow.py "$@"; }
helm lint charts/hades
helm lint charts/hades -f charts/hades/values-lab-example.yaml
helm template hades charts/hades > "$tmp/default.yaml"
helm template hades charts/hades -f charts/hades/values-lab-example.yaml > "$tmp/lab.yaml"
kubeconform -strict -summary "$tmp/default.yaml"
kubeconform -strict -summary "$tmp/lab.yaml"

# The defaults: the kustomize base's names (hades #609 step 2), BuildKit off.
flow "$tmp/default.yaml" --name hades --namespace hades \
  --workers-namespace hades-workers --buildkit off

# The lab example (hades #601): the product names, the cluster facts and the provider
# knobs reach the objects and the settings the service reads.
flow "$tmp/lab.yaml" --name hades --namespace hades --workers-namespace hades-workers \
  --buildkit off \
  --setting CRUCIBLE_KUBERNETES__CLUSTER_DNS_IP=10.43.0.10 \
  --setting CRUCIBLE_SERVICE__RENDER_TIMEZONE=America/Chicago \
  --setting CRUCIBLE_KUBERNETES__STORAGE_CLASS=longhorn \
  --setting CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_NAMESPACE=litellm \
  --setting 'CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_POD_LABELS={"app.kubernetes.io/name":"litellm"}' \
  --setting CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_PORT=4000 \
  --setting 'CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_CIDRS=[]' \
  --setting CRUCIBLE_KUBERNETES__MAX_CONCURRENCY=6

# Every override at once: a workers namespace that is not the default suffix, BuildKit
# on, the optional settings, a pull Secret and an extra setting that replaces a chart one.
helm template hades charts/hades \
  --set nameOverride=demo \
  --set namespaceOverride=demo-control \
  --set workersNamespaceOverride=demo-attempts \
  --set buildkit.enabled=true \
  --set storage.buildkitCacheStorageClass=demo-rwo \
  --set cluster.podPidLimitOverride=4096 \
  --set cluster.imagePullSecret=demo-pull \
  --set-json 'cluster.localEndpoint.cidrs=["192.0.2.0/24"]' \
  --set-string extraSettings.CRUCIBLE_SUPERVISOR__HOLDER=demo-kubernetes \
  --set-string extraSettings.CRUCIBLE_SERVICE__LOG_LEVEL=DEBUG > "$tmp/overrides.yaml"
kubeconform -strict -summary "$tmp/overrides.yaml"
flow "$tmp/overrides.yaml" --name demo --namespace demo-control \
  --workers-namespace demo-attempts --buildkit on --pull-secret demo-pull \
  --setting CRUCIBLE_KUBERNETES__POD_PID_LIMIT_OVERRIDE=4096 \
  --setting CRUCIBLE_KUBERNETES__IMAGE_PULL_SECRET=demo-pull \
  --setting 'CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_CIDRS=["192.0.2.0/24"]' \
  --setting CRUCIBLE_SUPERVISOR__HOLDER=demo-kubernetes \
  --setting CRUCIBLE_SERVICE__LOG_LEVEL=DEBUG

# A deployment installed with the earlier defaults keeps every name by setting them
# (docs/deployment.md, "Upgrading from crucible names").
helm template hades charts/hades \
  --set nameOverride=crucible \
  --set namespaceOverride=crucible \
  --set workersNamespaceOverride=crucible-workers \
  --set buildkit.enabled=true \
  --set buildkit.namespace=crucible-buildkit \
  --set postgres.databaseSecretName=crucible-database \
  --set secrets.githubApp=crucible-github-app \
  --set secrets.firstRunToken=crucible-first-run-admin > "$tmp/crucible.yaml"
kubeconform -strict -summary "$tmp/crucible.yaml"
flow "$tmp/crucible.yaml" --name crucible --namespace crucible \
  --workers-namespace crucible-workers --buildkit on --buildkit-namespace crucible-buildkit \
  --setting CRUCIBLE_KUBERNETES__FIRST_RUN_SECRET_NAME=crucible-first-run-admin \
  --setting CRUCIBLE_GITHUB__APP__SECRET_NAME=crucible-github-app

# The schema refuses a pinned tag without a digest, and a setting that is not a string.
if helm template hades charts/hades --set serviceImage.tag=0.12.0 >/dev/null 2>&1; then
  echo "chart: a tag other than latest rendered without a digest" >&2
  exit 1
fi
if helm template hades charts/hades --set extraSettings.CRUCIBLE_SERVICE__LOG_LEVEL=1 >/dev/null 2>&1; then
  echo "chart: an extraSettings value that is not a string rendered" >&2
  exit 1
fi
# BuildKit's privileged namespace is refused when it is the service's or the workers'
# namespace, which would render that Namespace a second time at `privileged`.
for collision in hades hades-workers; do
  if helm template hades charts/hades --set buildkit.enabled=true \
      --set buildkit.namespace="$collision" >/dev/null 2>&1; then
    echo "chart: buildkit.namespace $collision, a namespace the chart already creates, rendered" >&2
    exit 1
  fi
done
if helm template hades charts/hades --set buildkit.enabled=true \
    --set namespaceOverride=demo --set buildkit.namespace=demo >/dev/null 2>&1; then
  echo "chart: buildkit.namespace equal to namespaceOverride rendered" >&2
  exit 1
fi
# Overrides too long for the names they compose are refused; a dotted pull Secret is not.
long_name=$(printf 'n%.0s' $(seq 44))
long_namespace=$(printf 's%.0s' $(seq 56))
if helm template hades charts/hades --set nameOverride="$long_name" >/dev/null 2>&1; then
  echo "chart: a nameOverride longer than 43 characters rendered" >&2
  exit 1
fi
if helm template hades charts/hades --set namespaceOverride="$long_namespace" >/dev/null 2>&1; then
  echo "chart: a namespaceOverride too long for <namespace>-workers rendered" >&2
  exit 1
fi
helm template hades charts/hades --set namespaceOverride="$long_namespace" \
  --set workersNamespaceOverride=demo-workers >/dev/null
helm template hades charts/hades --set cluster.imagePullSecret=ghcr.io-pull > "$tmp/pull.yaml"
kubeconform -strict -summary "$tmp/pull.yaml"
flow "$tmp/pull.yaml" --name hades --namespace hades \
  --workers-namespace hades-workers --buildkit off --pull-secret ghcr.io-pull \
  --setting CRUCIBLE_KUBERNETES__IMAGE_PULL_SECRET=ghcr.io-pull

# Exercise independent credential mounts and external database configuration as well.
helm template hades charts/hades --is-upgrade \
  --set postgres.bundled=false \
  --set secrets.githubApp=lab-app \
  --set secrets.githubAppPrivateKey=lab-key \
  --set secrets.githubAppWebhook=lab-webhook \
  --set secrets.firstRunToken=lab-bootstrap > "$tmp/custom.yaml"
kubeconform -strict -summary "$tmp/custom.yaml"
