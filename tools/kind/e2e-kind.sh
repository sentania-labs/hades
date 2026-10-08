#!/usr/bin/env bash
# Build a disposable kind cluster and run the Kubernetes provider tier (18, 26).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=tools/kind/cluster.sh
. "$root/tools/kind/cluster.sh"
if [ -n "${CRUCIBLE_E2E_KIND_SHARD:-}" ] && [ ! -f "$root/tools/kind/shards/${CRUCIBLE_E2E_KIND_SHARD}.txt" ]; then
  echo "e2e-kind: unknown shard ${CRUCIBLE_E2E_KIND_SHARD}" >&2
  exit 2
fi
run_id=${CRUCIBLE_KIND_RUN_ID:-$(date +%s)-$$}
cluster="crucible-e2e-${run_id}"
registry="${cluster}-registry"
scratch=$(mktemp -d -t crucible-kind.XXXXXX)
kubeconfig="$scratch/kubeconfig"
cache="$scratch/reference-cache"
registry_ref=""
worker_registry_ref=""
cluster_created=0
registry_started=0
tag_created=0
worker_tag_created=0
# Set by crucible_kind_start_registry the moment it creates the `kind` network, so cleanup
# sees it even when the registry's own `docker run` fails right after (77).
CRUCIBLE_KIND_NETWORK_CREATED=0

cleanup() {
  status=$?
  cleanup_failed=0
  # A second interrupt during cleanup must not abort it partway: `trap -` would
  # restore the default action (immediate termination), which is exactly what a
  # second Ctrl-C during a slow `kind delete cluster` would do (77). Ignoring the
  # signals here, not resetting them, is what lets cleanup run to completion.
  trap '' EXIT HUP INT TERM
  if [ "$status" -ne 0 ]; then
    "$root/tools/kind/dump.sh" "$kubeconfig" "$scratch/canary.log" || cleanup_failed=1
  fi
  # An inspect that errors can mean either "confirmed absent" or "the daemon
  # can't answer" (for example it died mid-cleanup); those are not the same
  # thing, and the busybox pull below no longer fails a green run on its own
  # (77), so it can't be relied on as the daemon-health signal either. Check
  # `docker info` explicitly whenever an inspect comes back negative, and
  # only call the resource confirmed gone when the daemon is still reachable.
  if [ "$cluster_created" -eq 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || :
    if kind get clusters 2>/dev/null | grep -Fxq "$cluster"; then
      echo "e2e-kind cleanup: cluster $cluster remains" >&2
      cleanup_failed=1
    elif ! docker info >/dev/null 2>&1; then
      echo "e2e-kind cleanup: docker daemon unreachable, cannot confirm cluster $cluster removed" >&2
      cleanup_failed=1
    fi
  fi
  if [ "$registry_started" -eq 1 ]; then
    docker rm -f "$registry" >/dev/null 2>&1 || :
    if docker inspect "$registry" >/dev/null 2>&1; then
      echo "e2e-kind cleanup: registry $registry remains" >&2
      cleanup_failed=1
    elif ! docker info >/dev/null 2>&1; then
      echo "e2e-kind cleanup: docker daemon unreachable, cannot confirm registry $registry removed" >&2
      cleanup_failed=1
    fi
  fi
  if [ "$tag_created" -eq 1 ]; then
    docker image rm "$registry_ref" >/dev/null 2>&1 || :
    if docker image inspect "$registry_ref" >/dev/null 2>&1; then
      echo "e2e-kind cleanup: image tag $registry_ref remains" >&2
      cleanup_failed=1
    elif ! docker info >/dev/null 2>&1; then
      echo "e2e-kind cleanup: docker daemon unreachable, cannot confirm image tag $registry_ref removed" >&2
      cleanup_failed=1
    fi
  fi
  if [ "$worker_tag_created" -eq 1 ]; then
    docker image rm "$worker_registry_ref" >/dev/null 2>&1 || :
    if docker image inspect "$worker_registry_ref" >/dev/null 2>&1; then
      echo "e2e-kind cleanup: image tag $worker_registry_ref remains" >&2
      cleanup_failed=1
    elif ! docker info >/dev/null 2>&1; then
      echo "e2e-kind cleanup: docker daemon unreachable, cannot confirm image tag $worker_registry_ref removed" >&2
      cleanup_failed=1
    fi
  fi
  if [ "${CRUCIBLE_KIND_NETWORK_CREATED:-0}" -eq 1 ]; then
    # The name is shared by every kind cluster on the host; `network rm` on one a
    # concurrent run still holds fails harmlessly (Docker refuses while an endpoint is
    # attached), so this only ever removes what became ours to remove.
    docker network rm kind >/dev/null 2>&1 || :
    if docker network inspect kind >/dev/null 2>&1; then
      echo "e2e-kind cleanup: network kind remains (a concurrent kind cluster may hold it)" >&2
    fi
  fi
  # The cleanup helper is pulled the same way as the tier's other images (68). If no
  # source answers, the chmod does not run, which is not itself a leak: the scratch
  # path's removal is checked on its own right after, so a root-owned file left behind
  # still fails the run there, and an unreachable registry no longer fails an
  # otherwise clean one (77).
  busybox=$(crucible_kind_pull "$CRUCIBLE_BUSYBOX_IMAGE" 2>/dev/null) || busybox=$CRUCIBLE_BUSYBOX_IMAGE
  docker run --rm -v "$scratch:/cleanup" "$busybox" \
    chmod -R a+rwX /cleanup >/dev/null 2>&1 || :
  rm -rf "$scratch" || cleanup_failed=1
  if [ -e "$scratch" ]; then
    echo "e2e-kind cleanup: scratch path $scratch remains" >&2
    cleanup_failed=1
  fi
  if [ "$cleanup_failed" -ne 0 ] && [ "$status" -eq 0 ]; then
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

# The cleanup test sources this file up to here, with a stubbed docker on PATH, to
# exercise `cleanup` against fake resource state without a real cluster or registry.
if [ "${CRUCIBLE_KIND_TEST_HOOK:-0}" = "1" ]; then
  return 0
fi

crucible_kind_docker_shim "$scratch"

# The provider resolves the tier's registry tag with the same crane the service image
# ships (108), fetched and checked against the Dockerfile's pin.
crane_bin=$("$root/tools/crane/fetch.sh")
PATH="$(dirname "$crane_bin"):$PATH"
export PATH

mkdir -p "$cache"
chmod 0777 "$cache"

worker_image=${CRUCIBLE_E2E_IMAGE:-$(
  awk -F= '$1 == "SCRIPT_HARNESS" {print $2}' "$root/images/manifest.env"
)}
if ! docker image inspect "$worker_image" >/dev/null 2>&1; then
  echo "e2e-kind: images/manifest.env pins $worker_image but the host daemon lacks it" >&2
  echo "e2e-kind: run 'make e2e-image' first" >&2
  exit 2
fi
# hades #184: CRUCIBLE_E2E_KIND_WORKER_IMAGE names the combined worker image as well
# (`make e2e-kind-self-hosting` passes the WORKER tag images/manifest.env pins), for a
# case that runs a real harness and the verifier in the image the lab runs.
combined_image=${CRUCIBLE_E2E_KIND_WORKER_IMAGE:-}
if [ -n "$combined_image" ] && ! docker image inspect "$combined_image" >/dev/null 2>&1; then
  echo "e2e-kind: CRUCIBLE_E2E_KIND_WORKER_IMAGE is $combined_image but the host daemon lacks it" >&2
  echo "e2e-kind: run 'make images' first" >&2
  exit 2
fi

registry_started=1
crucible_kind_start_registry "$registry"
registry_port=$CRUCIBLE_KIND_REGISTRY_PORT
registry_ref="localhost:${registry_port}/crucible-worker:${run_id}"
crucible_kind_await_registry "http://127.0.0.1:${registry_port}"
tag_created=1
docker tag "$worker_image" "$registry_ref"
docker push "$registry_ref" >/dev/null
if [ -n "$combined_image" ]; then
  worker_registry_ref="localhost:${registry_port}/crucible-worker:worker-${run_id}"
  worker_tag_created=1
  docker tag "$combined_image" "$worker_registry_ref"
  docker push "$worker_registry_ref" >/dev/null
fi

cat > "$scratch/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: $cluster
networking:
  disableDefaultCNI: true
  podSubnet: 10.244.0.0/16
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: $cache
        containerPath: /crucible-kind-cache
kubeadmConfigPatches:
  - |
    kind: KubeletConfiguration
    podPidsLimit: 512
containerdConfigPatches:
  - |-
    [plugins."io.containerd.grpc.v1.cri".registry.mirrors."localhost:${registry_port}"]
      endpoint = ["http://${registry}:5000"]
    ${CRUCIBLE_KIND_DOCKER_HUB_MIRROR_PATCH}
EOF

# Readiness gate wrapper: calls the gate, and on failure tears down the cluster,
# recreates it, re-runs the full setup, and retries the gate once.
# If both attempts fail, returns 1 so the caller exits with code 75.
crucible_kind_wait_and_retry() {
  local attempt=0
  while [ "$attempt" -lt 2 ]; do
    if [ "$attempt" -gt 0 ]; then
      echo "e2e-kind: recreating cluster (attempt 2/2)" >&2
      kind delete cluster --name "$cluster" >/dev/null 2>&1 || :
      node_image=$(crucible_kind_pull "$CRUCIBLE_KIND_NODE_IMAGE")
      # Marked created before the create call so the EXIT trap deletes a cluster whose
      # later setup step fails (Codex review of PR 326).
      cluster_created=1
      kind create cluster --config "$scratch/kind.yaml" --kubeconfig "$kubeconfig" --wait 0s \
        --image "$node_image"
      node="${cluster}-control-plane"
      for address in 10.0.0.1 172.16.0.1 192.168.0.1 100.64.0.1 169.254.169.254; do
        docker exec "$node" ip address add "$address/32" dev lo
      done
      for address in 198.51.100.10 198.51.100.20; do
        docker exec "$node" ip address add "$address/32" dev lo
      done
      docker exec "$node" ip route add blackhole 198.51.100.30/32
      crucible_kind_install_calico "$scratch" "$kubeconfig"
      kind load docker-image "$worker_image" --name "$cluster"
      cluster_created=1
      # On retry we must redo the cluster setup that was already done on the
      # first pass (namespaces, RBAC, CoreDNS tuning).  These commands are
      # idempotent so we can re-run them safely.
      KUBECONFIG="$kubeconfig" kubectl create namespace crucible >/dev/null || :
      KUBECONFIG="$kubeconfig" kubectl -n crucible create serviceaccount crucible-supervisor >/dev/null || :
      KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kind/workers.yaml" >/dev/null || :
      KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kubernetes/base/workers/role.yaml" >/dev/null || :
      KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kubernetes/base/workers/rolebinding.yaml" >/dev/null || :
      supervisor_kubeconfig="$scratch/supervisor-kubeconfig"
      api_server=$(KUBECONFIG="$kubeconfig" kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
      ca_data=$(KUBECONFIG="$kubeconfig" kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
      supervisor_token=$(KUBECONFIG="$kubeconfig" kubectl -n crucible create token crucible-supervisor)
      ca_file="$scratch/cluster-ca.crt"
      printf '%s' "$ca_data" | base64 -d > "$ca_file"
      KUBECONFIG="$supervisor_kubeconfig" kubectl config set-cluster kind \
        --server="$api_server" --certificate-authority="$ca_file" --embed-certs=true >/dev/null
      KUBECONFIG="$supervisor_kubeconfig" kubectl config set-credentials crucible-supervisor \
        --token="$supervisor_token" >/dev/null
      KUBECONFIG="$supervisor_kubeconfig" kubectl config set-context crucible-supervisor \
        --cluster=kind --user=crucible-supervisor --namespace=crucible-workers >/dev/null
      KUBECONFIG="$supervisor_kubeconfig" kubectl config use-context crucible-supervisor >/dev/null
      corefile=$(KUBECONFIG="$kubeconfig" kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}')
      corefile=$(printf '%s\n' "$corefile" | awk '
        { print }
        /^[[:space:]]*ready[[:space:]]*$/ && !done {
          print "    hosts {"
          print "       198.51.100.20 github.com api.github.com"
          print "       fallthrough"
          print "    }"
          done = 1
        }')
      KUBECONFIG="$kubeconfig" kubectl -n kube-system create configmap coredns \
        --from-literal=Corefile="$corefile" --dry-run=client -o yaml \
        | KUBECONFIG="$kubeconfig" kubectl apply -f - >/dev/null || :
      KUBECONFIG="$kubeconfig" kubectl -n kube-system patch deployment coredns --type=json -p='[
        {"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"wrong-port-www","emptyDir":{}}},
        {"op":"add","path":"/spec/template/spec/containers/-","value":{"name":"wrong-port-http","image":"busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0","command":["sh","-c","mkdir -p /www; echo dns-wrong-port > /www/index.html; exec httpd -f -p 18080 -h /www"],"securityContext":{"allowPrivilegeEscalation":false,"readOnlyRootFilesystem":true,"runAsNonRoot":true,"runAsUser":1000,"capabilities":{"drop":["ALL"]}},"volumeMounts":[{"name":"wrong-port-www","mountPath":"/www"}]}}
      ]' >/dev/null || :
      KUBECONFIG="$kubeconfig" kubectl -n kube-system patch service kube-dns --type=json \
        -p='[{"op":"add","path":"/spec/ports/-","value":{"name":"wrong-port","port":443,"protocol":"TCP","targetPort":18080}}]' >/dev/null || :
      KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status deployment/coredns --timeout=180s
      KUBECONFIG="$kubeconfig" kubectl -n kube-system wait --for=condition=Ready pod/range-http --timeout=90s
      KUBECONFIG="$kubeconfig" kubectl -n crucible-kind-peer wait --for=condition=Ready pod/peer-http --timeout=90s
      KUBECONFIG="$kubeconfig" kubectl -n crucible-workers wait \
        --for=jsonpath='{.status.phase}'=Bound pvc/crucible-reference-cache --timeout=90s
    fi
    if crucible_kind_wait_ready "$kubeconfig"; then
      return 0
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

cluster_created=1
node_image=$(crucible_kind_pull "$CRUCIBLE_KIND_NODE_IMAGE")
kind create cluster --config "$scratch/kind.yaml" --kubeconfig "$kubeconfig" --wait 0s \
  --image "$node_image"

node="${cluster}-control-plane"
for address in 10.0.0.1 172.16.0.1 192.168.0.1 100.64.0.1 169.254.169.254; do
  docker exec "$node" ip address add "$address/32" dev lo
done
# hades #189, #190, #191: a stand-in github.com on public-shaped documentation addresses
# (RFC 5737), which no rule of 26 denies. range-http serves the tier's bare repositories
# over plain HTTP on 443 at every node address, so 198.51.100.10 and 198.51.100.20 both
# answer as the git host; cluster DNS says github.com is .20 (the CoreDNS patch below),
# which is the pod-side lookup a policy written for .10 does not permit. 198.51.100.30
# is a blackhole route: a git host that never answers, for a prepare that hangs. A route,
# not a filter rule, so no CNI chain can accept the packet before it is dropped.
for address in 198.51.100.10 198.51.100.20; do
  docker exec "$node" ip address add "$address/32" dev lo
done
docker exec "$node" ip route add blackhole 198.51.100.30/32

crucible_kind_install_calico "$scratch" "$kubeconfig"

# Cluster setup: namespaces, RBAC, CoreDNS tuning (189, 190, 191).
KUBECONFIG="$kubeconfig" kubectl create namespace crucible >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n crucible create serviceaccount crucible-supervisor >/dev/null
KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kind/workers.yaml" >/dev/null
# The tier consumes the deployed RBAC files directly. A provider change needing another
# verb therefore exercises the same Role and RoleBinding that the deployment uses.
KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kubernetes/base/workers/role.yaml" >/dev/null
KUBECONFIG="$kubeconfig" kubectl apply -f "$root/deploy/kubernetes/base/workers/rolebinding.yaml" >/dev/null
supervisor_kubeconfig="$scratch/supervisor-kubeconfig"
api_server=$(KUBECONFIG="$kubeconfig" kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
ca_data=$(KUBECONFIG="$kubeconfig" kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
supervisor_token=$(KUBECONFIG="$kubeconfig" kubectl -n crucible create token crucible-supervisor)
ca_file="$scratch/cluster-ca.crt"
printf '%s' "$ca_data" | base64 -d > "$ca_file"
KUBECONFIG="$supervisor_kubeconfig" kubectl config set-cluster kind \
  --server="$api_server" --certificate-authority="$ca_file" --embed-certs=true >/dev/null
KUBECONFIG="$supervisor_kubeconfig" kubectl config set-credentials crucible-supervisor \
  --token="$supervisor_token" >/dev/null
KUBECONFIG="$supervisor_kubeconfig" kubectl config set-context crucible-supervisor \
  --cluster=kind --user=crucible-supervisor --namespace=crucible-workers >/dev/null
KUBECONFIG="$supervisor_kubeconfig" kubectl config use-context crucible-supervisor >/dev/null
corefile=$(KUBECONFIG="$kubeconfig" kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}')
corefile=$(printf '%s\n' "$corefile" | awk '
  { print }
  /^[[:space:]]*ready[[:space:]]*$/ && !done {
    print "    hosts {"
    print "       198.51.100.20 github.com api.github.com"
    print "       fallthrough"
    print "    }"
    done = 1
  }')
KUBECONFIG="$kubeconfig" kubectl -n kube-system create configmap coredns \
  --from-literal=Corefile="$corefile" --dry-run=client -o yaml \
  | KUBECONFIG="$kubeconfig" kubectl apply -f - >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n kube-system patch deployment coredns --type=json -p='[
  {"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"wrong-port-www","emptyDir":{}}},
  {"op":"add","path":"/spec/template/spec/containers/-","value":{"name":"wrong-port-http","image":"busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0","command":["sh","-c","mkdir -p /www; echo dns-wrong-port > /www/index.html; exec httpd -f -p 18080 -h /www"],"securityContext":{"allowPrivilegeEscalation":false,"readOnlyRootFilesystem":true,"runAsNonRoot":true,"runAsUser":1000,"capabilities":{"drop":["ALL"]}},"volumeMounts":[{"name":"wrong-port-www","mountPath":"/www"}]}}
]' >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n kube-system patch service kube-dns --type=json \
  -p='[{"op":"add","path":"/spec/ports/-","value":{"name":"wrong-port","port":443,"protocol":"TCP","targetPort":18080}}]' >/dev/null
KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status deployment/coredns --timeout=180s
KUBECONFIG="$kubeconfig" kubectl -n kube-system wait --for=condition=Ready pod/range-http --timeout=90s
KUBECONFIG="$kubeconfig" kubectl -n crucible-kind-peer wait --for=condition=Ready pod/peer-http --timeout=90s
KUBECONFIG="$kubeconfig" kubectl -n crucible-workers wait \
  --for=jsonpath='{.status.phase}'=Bound pvc/crucible-reference-cache --timeout=90s

# Required even though the real registry path below is what proves resolution and pull.
kind load docker-image "$worker_image" --name "$cluster"

# hades #508 (finding Q3VFH6VXPNWC9BC2H): start the watchdog *before* the readiness
# gate so the GitHub Actions 20-minute ceiling counts from the same point as this
# script.  We subtract the elapsed wall-clock time so the watchdog still fires 18
# minutes after the *script* began, not 18 minutes after cluster setup finished.
_watchdog_start_time=${SECONDS:-0}

# Run the readiness gate; retry once by recreating the cluster. The call sits inside
# the conditional on purpose: under set -e a bare call would abort the script before
# the infrastructure exit below (Codex review of PR 326).
if ! crucible_kind_wait_and_retry; then
  echo "kind cluster not healthy (infrastructure)" >&2
  exit 75
fi

dns_ip=$(KUBECONFIG="$kubeconfig" kubectl -n kube-system get service kube-dns -o jsonpath='{.spec.clusterIP}')
peer_ip=$(KUBECONFIG="$kubeconfig" kubectl -n crucible-kind-peer get service peer-http -o jsonpath='{.spec.clusterIP}')
api_ip=$(KUBECONFIG="$kubeconfig" kubectl -n default get service kubernetes -o jsonpath='{.spec.clusterIP}')

export CRUCIBLE_E2E_KIND=1
export CRUCIBLE_E2E_KIND_CACHE="$cache"
export CRUCIBLE_E2E_KIND_CLUSTER="$cluster"
export CRUCIBLE_E2E_KIND_DNS_IP="$dns_ip"
export CRUCIBLE_E2E_KIND_PEER_IP="$peer_ip"
export CRUCIBLE_E2E_KIND_API_IP="$api_ip"
export CRUCIBLE_E2E_KIND_REGISTRY="$registry_ref"
export CRUCIBLE_E2E_KIND_WORKER_REGISTRY="$worker_registry_ref"
export CRUCIBLE_E2E_KIND_KUBECONFIG="$supervisor_kubeconfig"
export CRUCIBLE_E2E_KIND_CANARY_LOG="$scratch/canary.log"
export CRUCIBLE_E2E_DOCKER_SOCKET="${CRUCIBLE_E2E_DOCKER_SOCKET:-/var/run/docker.sock}"
export KUBECONFIG="$kubeconfig"

cd "$root"
uv sync --frozen --quiet
# CRUCIBLE_E2E_KIND_PYTEST_ARGS narrows the run (for example `-k login -s`) when one
# case is being proven on its own cluster; CI leaves it empty and runs the whole tier.
read -r -a extra_args <<< "${CRUCIBLE_E2E_KIND_PYTEST_ARGS:-}"
# CRUCIBLE_E2E_KIND_TESTS picks the file; the default is the tier CI runs. CI runs the
# tier as three shards (hades #222): CRUCIBLE_E2E_KIND_SHARD=N selects the node ids
# listed in tools/kind/shards/N.txt, one per line, each on its own cluster. An unknown
# shard is refused before anything is built. tests/unit/test_kind_shards.py proves every
# test is in exactly one list. --durations=0 puts each test's time in the job log.
selection=()
if [ -n "${CRUCIBLE_E2E_KIND_SHARD:-}" ]; then
  shard_file="$root/tools/kind/shards/${CRUCIBLE_E2E_KIND_SHARD}.txt"
  if [ ! -f "$shard_file" ]; then
    echo "e2e-kind: unknown shard ${CRUCIBLE_E2E_KIND_SHARD} (no $shard_file)" >&2
    exit 2
  fi
  while IFS= read -r node_id; do
    [ -n "$node_id" ] && selection+=("$node_id")
  done < "$shard_file"
else
  selection=("${CRUCIBLE_E2E_KIND_TESTS:-tests/e2e/test_kind.py}")
fi

# hades #508: watchdog before the GitHub Actions `timeout-minutes: 20` ceiling.
# Kills pytest with SIGABRT (faulthandler dump of all Python stacks) at 18 minutes,
# two minutes before the CI cancellation.  The marker file lets the EXIT trap know
# it must call dump.sh; the dump script already prints cluster state on non-zero exit.
#
# (finding Q3VFH6VXPNWC9BC2H) Adjust the timeout so the watchdog fires at 18 minutes
# from the *start* of this script, not from after cluster setup.  Subtract the wall
# clock time already spent so the background timer still fires at the right moment.
_elapsed_setup=$(( SECONDS - _watchdog_start_time ))
_watchdog_remaining=$(( ${CRUCIBLE_KIND_WATCHDOG_SECONDS:-1080} - _elapsed_setup ))
if [ "$_watchdog_remaining" -lt 60 ]; then
  _watchdog_remaining=60
fi
WATCHDOG_TIMEOUT=${CRUCIBLE_KIND_WATCHDOG_SECONDS:-$_watchdog_remaining}
WATCHDOG_MARKER="$scratch/watchdog.marker"
export CRUCIBLE_KIND_WATCHDOG_MARKER="$WATCHDOG_MARKER"
shard_label=${CRUCIBLE_E2E_KIND_SHARD:-whole}
watchdog_pid=
pytest_pid=

# (finding 3T8YNPPDCHQ1SX9F48 / 3Z5AYT8CSQ0QTY81WZ) Start the watchdog before running
# pytest so the test runs under watch, and capture the pytest process PID so the
# watchdog can target Python's faulthandler instead of the parent shell.
if [ -f "$root/tools/kind/watchdog.sh" ]; then
    watchdog_pid=$(bash "$root/tools/kind/watchdog.sh" "$WATCHDOG_TIMEOUT" "$WATCHDOG_MARKER" "") || true
fi

# Run pytest in the foreground and capture its PID.  The watchdog already has a
# reference (it will be filled below) so the SIGABRT reaches Python, not bash.
# (finding 3XW34RMPY575HQW7DD)  Use `set +e` so a non-zero pytest exit still lets the
# caller reach the marker check and watchdog kill below; `set -e` would abort here.
set +e
uv run pytest "${selection[@]}" -q -m e2e --durations=0 "${extra_args[@]}" &
pytest_pid=$!
pytest_exit=0
wait "$pytest_pid" || pytest_exit=$?
set -e

# If pytest exited because of a watchdog SIGABRT, the process is gone and pytest_exit
# is the signal; if it exited clean but the watchdog marker appeared, the watchdog fired
# and pytest was killed by our parent (the shell), so the marker is what we honour.
if [ -n "$watchdog_pid" ] && [ -f "$WATCHDOG_MARKER" ]; then
    echo "e2e-kind: shard timed out after ${WATCHDOG_TIMEOUT}s (watchdog fired)" >&2
    pytest_exit=1
fi

# Kill the watchdog child so it does not keep sleeping if pytest finished early.
if [ -n "$watchdog_pid" ]; then
    kill "$watchdog_pid" 2>/dev/null || true
fi

exit "$pytest_exit"
