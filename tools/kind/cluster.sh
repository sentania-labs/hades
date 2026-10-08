# Shared kind mechanics, sourced by tools/kind/e2e-kind.sh (C8b) and
# tools/kind/deploy-kind.sh (C9). One definition, two callers (sdlc skill).
#
# Nothing here creates a cluster or deletes one: the caller owns its own cleanup trap,
# because what has to be torn down differs between the two tiers.

# Calico, because kind's default CNI does not enforce egress NetworkPolicy and 26's
# readiness probe refuses to launch anything on a cluster that does not (C8b). The
# manifest is pinned by version and by the SHA-256 of the file itself, but the images
# it names were tag-only (67): a retag at quay.io would still pass that check and pull
# something the manifest was never proved against. Pin each by the digest quay.io
# reported for v3.32.2 on 2026-09-25 (docker-content-digest of the manifest list).
CRUCIBLE_CALICO_VERSION=v3.32.2
CRUCIBLE_CALICO_SHA256=a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02
CRUCIBLE_CALICO_CNI_DIGEST=sha256:0ef740bc587f25565905adf1d1f61a7faff0d571c449c6bdd789feed743d3ef7
CRUCIBLE_CALICO_NODE_DIGEST=sha256:99b03fe91e8bfbcb153ae65ef4b701b24ce541ffdd74ff314eb041096008f7fd
CRUCIBLE_CALICO_KUBE_CONTROLLERS_DIGEST=sha256:7870b67ebb13fabc3005252b44fe6e78b21635649bd3072b80afa1684b6565d0
CRUCIBLE_REGISTRY_IMAGE='registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373'

# Docker Hub serves the registry and busybox images this tier pulls, anonymously and
# rate limited on shared CI runners (68). mirror.gcr.io is Google's public pull-through
# cache of Docker Hub and needs no credential. Every Docker Hub image here is pinned by
# digest, so whichever of the two serves it, the bytes are the same.
CRUCIBLE_KIND_DOCKER_HUB_MIRROR=mirror.gcr.io
# The node image kind v0.33.0 (the CI pin) uses by default, named here so it can be
# pulled through the mirror like the rest and handed to `kind create cluster --image`.
# shellcheck disable=SC2034 # used by the scripts that source this file
# Kubernetes 1.29 or later: the declared test services (hades #558, #85) are native
# sidecars (an init container with restartPolicy Always), which older kubelets refuse.
CRUCIBLE_KIND_NODE_IMAGE='kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5'
# shellcheck disable=SC2034 # used by the scripts that source this file
CRUCIBLE_BUSYBOX_IMAGE='busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0'

# The containerd patch that points the kind node's own Docker Hub pulls (busybox in
# deploy/kind/workers.yaml and the CoreDNS side container) at the mirror first, with
# Docker Hub itself as the fallback. Only for a cluster whose containerd does not set
# config_path, which cannot be combined with registry.mirrors.
# shellcheck disable=SC2034 # used by the scripts that source this file
CRUCIBLE_KIND_DOCKER_HUB_MIRROR_PATCH="[plugins.\"io.containerd.grpc.v1.cri\".registry.mirrors.\"docker.io\"]
      endpoint = [\"https://${CRUCIBLE_KIND_DOCKER_HUB_MIRROR}\", \"https://registry-1.docker.io\"]"

# Pull a digest-pinned image onto the host daemon and print the reference that now
# resolves locally (68). A Docker Hub image is tried from the mirror first and from
# Docker Hub second on each attempt; four attempts, doubling from two seconds, ride out
# a short rate limit or a blip without masking a real outage.
crucible_kind_pull() {
  local image=$1 attempt delay=2 first candidate error
  local -a candidates=("$image")
  first=${image%%/*}
  if [ "$first" = "$image" ]; then
    candidates=("${CRUCIBLE_KIND_DOCKER_HUB_MIRROR}/library/${image}" "$image")
  elif [[ "$first" != *.* && "$first" != *:* && "$first" != localhost ]]; then
    candidates=("${CRUCIBLE_KIND_DOCKER_HUB_MIRROR}/${image}" "$image")
  fi
  for attempt in 1 2 3 4; do
    for candidate in "${candidates[@]}"; do
      if error=$(docker pull -q "$candidate" 2>&1 >/dev/null); then
        printf '%s\n' "$candidate"
        return 0
      fi
      echo "kind: pull of $candidate failed (attempt $attempt/4): ${error##*$'\n'}" >&2
    done
    [ "$attempt" -eq 4 ] && break
    sleep "$delay"
    delay=$((delay * 2))
  done
  echo "kind: could not pull $image from any source" >&2
  return 1
}

# Put a shim ahead of PATH so both the caller and kind use the daemon selected by the
# same possibly wrapped Docker command as the other end-to-end tiers.
crucible_kind_docker_shim() {
  local scratch=$1
  local -a docker_command
  read -r -a docker_command <<< "${CRUCIBLE_E2E_DOCKER:-docker}"
  if [ "${#docker_command[@]}" -eq 0 ]; then
    echo "kind: CRUCIBLE_E2E_DOCKER must name a Docker command" >&2
    return 2
  fi
  local host_docker index
  host_docker=$(command -v docker)
  for index in "${!docker_command[@]}"; do
    if [ "${docker_command[$index]}" = docker ]; then
      docker_command[index]=$host_docker
    fi
  done
  mkdir -p "$scratch/bin"
  {
    printf '#!/usr/bin/env bash\nset -euo pipefail\nexec'
    printf ' %q' "${docker_command[@]}"
    printf ' "$@"\n'
  } > "$scratch/bin/docker"
  chmod 0755 "$scratch/bin/docker"
  export PATH="$scratch/bin:$PATH"
}

# Start the disposable OCI registry on the `kind` Docker network and wait for it to
# answer. Sets CRUCIBLE_KIND_REGISTRY_PORT (the host-loopback port) and
# CRUCIBLE_KIND_REGISTRY_IP (its address on the `kind` network, which is how a Pod
# reaches it). Extra arguments are passed to `docker run`. Also sets
# CRUCIBLE_KIND_NETWORK_CREATED=1 when the `kind` network did not already exist, so a
# caller that made it is the one that can try to clean it up (77): the name is shared
# by every kind cluster on the host, so a caller that finds it already there never owns
# its removal.
crucible_kind_start_registry() {
  local name=$1
  shift
  CRUCIBLE_KIND_NETWORK_CREATED=0
  if ! docker network inspect kind >/dev/null 2>&1; then
    docker network create kind >/dev/null
    CRUCIBLE_KIND_NETWORK_CREATED=1
  fi
  local image
  image=$(crucible_kind_pull "$CRUCIBLE_REGISTRY_IMAGE")
  docker run -d --restart=no --network kind --name "$name" \
    -p 127.0.0.1::5000 "$@" "$image" >/dev/null
  CRUCIBLE_KIND_REGISTRY_PORT=$(docker port "$name" 5000/tcp | awk -F: 'NR == 1 {print $NF}')
  CRUCIBLE_KIND_REGISTRY_IP=$(docker inspect -f \
    '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$name")
  export CRUCIBLE_KIND_REGISTRY_PORT CRUCIBLE_KIND_REGISTRY_IP CRUCIBLE_KIND_NETWORK_CREATED
}

# Wait for a registry to answer /v2/ on the given base URL.
crucible_kind_await_registry() {
  local base=$1 curl_ca=${2:-}
  local -a curl_args=(-fsS)
  [ -n "$curl_ca" ] && curl_args+=(--cacert "$curl_ca")
  local _
  for _ in $(seq 1 40); do
    if curl "${curl_args[@]}" "${base}/v2/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  curl "${curl_args[@]}" "${base}/v2/" >/dev/null
}

# Install the pinned Calico and wait for the data plane and the node.
crucible_kind_install_calico() {
  local scratch=$1 kubeconfig=$2
  local calico="$scratch/calico.yaml"
  curl -fsSL --retry 4 \
    "https://raw.githubusercontent.com/projectcalico/calico/${CRUCIBLE_CALICO_VERSION}/manifests/calico.yaml" \
    -o "$calico"
  echo "${CRUCIBLE_CALICO_SHA256}  ${calico}" | sha256sum -c -
  sed -i \
    -e 's#192\.168\.0\.0/16#10.244.0.0/16#g' \
    -e "s#quay.io/calico/cni:${CRUCIBLE_CALICO_VERSION}#quay.io/calico/cni:${CRUCIBLE_CALICO_VERSION}@${CRUCIBLE_CALICO_CNI_DIGEST}#g" \
    -e "s#quay.io/calico/node:${CRUCIBLE_CALICO_VERSION}#quay.io/calico/node:${CRUCIBLE_CALICO_VERSION}@${CRUCIBLE_CALICO_NODE_DIGEST}#g" \
    -e "s#quay.io/calico/kube-controllers:${CRUCIBLE_CALICO_VERSION}#quay.io/calico/kube-controllers:${CRUCIBLE_CALICO_VERSION}@${CRUCIBLE_CALICO_KUBE_CONTROLLERS_DIGEST}#g" \
    "$calico"
  # A new manifest that spells an image differently would slip past the rewrite above.
  local images
  images=$(grep -E 'image:' "$calico" || :)
  if [ -z "$images" ] || grep -v '@sha256:' <<< "$images" >&2; then
    echo "kind: the Calico manifest names no image, or one not pinned by digest (67)" >&2
    return 1
  fi
  KUBECONFIG="$kubeconfig" kubectl apply -f "$calico" >/dev/null
  KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status daemonset/calico-node --timeout=180s
  KUBECONFIG="$kubeconfig" kubectl wait --for=condition=Ready nodes --all --timeout=180s
}

# A readiness gate that confirms the cluster is truly operational after Calico
# is applied. Checks four things, each with a 3-minute deadline: every node
# Ready, the Calico DaemonSet rolled out, CoreDNS Ready, and a throwaway pod
# that resolves kubernetes.default and connects to the API service IP.
crucible_kind_wait_ready() {
  local kubeconfig=$1
  local deadline=180
  local failures=()

  # Check 1: every node Ready (3-minute deadline)
  if ! KUBECONFIG="$kubeconfig" kubectl wait --for=condition=Ready nodes --all --timeout="${deadline}s" 2>/dev/null; then
    failures+=("nodes not Ready")
  fi

  # Check 2: Calico DaemonSet rolled out (3-minute deadline)
  if ! KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status daemonset/calico-node --timeout="${deadline}s" 2>/dev/null; then
    failures+=("calico DaemonSet not rolled out")
  fi

  # Check 3: CoreDNS Ready (3-minute deadline)
  if ! KUBECONFIG="$kubeconfig" kubectl -n kube-system rollout status deployment/coredns --timeout="${deadline}s" 2>/dev/null; then
    failures+=("CoreDNS not Ready")
  fi

  # Check 4: a throwaway pod resolves kubernetes.default.svc.cluster.local (pod DNS
  # works, not only CoreDNS's own pods; the full name so busybox's resolver needs no
  # search path) and then opens a TCP connection to the API service IP on
  # 443. The API speaks only HTTPS, so a plain http fetch can never pass; a TCP connect
  # is the reachability proof (PR 326's own kind run and Codex review). busybox nc has
  # no -z: connecting with stdin at EOF opens the socket and exits 0 once connected.
  # The image is pulled if the node lacks it: with Never the pod could not start on a
  # fresh cluster and the gate blamed the API server (PR 326's third kind run).
  local api_ip
  api_ip=$(KUBECONFIG="$kubeconfig" kubectl -n default get service kubernetes -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
  if [ -n "$api_ip" ]; then
    local check_pod="crucible-readiness-$$"
    KUBECONFIG="$kubeconfig" kubectl run "$check_pod" \
      --image="$CRUCIBLE_BUSYBOX_IMAGE" \
      --image-pull-policy=IfNotPresent \
      --restart=Never \
      -- /bin/sh -c "nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1 || exit 2; nc -w 5 ${api_ip} 443 </dev/null >/dev/null 2>&1 || exit 1" >/dev/null 2>&1
    # Wait for the pod to finish rather than attaching: an attach to a container that
    # has already exited reports a failure of its own. On any failure, say what the
    # pod saw before it is deleted, so the job log explains the gate.
    local rc=1
    if KUBECONFIG="$kubeconfig" kubectl wait --for=jsonpath='{.status.phase}'=Succeeded \
        "pod/$check_pod" --timeout="${deadline}s" >/dev/null 2>&1; then
      rc=0
    else
      rc=$(KUBECONFIG="$kubeconfig" kubectl get "pod/$check_pod" \
        -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null)
      rc=${rc:-1}
      echo "kind: readiness pod $check_pod did not succeed (exit ${rc}):" >&2
      KUBECONFIG="$kubeconfig" kubectl get "pod/$check_pod" -o wide 2>&1 | sed 's/^/kind:   /' >&2
      KUBECONFIG="$kubeconfig" kubectl logs "pod/$check_pod" 2>&1 | tail -5 | sed 's/^/kind:   /' >&2
      KUBECONFIG="$kubeconfig" kubectl describe "pod/$check_pod" 2>&1 | grep -A8 '^Events' | sed 's/^/kind:   /' >&2
    fi
    KUBECONFIG="$kubeconfig" kubectl delete pod "$check_pod" --ignore-not-found >/dev/null 2>&1 || :
    if [ "$rc" = 2 ]; then
      failures+=("readiness pod cannot resolve kubernetes.default")
    elif [ "$rc" != 0 ]; then
      failures+=("readiness pod cannot reach API server")
    fi
  else
    failures+=("could not find API server IP")
  fi

  if [ "${#failures[@]}" -gt 0 ]; then
    local i
    for i in "${!failures[@]}"; do
      echo "kind: readiness check ${failures[$i]} failed" >&2
    done
    return 1
  fi

  return 0
}
