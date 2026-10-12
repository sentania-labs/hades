"""The Python half of tools/kind/cilium-egress.sh (crucible#91).

`render` writes one worker NetworkPolicy the way a given renderer produces it: `main`
is the renderer as it was before #91 (loaded from a file the shell script extracts with
`git show`), `selectors` is the renderer of this tree with the kube-dns selector and
an in-cluster gateway selector. `canary` runs the provider's own readiness canary
against the cluster and prints what it found.

Nothing here reads or writes anything outside the disposable cluster the shell script
created: the kubeconfig it is given is that cluster's.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import KubernetesClient, kubeconfig_access
from crucible.adapters.execution.k8sregistry import CraneRegistryClient
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.domain.cluster_egress import ClusterEgress

LABELS = {k8sspec.LABEL_ATTEMPT: "PROOF", k8sspec.LABEL_ROLE: k8sspec.ROLE_WORKER}


def _main_renderer(path: str) -> Any:
    spec = importlib.util.spec_from_file_location("k8sspec_before_91", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def render(args: argparse.Namespace) -> dict[str, Any]:
    common = {
        "name": f"np-proof-{args.form}",
        "namespace": "hades-workers",
        "object_labels": LABELS,
        "attempt_id": "PROOF",
        "role": k8sspec.ROLE_WORKER,
        "dns_server": args.dns_ip,
    }
    if args.form == "main":
        # What the provider on main renders for a local route that resolves to the
        # gateway's service address (declared in local_endpoint_cidrs, as the lab did).
        main = _main_renderer(args.main_module)
        return dict(
            main.egress_policy(
                plan=main.EgressPlan(endpoints=(f"{args.gateway_ip}:{args.gateway_port}",)),
                **common,
            )
        )
    return k8sspec.egress_policy(
        plan=k8sspec.EgressPlan(
            endpoint_selector=k8sspec.PeerSelector.of(args.gateway_namespace, {"app": "litellm"}),
            endpoint_ports=(args.gateway_target_port,),
        ),
        dns_selector=k8sspec.PeerSelector.of("kube-system", {"k8s-app": "kube-dns"}),
        **common,
    )


async def canary(args: argparse.Namespace) -> dict[str, Any]:
    egress = (
        ClusterEgress(
            endpoint_namespace=args.gateway_namespace,
            endpoint_pod_labels=(("app", "litellm"),),
            endpoint_port=args.gateway_target_port,
        )
        if args.form == "selectors"
        # The address-only form: no DNS selector, and the gateway's service address
        # declared as a local endpoint range, which is how main had to be configured.
        else ClusterEgress(dns_namespace="", dns_pod_labels=())
    )
    config = KubernetesConfig(
        namespace="hades-workers",
        cluster_dns_ip=args.dns_ip,
        egress=egress,
        local_endpoint_url=args.endpoint_url,
        local_endpoint_cidrs=(f"{args.gateway_ip}/32",) if args.form == "main" else (),
        probe_image=args.image,
        # kind's containerd hides the pod-level cgroup from the canary, so the podPidsLimit
        # the cluster config sets is attested here, as deploy-kind.sh does (95).
        pod_pid_limit_override=512,
        launch_timeout_seconds=240,
        poll_interval_seconds=1,
    )
    client = KubernetesClient(kubeconfig_access(args.kubeconfig), "hades-workers")
    # Crucible runs inside the cluster and resolves the gateway's name to its service
    # address; this process runs on the host, so that one answer is given to it.
    gateway_host = urlsplit(args.endpoint_url).hostname

    def resolver(host: str) -> list[str]:
        return [f"{args.gateway_ip}/32"] if host == gateway_host else []

    provider = KubernetesProvider(config, client, CraneRegistryClient(), resolver=resolver)
    probe = await provider.ensure_ready()
    return {"form": args.form, "passed": probe.passed, "checked": probe.checked, **probe.as_dict()}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "canary"):
        p = sub.add_parser(name)
        p.add_argument("--form", choices=("main", "selectors"), required=True)
        p.add_argument("--dns-ip", required=True)
        p.add_argument("--gateway-ip", required=True)
        p.add_argument("--gateway-port", type=int, default=80)
        p.add_argument("--gateway-namespace", default="litellm")
        p.add_argument("--gateway-target-port", type=int, default=4000)
        p.add_argument("--main-module", default="")
        p.add_argument("--kubeconfig", default="")
        p.add_argument("--image", default="")
        p.add_argument("--endpoint-url", default="")
    args = parser.parse_args(argv)
    if args.command == "render":
        json.dump(render(args), sys.stdout, indent=2)
    else:
        json.dump(asyncio.run(canary(args)), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main(sys.argv[1:]))
