#!/usr/bin/env bash
# Bring deploy/kubernetes up on a disposable kind cluster and run one task through it (C9).
#
# This is the author's half of "done means seen working" for the deployment manifests:
# it applies the same base the lab overlay applies, on a real API server with a real
# kubelet and an enforcing CNI, and then drives one script-harness task end to end
# through the deployed API on the Kubernetes provider.
#
# What it deliberately does not prove (github-ci skill): anything cluster-specific. No
# Longhorn, no NFS ReadWriteMany, no ingress controller, no lab network, no DNS record.
#
# CRUCIBLE_DEPLOY_KIND_FIRST_RUN=1 (`make first-run-kind`) runs the first-run proof
# instead of the task smoke (crucible#119, #120, #121, #123, #79): it also pushes the
# combined worker image images/manifest.env pins, points the GitHub API at the stand-ins
# tools/smoke/first_run_smoke.py deploys, and walks gateway, models, GitHub and Status.
# It then prepares a private repository (crucible#157, ADR 0019) from a git stand-in
# served over HTTPS with a certificate from this run's CA: the script-harness image it
# pushes is the pinned one plus that CA in its trust store, and the credential host is
# the stand-in's, so the preparer verifies it and hands it the token as it would GitHub.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=tools/kind/cluster.sh
. "$root/tools/kind/cluster.sh"

run_id=${CRUCIBLE_KIND_RUN_ID:-$(date +%s)-$$}
cluster="crucible-deploy-${run_id}"
registry_host="crucible-registry-${run_id}"
scratch=$(mktemp -d -t crucible-deploy-kind.XXXXXX)
kubeconfig="$scratch/kubeconfig"
certs="$scratch/certs"
# kustomize refuses an absolute path in resources and refuses to leave its own root,
# so the generated overlay has to sit inside the repository. var/ is gitignored and is
# where the other generated deployment artefacts already go.
overlay="$root/var/deploy-kind/$run_id"
worker_ref=""
push_ref=""
first_run=${CRUCIBLE_DEPLOY_KIND_FIRST_RUN:-0}
combined_push_ref=""
built_image=""
release_image=""
cluster_created=0
registry_started=0

cleanup() {
  status=$?
  cleanup_failed=0
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ "$cluster_created" -eq 1 ]; then
    echo "--- deploy-kind: cluster state on failure ---" >&2
    KUBECONFIG="$kubeconfig" kubectl get pods -A -o wide >&2 2>/dev/null || :
    KUBECONFIG="$kubeconfig" kubectl -n hades describe pods >&2 2>/dev/null || :
    KUBECONFIG="$kubeconfig" kubectl -n hades logs deployment/hades-api --tail=120 >&2 2>/dev/null || :
    KUBECONFIG="$kubeconfig" kubectl -n hades logs deployment/hades-api --previous --tail=60 >&2 2>/dev/null || :
    KUBECONFIG="$kubeconfig" kubectl -n hades logs deployment/hades-supervisor --tail=200 >&2 2>/dev/null || :
    KUBECONFIG="$kubeconfig" kubectl -n hades logs job/hades-migrate --tail=40 >&2 2>/dev/null || :
    KUBECONFIG="$kubeconfig" kubectl -n hades-workers get pods,jobs,pvc >&2 2>/dev/null || :
  fi
  if [ "$cluster_created" -eq 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || :
    if kind get clusters 2>/dev/null | grep -Fxq "$cluster"; then
      echo "deploy-kind cleanup: cluster $cluster remains" >&2
      cleanup_failed=1
    fi
  fi
  if [ "$registry_started" -eq 1 ]; then
    docker rm -f "$registry_host" >/dev/null 2>&1 || :
    if docker inspect "$registry_host" >/dev/null 2>&1; then
      echo "deploy-kind cleanup: registry $registry_host remains" >&2
      cleanup_failed=1
    fi
  fi
  # The shared worker image must be left exactly as the other tiers expect to find it:
  # a leftover repository digest made a later Docker e2e run select the wrong reference
  # once already (C8b defect 4).
  for ref in "$push_ref" "$combined_push_ref" "$built_image"; do
    [ -n "$ref" ] || continue
    docker image rm "$ref" >/dev/null 2>&1 || :
    if docker image inspect "$ref" >/dev/null 2>&1; then
      echo "deploy-kind cleanup: image $ref remains" >&2
      cleanup_failed=1
    fi
  done
  busybox=$(crucible_kind_pull "$CRUCIBLE_BUSYBOX_IMAGE" 2>/dev/null) || busybox=$CRUCIBLE_BUSYBOX_IMAGE
  docker run --rm -v "$scratch:/cleanup" "$busybox" \
    chmod -R a+rwX /cleanup >/dev/null 2>&1 || cleanup_failed=1
  rm -rf "$scratch" "$overlay" || cleanup_failed=1
  rmdir "$root/var/deploy-kind" 2>/dev/null || :
  if [ -e "$overlay" ]; then
    echo "deploy-kind cleanup: generated overlay $overlay remains" >&2
    cleanup_failed=1
  fi
  if [ -e "$scratch" ]; then
    echo "deploy-kind cleanup: scratch path $scratch remains" >&2
    cleanup_failed=1
  fi
  if [ "$cleanup_failed" -ne 0 ] && [ "$status" -eq 0 ]; then
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

for tool in docker kind kubectl openssl curl; do
  command -v "$tool" >/dev/null || { echo "deploy-kind: $tool is not on PATH" >&2; exit 2; }
done

crucible_kind_docker_shim "$scratch"

worker_image=${CRUCIBLE_E2E_IMAGE:-$(
  awk -F= '$1 == "SCRIPT_HARNESS" {print $2}' "$root/images/manifest.env"
)}
if ! docker image inspect "$worker_image" >/dev/null 2>&1; then
  echo "deploy-kind: images/manifest.env pins $worker_image but the host daemon lacks it" >&2
  echo "deploy-kind: run 'make e2e-image' first" >&2
  exit 2
fi

# The only release reference comes from the manifest source. The working-tree image gets
# a disposable host tag, then kind retags it to this exact reference after loading. That
# avoids replacing a release image the operator already has on the host.
release_image=$(awk '
  $2 == "name:" { name = $3 }
  $1 == "newTag:" && name { print name ":" $2; exit }
' "$root/deploy/kubernetes/base/kustomization.yaml")
if [ -z "$release_image" ]; then
  echo "deploy-kind: base kustomization names no release image" >&2
  exit 2
fi
built_image="ghcr.io/sentania-labs/crucible:deploy-kind-${run_id}"
revision=$(git -C "$root" rev-parse HEAD 2>/dev/null || echo unknown)
echo "deploy-kind: building and proving $release_image from the working tree"
echo "deploy-kind: kind loads this local image and never pulls it, so the rendered release"
echo "deploy-kind: reference and the image under test are both $release_image"
docker build -t "$built_image" --build-arg "REVISION=$revision" "$root" >/dev/null

# A registry the deployed provider can reach over HTTPS. The provider's crane speaks
# HTTPS to a registry named by hostname, and that is right: resolving a tag to a digest is
# how the harness version refusal happens before a kubelet pulls (07, 26), so it must not
# be a plaintext hop. crane trusts this run's CA through SSL_CERT_FILE below. The CA is
# generated per run, lives only in this scratch directory, and is trusted by exactly two
# things: containerd on the throwaway node, and the two Crucible containers.
mkdir -p "$certs"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout "$certs/ca.key" -out "$certs/ca.crt" \
  -subj "/CN=crucible-deploy-kind-ca" -addext "basicConstraints=critical,CA:TRUE" 2>/dev/null
openssl req -newkey rsa:2048 -nodes \
  -keyout "$certs/registry.key" -out "$certs/registry.csr" \
  -subj "/CN=${registry_host}" 2>/dev/null
cat > "$certs/registry.ext" <<EOF
subjectAltName=DNS:${registry_host},DNS:localhost,IP:127.0.0.1
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -in "$certs/registry.csr" -CA "$certs/ca.crt" -CAkey "$certs/ca.key" \
  -CAcreateserial -out "$certs/registry.crt" -days 1 -extfile "$certs/registry.ext" 2>/dev/null
# The registry container runs unprivileged and has to read its own key. The mode is
# only reachable through this scratch directory, which mktemp -d made 0700, and the
# key is a one-day certificate for a registry that is deleted with the cluster.
chmod 0644 "$certs/registry.key"

registry_started=1
crucible_kind_start_registry "$registry_host" \
  -v "$certs:/certs:ro" \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/registry.crt \
  -e REGISTRY_HTTP_TLS_KEY=/certs/registry.key
registry_port=$CRUCIBLE_KIND_REGISTRY_PORT
registry_ip=$CRUCIBLE_KIND_REGISTRY_IP
crucible_kind_await_registry "https://127.0.0.1:${registry_port}" "$certs/ca.crt"

# The host pushes over loopback, which the Docker daemon treats as an insecure registry
# and therefore does not verify; the cluster pulls by the registry's own name, which is
# what the certificate is for.
push_ref="127.0.0.1:${registry_port}/crucible-worker:${run_id}"
worker_ref="${registry_host}:5000/crucible-worker:${run_id}"
if [ "$first_run" = 1 ]; then
  # The pinned image with one more trusted CA, this run's, and nothing else changed: the
  # preparer clones the private repository from the git stand-in over HTTPS and must be
  # able to verify it. The tag is this run's and is removed with it.
  mkdir -p "$scratch/trust"
  cp "$certs/ca.crt" "$scratch/trust/ca.crt"
  cat > "$scratch/trust/Dockerfile" <<EOF
FROM $worker_image
USER root
COPY ca.crt /usr/local/share/ca-certificates/crucible-deploy-kind.crt
RUN cat /usr/local/share/ca-certificates/crucible-deploy-kind.crt >> /etc/ssl/certs/ca-certificates.crt
USER 1000:1000
EOF
  docker build -q -t "$push_ref" "$scratch/trust" >/dev/null
else
  docker tag "$worker_image" "$push_ref"
fi
docker push "$push_ref" >/dev/null
echo "deploy-kind: the worker image is $worker_ref"
if [ "$first_run" = 1 ]; then
  # The combined worker image carries Hermes, so promoting it is the first-run step that
  # gives Hermes a promoted image. Only its tag is pushed and removed; the host image
  # the other tiers use stays exactly as it was.
  combined_image=$(awk -F= '$1 == "WORKER" {print $2}' "$root/images/manifest.env")
  if ! docker image inspect "$combined_image" >/dev/null 2>&1; then
    echo "deploy-kind: images/manifest.env pins $combined_image but the host daemon lacks it" >&2
    echo "deploy-kind: run 'make images' first" >&2
    exit 2
  fi
  combined_push_ref="127.0.0.1:${registry_port}/crucible-worker:combined-${run_id}"
  docker tag "$combined_image" "$combined_push_ref"
  docker push "$combined_push_ref" >/dev/null
  echo "deploy-kind: the combined worker image is ${registry_host}:5000/crucible-worker:combined-${run_id}"
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
      - hostPath: $certs/ca.crt
        containerPath: /etc/crucible-kind/ca.crt
        readOnly: true
kubeadmConfigPatches:
  - |
    kind: KubeletConfiguration
    podPidsLimit: 512
containerdConfigPatches:
  # containerd 2.x ignores registry.configs, so the CA goes in a certs.d host file
  # written below. Setting config_path here, before containerd first starts, is what
  # makes that directory be read at all and saves restarting the daemon.
  - |-
    [plugins."io.containerd.grpc.v1.cri".registry]
      config_path = "/etc/containerd/certs.d"
EOF

cluster_created=1
kind create cluster --config "$scratch/kind.yaml" --kubeconfig "$kubeconfig" --wait 0s
export KUBECONFIG="$kubeconfig"

node="${cluster}-control-plane"
docker exec "$node" mkdir -p "/etc/containerd/certs.d/${registry_host}:5000"
docker exec -i "$node" sh -c "cat > '/etc/containerd/certs.d/${registry_host}:5000/hosts.toml'" <<EOF
server = "https://${registry_host}:5000"

[host."https://${registry_host}:5000"]
  ca = "/etc/crucible-kind/ca.crt"
EOF

# docker.io mirror so that cluster-side pulls (busybox, CoreDNS) use the mirror first
# and fall back to Docker Hub itself (22).
docker exec "$node" mkdir -p "/etc/containerd/certs.d/docker.io"
docker exec -i "$node" sh -c "cat > '/etc/containerd/certs.d/docker.io/hosts.toml'" <<EOF
server = "https://registry-1.docker.io"

[host."https://${CRUCIBLE_KIND_DOCKER_HUB_MIRROR}"]
  capabilities = ["pull", "resolve"]

[host."https://registry-1.docker.io"]
  capabilities = ["pull", "resolve"]
EOF

crucible_kind_install_calico "$scratch" "$kubeconfig"

# The locally built image goes in by kind load, never a registry pull. Retag it inside
# the node at the exact reference kustomize rendered, leaving any host release tag alone.
kind load docker-image "$built_image" --name "$cluster"
loaded_image=$(docker exec "$node" ctr -n k8s.io images list -q | \
  awk -v tag="deploy-kind-${run_id}" '$0 ~ (":" tag "$") { print; exit }')
if [ -z "$loaded_image" ]; then
  echo "deploy-kind: kind did not load $built_image" >&2
  exit 2
fi
docker exec "$node" ctr -n k8s.io images tag "$loaded_image" "$release_image"

# The generated overlay: the per-run values, layered on the committed kind overlay. They
# are generated rather than committed because an image under test, a disposable
# registry's name and a container's address on a Docker network are facts about one run.
mkdir -p "$overlay"
# kustomize will not read a generator source from outside its own root.
cp "$certs/ca.crt" "$overlay/ca.crt"
cat > "$overlay/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../../deploy/kubernetes/overlays/kind
generatorOptions:
  disableNameSuffixHash: true
configMapGenerator:
  - name: crucible-registry-ca
    namespace: hades
    files:
      - ca.crt
patches:
  - path: settings.yaml
  - path: api-registry-access.yaml
  - path: supervisor-registry-access.yaml
EOF
cat > "$overlay/settings.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: hades-settings
  namespace: hades
data:
  CRUCIBLE_KUBERNETES__IMAGE_REPOSITORIES: '["${registry_host}:5000/crucible-worker"]'
  CRUCIBLE_KUBERNETES__PROBE_IMAGE: ${worker_ref}
  # kind's containerd gives every container a private cgroup namespace, so the readiness
  # canary cannot see the pod-level cgroup the KubeletConfiguration above sets
  # podPidsLimit: 512 on (95). This is lab-admin's attestation of that same number for
  # this disposable cluster, the same way a real deployment would state it.
  CRUCIBLE_KUBERNETES__POD_PID_LIMIT_OVERRIDE: "512"
  # crane, which the provider runs to resolve a worker image, reads this, as does Python's
  # default HTTPS context; the Kubernetes API client does not: it builds its own context
  # from the ServiceAccount's ca.crt (k8sapi.py). So the only thing this changes is which
  # registry the provider will trust.
  SSL_CERT_FILE: /etc/crucible-registry-ca/ca.crt
EOF
if [ "$first_run" = 1 ]; then
  # The stand-in GitHub API tools/smoke/first_run_smoke.py deploys. The real GitHub API
  # is never called on this cluster. The private repository's git stand-in is the one
  # host the checkout token is answered for, and it is a Pod: its address is inside this
  # cluster's pod range, which 26 denies unless a deployment names it, so the run names
  # kind's pod subnet (the podSubnet above) for the endpoints it resolves.
  cat >> "$overlay/settings.yaml" <<EOF
  CRUCIBLE_GITHUB__API_BASE: http://crucible-stubs.crucible-stubs.svc.cluster.local:8080
  CRUCIBLE_GITHUB__CREDENTIAL_HOST: crucible-git.crucible-stubs.svc.cluster.local:8443
  CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_CIDRS: '["10.244.0.0/16"]'
EOF
fi
for component in api supervisor; do
  cat > "$overlay/${component}-registry-access.yaml" <<EOF
# The disposable registry is a Docker container on the kind network, so the deployed pods
# reach it through an /etc/hosts entry and trust it through this run's own CA. Neither is
# part of the manifests: on a real cluster the registry is a real registry, with a real
# name and a publicly trusted certificate.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hades-${component}
  namespace: hades
spec:
  template:
    spec:
      hostAliases:
        - ip: "$registry_ip"
          hostnames: ["$registry_host"]
      containers:
        - name: ${component}
          volumeMounts:
            - name: registry-ca
              mountPath: /etc/crucible-registry-ca
              readOnly: true
      volumes:
        - name: registry-ca
          configMap:
            name: crucible-registry-ca
EOF
done

echo "deploy-kind: applying deploy/kubernetes/overlays/kind at $release_image"
# A Job's pod template is immutable, so a second apply against a live cluster would be
# refused. Argo re-creates it through the hook annotations the Job carries; a plain
# apply needs this. The cluster is new on the first run, so --ignore-not-found is enough.
kubectl -n hades delete job hades-migrate --ignore-not-found --wait=true
kubectl apply -k "$overlay"

kubectl -n hades wait --for=condition=Complete job/hades-migrate --timeout=300s
kubectl -n hades rollout status statefulset/hades-postgres --timeout=300s
kubectl -n hades rollout status deployment/hades-api --timeout=300s
kubectl -n hades rollout status deployment/hades-supervisor --timeout=300s
# The reference-cache claim is deliberately not waited on: kind's local-path class is
# WaitForFirstConsumer, so it binds when the first Pod mounts it, which is the smoke's
# origin-seed Pod. On the lab's class it binds earlier; neither is a property of the
# manifests.
echo "deploy-kind: the deployment is up"
kubectl -n hades get deploy,sts,svc,ingress,job
kubectl -n hades-workers get sa,role,rolebinding,networkpolicy,resourcequota,pvc

if [ "$first_run" = 1 ]; then
  CRUCIBLE_DEPLOY_KIND_STUB_IMAGE="$release_image" \
    CRUCIBLE_DEPLOY_KIND_GIT_IMAGE="${registry_host}:5000/crucible-worker:combined-${run_id}" \
    CRUCIBLE_DEPLOY_KIND_CA_DIR="$certs" \
    "${UV:-uv}" run --frozen python "$root/tools/smoke/first_run_smoke.py"
else
  CRUCIBLE_DEPLOY_KIND_WORKER_IMAGE="$worker_ref" python3 "$root/tools/smoke/kubernetes_smoke.py"
fi
