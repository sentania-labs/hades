#!/usr/bin/env bash
# The kind proof for issue 93 (contract FDY-0107, requirement 8): the real
# crucible-workers ResourceQuota (deploy/kubernetes/base/workers/resourcequota.yaml,
# requests.cpu 3, max_concurrency 3) refuses two concurrent pods of the old shape
# (request equal to the 2-CPU limit, 4 CPU total) and admits three concurrent pods of
# the new shape (request half the limit, 3 CPU total: the exact concurrency the base
# quota is sized for).
#
# A disposable kind cluster, unique name, deleted on exit even on failure (sdlc skill's
# kind pattern). No Calico, no registry, no Crucible process: this proof is about the
# rendered ResourceQuota and one container's `resources` block, not the whole provider.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=tools/kind/cluster.sh
. "$root/tools/kind/cluster.sh"
run_id=${CRUCIBLE_KIND_RUN_ID:-$(date +%s)-$$}
cluster="crucible-req-${run_id}"
namespace="crucible-verify"
cluster_created=0
scratch=$(mktemp -d -t crucible-verify.XXXXXX)

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$cluster_created" -eq 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || :
  fi
  rm -rf "$scratch"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

crucible_kind_docker_shim "$scratch"

echo "verify: creating kind cluster $cluster"
kind create cluster --name "$cluster" --wait 0s
cluster_created=1
kubectl wait --for=condition=Ready nodes --all --timeout=180s

echo "verify: pulling busybox into the daemon, then loading it into the cluster so scheduling is the only thing tested"
busybox=$(crucible_kind_pull "$CRUCIBLE_BUSYBOX_IMAGE" 2>/dev/null) || busybox="$CRUCIBLE_BUSYBOX_IMAGE"
kind load docker-image "$busybox" --name "$cluster"

kubectl create namespace "$namespace"
kubectl -n "$namespace" create serviceaccount crucible-worker

echo "verify: applying the real base ResourceQuota, renamespaced to $namespace"
"${UV:-uv}" run python - "$root/deploy/kubernetes/base/workers" "$namespace" <<'PY' > "$scratch/quota.yaml"
import subprocess
import sys

import yaml

base, namespace = sys.argv[1], sys.argv[2]
rendered = subprocess.run(
    ["kubectl", "kustomize", base], check=True, capture_output=True, text=True
).stdout
for doc in yaml.safe_load_all(rendered):
    if doc and doc.get("kind") == "ResourceQuota":
        doc["metadata"]["namespace"] = namespace
        print(yaml.safe_dump(doc))
PY
cat "$scratch/quota.yaml"
kubectl apply -f "$scratch/quota.yaml"
requests_cpu=$("${UV:-uv}" run python -c "
import yaml
print(yaml.safe_load(open('$scratch/quota.yaml'))['spec']['hard']['requests.cpu'])
")
echo "verify: quota's requests.cpu is $requests_cpu (base/workers/resourcequota.yaml)"

for n in 1 2; do
  "${UV:-uv}" run python "$root/tools/kind/render_request_pod.py" \
    --name "old-shape-$n" --namespace "$namespace" --cpu-request-fraction 1.0 \
    > "$scratch/old-$n.json"
done
for n in 1 2 3; do
  "${UV:-uv}" run python "$root/tools/kind/render_request_pod.py" \
    --name "new-shape-$n" --namespace "$namespace" --cpu-request-fraction 0.5 \
    > "$scratch/new-$n.json"
done

echo "verify: applying two old-shape pods (2 CPU request each, 4 CPU total against a $requests_cpu CPU quota)"
kubectl apply -f "$scratch/old-1.json"
if kubectl apply -f "$scratch/old-2.json" 2>"$scratch/old-2.err"; then
  echo "verify: FAIL: the second old-shape pod was admitted; it should have exceeded the quota" >&2
  exit 1
fi
echo "verify: second old-shape pod refused, as expected:"
cat "$scratch/old-2.err"
if ! grep -q "exceeded quota" "$scratch/old-2.err"; then
  echo "verify: FAIL: the refusal was not the quota (see above)" >&2
  exit 1
fi
kubectl -n "$namespace" delete pod old-shape-1 --now >/dev/null

echo "verify: applying three new-shape pods (1 CPU request each, 3 CPU total: max_concurrency)"
for n in 1 2 3; do
  kubectl apply -f "$scratch/new-$n.json"
done
for n in 1 2 3; do
  kubectl -n "$namespace" wait --for=condition=Ready "pod/new-shape-$n" --timeout=60s
done
kubectl -n "$namespace" get pods -o wide

echo "verify: PASS: under the real base ResourceQuota, the old (request == limit) shape"
echo "verify: cannot run two concurrent attempts, and the new (request < limit) shape"
echo "verify: runs three, the base's own max_concurrency."
