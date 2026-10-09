#!/usr/bin/env bash
# crucible#91 on a real Cilium: a disposable kind cluster with kube-proxy replacement,
# an in-cluster stand-in for the model gateway behind a Service, and the worker egress
# policy in two forms.
#
#   main       the policy as it was before #91: kube-dns and the gateway as ipBlock /32s
#              on their service addresses. Cilium translates those to the backend pods
#              before it evaluates policy, so neither rule matches.
#   selectors  the policy of this tree: the same address rules plus the kube-dns pods
#              and the gateway pods by namespace and pod selector.
#
# For each form a probe pod under that policy reports DNS, the gateway by name, by its
# service address and by pod address, and the API server by its service address and by
# the node's address. Then the provider's own readiness canary runs against the cluster
# in both forms and prints what the status page would say.
#
# The cluster, its kubeconfig and the downloaded tools are deleted on exit, success or
# not. Nothing here touches any other cluster or kubeconfig.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=tools/kind/cluster.sh
. "$root/tools/kind/cluster.sh"

CILIUM_VERSION=1.20.2
CILIUM_CHART_SHA256=b2afd87b7f75f875f92a14559f14f59b7babbb479d968e3fd625a20bf30ec20e
HELM_VERSION=v4.3.0
HELM_SHA256=86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb
# curl, getent, nslookup, nc and timeout in one small image; runs as any uid.
PROBE_IMAGE='curlimages/curl@sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6'
GATEWAY_IMAGE='busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0'
# The renderer as it was before #91. The merge base with main is the default; pass a
# ref to compare against another.
BASELINE=${CRUCIBLE_CILIUM_BASELINE:-$(git -C "$root" merge-base HEAD origin/main)}

run_id=${CRUCIBLE_KIND_RUN_ID:-$(date +%s)-$$}
cluster="crucible-91-cilium-${run_id}"
scratch=$(mktemp -d -t crucible-cilium-egress.XXXXXX)
kubeconfig="$scratch/kubeconfig"
cluster_created=0

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ "$cluster_created" -eq 1 ]; then
    echo "--- cilium-egress: cluster state on failure ---" >&2
    KUBECONFIG="$kubeconfig" kubectl get pods -A -o wide >&2 2>/dev/null || :
  fi
  if [ "$cluster_created" -eq 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || :
    if kind get clusters 2>/dev/null | grep -Fxq "$cluster"; then
      echo "cilium-egress cleanup: cluster $cluster remains" >&2
      status=1
    else
      echo "cilium-egress: cluster $cluster deleted"
    fi
  fi
  rm -rf "$scratch"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

crucible_kind_docker_shim "$scratch"
k() { KUBECONFIG="$kubeconfig" kubectl "$@"; }

echo "cilium-egress: fetching helm ${HELM_VERSION} and the cilium ${CILIUM_VERSION} chart"
curl -fsSL --retry 4 "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
  -o "$scratch/helm.tgz"
echo "${HELM_SHA256}  $scratch/helm.tgz" | sha256sum -c - >/dev/null
tar -xzf "$scratch/helm.tgz" -C "$scratch" linux-amd64/helm
helm="$scratch/linux-amd64/helm"
curl -fsSL --retry 4 "https://helm.cilium.io/cilium-${CILIUM_VERSION}.tgz" -o "$scratch/cilium.tgz"
echo "${CILIUM_CHART_SHA256}  $scratch/cilium.tgz" | sha256sum -c - >/dev/null

cat > "$scratch/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: $cluster
networking:
  disableDefaultCNI: true
  kubeProxyMode: none
  podSubnet: 10.244.0.0/16
nodes:
  - role: control-plane
kubeadmConfigPatches:
  - |
    kind: KubeletConfiguration
    podPidsLimit: 512
EOF
cluster_created=1
echo "cilium-egress: creating kind cluster $cluster (no CNI, no kube-proxy)"
kind create cluster --config "$scratch/kind.yaml" --kubeconfig "$kubeconfig" --wait 0s
node="${cluster}-control-plane"

echo "cilium-egress: installing cilium ${CILIUM_VERSION} with kube-proxy replacement"
KUBECONFIG="$kubeconfig" "$helm" install cilium "$scratch/cilium.tgz" --namespace kube-system \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost="$node" \
  --set k8sServicePort=6443 \
  --set ipam.mode=kubernetes \
  --set operator.replicas=1 \
  --set routingMode=tunnel \
  --set tunnelProtocol=vxlan >/dev/null
k -n kube-system rollout status daemonset/cilium --timeout=300s
k wait --for=condition=Ready nodes --all --timeout=180s
k -n kube-system rollout status deployment/coredns --timeout=180s
echo "cilium-egress: cilium status"
k -n kube-system exec daemonset/cilium -c cilium-agent -- cilium-dbg status \
  | grep -E '^(KubeProxyReplacement|Cilium|Kubernetes):' || :
k -n kube-system exec daemonset/cilium -c cilium-agent -- cilium-dbg version | head -1 || :

echo "cilium-egress: the workers namespace, its default deny and the gateway stand-in"
k apply -f "$root/deploy/kubernetes/base/workers/namespace.yaml" >/dev/null
k apply -f "$root/deploy/kubernetes/base/workers/networkpolicy.yaml" >/dev/null
k apply -f "$root/deploy/kubernetes/base/workers/serviceaccounts.yaml" >/dev/null
k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: litellm
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm
  namespace: litellm
spec:
  replicas: 1
  selector:
    matchLabels: {app: litellm}
  template:
    metadata:
      labels: {app: litellm}
    spec:
      containers:
        - name: gateway
          image: $GATEWAY_IMAGE
          command: [sh, -c, "mkdir -p /tmp/www && echo gateway-stand-in > /tmp/www/index.html && exec httpd -f -p 4000 -h /tmp/www"]
          ports: [{containerPort: 4000}]
---
apiVersion: v1
kind: Service
metadata:
  name: litellm
  namespace: litellm
spec:
  selector: {app: litellm}
  ports: [{port: 80, targetPort: 4000}]
EOF
k -n litellm rollout status deployment/litellm --timeout=180s

dns_ip=$(k -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}')
gateway_ip=$(k -n litellm get svc litellm -o jsonpath='{.spec.clusterIP}')
gateway_pod_ip=$(k -n litellm get pods -l app=litellm -o jsonpath='{.items[0].status.podIP}')
dns_pod_ip=$(k -n kube-system get pods -l k8s-app=kube-dns -o jsonpath='{.items[0].status.podIP}')
api_ip=$(k get svc kubernetes -o jsonpath='{.spec.clusterIP}')
node_ip=$(docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$node")
echo "cilium-egress: kube-dns ${dns_ip}, gateway service ${gateway_ip}:80 -> pod ${gateway_pod_ip}:4000, API ${api_ip}:443 and ${node_ip}:6443"

git -C "$root" show "${BASELINE}:crucible/adapters/execution/k8sspec.py" > "$scratch/k8sspec_main.py"
echo "cilium-egress: baseline renderer from ${BASELINE}"

probe() {
  local form=$1
  echo
  echo "=== form: $form ==="
  "${UV:-uv}" run --quiet python "$root/tools/kind/cilium_egress.py" render --form "$form" \
    --dns-ip "$dns_ip" --gateway-ip "$gateway_ip" --main-module "$scratch/k8sspec_main.py" \
    > "$scratch/np-$form.json"
  echo "--- rendered egress rules ($form) ---"
  python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["spec"]["egress"], indent=1))' \
    "$scratch/np-$form.json"
  k apply -f "$scratch/np-$form.json" >/dev/null
  k -n hades-workers delete pod "probe-$form" --ignore-not-found >/dev/null
  k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: probe-$form
  namespace: hades-workers
  labels: {crucible.attempt: PROOF, crucible.role: worker}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: probe
      image: $PROBE_IMAGE
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      env:
        - {name: GATEWAY_IP, value: "$gateway_ip"}
        - {name: GATEWAY_POD_IP, value: "$gateway_pod_ip"}
        - {name: DNS_POD_IP, value: "$dns_pod_ip"}
        - {name: API_IP, value: "$api_ip"}
        - {name: NODE_IP, value: "$node_ip"}
      command:
        - sh
        - -c
        - |
          check() { name=\$1; shift; if "\$@" >/dev/null 2>&1; then echo "\$name: ok"; else echo "\$name: blocked (exit \$?)"; fi; }
          check "dns kubernetes.default.svc          " timeout 20 getent hosts kubernetes.default.svc
          check "gateway by name  litellm.litellm.svc" curl -sS -o /dev/null --max-time 5 http://litellm.litellm.svc.cluster.local/
          check "gateway service  \$GATEWAY_IP:80    " curl -sS -o /dev/null --max-time 5 http://\$GATEWAY_IP:80/
          check "gateway pod      \$GATEWAY_POD_IP:4000" curl -sS -o /dev/null --max-time 5 http://\$GATEWAY_POD_IP:4000/
          check "kube-dns pod, not port 53 :8080     " curl -sS -o /dev/null --max-time 5 http://\$DNS_POD_IP:8080/health
          check "API server service \$API_IP:443     " nc -z -w 5 \$API_IP 443
          check "API server node  \$NODE_IP:6443     " nc -z -w 5 \$NODE_IP 6443
          check "public internet 1.1.1.1:443         " nc -z -w 5 1.1.1.1 443
EOF
  k -n hades-workers wait --for=jsonpath='{.status.phase}'=Succeeded "pod/probe-$form" --timeout=240s >/dev/null
  echo "--- probe pod under the $form policy ---"
  k -n hades-workers logs "probe-$form"
  k -n hades-workers delete pod "probe-$form" --wait=true >/dev/null
  k -n hades-workers delete networkpolicy "np-proof-$form" >/dev/null
}

probe main
probe selectors

canary() {
  local form=$1
  echo
  echo "--- the provider's readiness canary, $form form ---"
  "${UV:-uv}" run --quiet python "$root/tools/kind/cilium_egress.py" canary --form "$form" \
    --dns-ip "$dns_ip" --gateway-ip "$gateway_ip" --kubeconfig "$kubeconfig" \
    --image "$PROBE_IMAGE" --endpoint-url "http://litellm.litellm.svc.cluster.local:80/"
}

canary main
canary selectors
echo
echo "cilium-egress: done"
