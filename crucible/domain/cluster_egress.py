"""Where a Kubernetes worker's DNS and in-cluster model endpoint live (26, crucible#91).

A CNI that translates a service address to its backend pods before it evaluates policy
(Cilium with kube-proxy replacement) never matches an `ipBlock` on a ClusterIP or a
LoadBalancer address. What it does match is a selector on the backends: their namespace
and their pod labels. This module is that selector pair as a document an administrator
edits, and the one place its shape is checked. It is pure, so the admin service, the
provider and the renderer all check the same rules.

The document:

    {"dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
     "local_endpoint": {"namespace": "", "pod_labels": {}, "port": 0}}

An empty `dns.namespace` means the resolver is allowed by its service address alone.
An empty `local_endpoint.namespace` means the local model endpoint is outside the
cluster and keeps its resolved-address rule. A `port` of 0 means the endpoint URL's own
port, which is right when the Service's port and its pods' port are the same.

The module also holds the one rule for how a running attempt's allowlist addresses
move (hades #205): `AllowedAddresses` and `refresh_addresses`, which decide what a
policy allows after its names are looked up again, with an overlap window before an
address that left the answer is dropped. It is pure so the provider and its tests
share the rule.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SETTING_NAME = "kubernetes.egress"

# How long an address that has left a name's answer stays in a running attempt's
# NetworkPolicy beside the new one (hades #205). Twice the 60 second TTL github.com
# answers with: a client in the Pod that cached the old answer when the policy changed
# has had that cache expire and reconnected before the old address is dropped.
DEFAULT_ADDRESS_OVERLAP_SECONDS = 120.0

DEFAULT_DNS_NAMESPACE = "kube-system"
DEFAULT_DNS_POD_LABELS: tuple[tuple[str, str], ...] = (("k8s-app", "kube-dns"),)

_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_LABEL_NAME = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
_LABEL_PREFIX = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")


def namespace_problem(namespace: str) -> str | None:
    if not _NAMESPACE.match(namespace):
        return f"{namespace!r} is not a namespace name"
    return None


def label_problem(key: str, value: str) -> str | None:
    prefix, _, name = key.rpartition("/")
    if (prefix and not _LABEL_PREFIX.match(prefix)) or not _LABEL_NAME.match(name):
        return f"{key!r} is not a label key"
    if not _LABEL_VALUE.match(value):
        return f"{key}={value!r} is not a label value"
    return None


def parse_labels(text: str) -> dict[str, str]:
    """`k8s-app=kube-dns,app=litellm` into a mapping: how the CLI and the admin UI take
    labels. An empty string is no labels."""
    out: dict[str, str] = {}
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        key, separator, value = item.partition("=")
        if not separator:
            raise ValueError(f"{item!r} is not a key=value label")
        if key.strip() in out:
            raise ValueError(f"the label {key.strip()!r} is given twice")
        out[key.strip()] = value.strip()
    return out


def format_labels(labels: Mapping[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


@dataclass(frozen=True, slots=True)
class ClusterEgress:
    dns_namespace: str = DEFAULT_DNS_NAMESPACE
    dns_pod_labels: tuple[tuple[str, str], ...] = DEFAULT_DNS_POD_LABELS
    endpoint_namespace: str = ""
    endpoint_pod_labels: tuple[tuple[str, str], ...] = ()
    endpoint_port: int = 0

    @property
    def endpoint_in_cluster(self) -> bool:
        return bool(self.endpoint_namespace)

    def as_document(self) -> dict[str, Any]:
        return {
            "dns": {"namespace": self.dns_namespace, "pod_labels": dict(self.dns_pod_labels)},
            "local_endpoint": {
                "namespace": self.endpoint_namespace,
                "pod_labels": dict(self.endpoint_pod_labels),
                "port": self.endpoint_port,
            },
        }


def _labels(value: Any, what: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = parse_labels(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} pod_labels must be an object of label keys to values")
    pairs: list[tuple[str, str]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{what} pod_labels must map strings to strings")
        problem = label_problem(key, item)
        if problem is not None:
            raise ValueError(f"{what} pod label {problem}")
        pairs.append((key, item))
    return tuple(sorted(pairs))


def _section(
    document: Mapping[str, Any], key: str, what: str
) -> tuple[str, tuple[tuple[str, str], ...]]:
    section = document.get(key) or {}
    if not isinstance(section, Mapping):
        raise ValueError(f"{key} must be an object")
    namespace = section.get("namespace")
    if namespace is None:
        namespace = ""
    if not isinstance(namespace, str):
        raise ValueError(f"{what} namespace must be a string")
    namespace = namespace.strip()
    labels = _labels(section.get("pod_labels"), what)
    if namespace:
        problem = namespace_problem(namespace)
        if problem is not None:
            raise ValueError(f"{what} namespace {problem}")
        if not labels:
            # An empty podSelector is every pod in the namespace, which is not a
            # destination anybody chose.
            raise ValueError(f"{what} needs at least one pod label when a namespace is set")
    elif labels:
        raise ValueError(f"{what} pod labels need a namespace")
    return namespace, labels


def parse_cluster_egress(
    document: Mapping[str, Any], *, protected_namespaces: tuple[str, ...] = ()
) -> ClusterEgress:
    """Check a document and return it normalised. Raises ValueError naming the field.

    `protected_namespaces` are the workers namespace and Crucible's own: a selector into
    the first is a worker reaching another attempt, into the second a worker reaching
    Crucible's database. Neither is ever the resolver or a model gateway."""
    dns_namespace, dns_labels = _section(document, "dns", "cluster DNS")
    endpoint_namespace, endpoint_labels = _section(
        document, "local_endpoint", "the in-cluster local endpoint"
    )
    raw_port = (document.get("local_endpoint") or {}).get("port") or 0
    if isinstance(raw_port, bool) or not isinstance(raw_port, int | str):
        raise ValueError("the in-cluster local endpoint port must be a number")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("the in-cluster local endpoint port must be a number") from exc
    if not 0 <= port <= 65535:
        raise ValueError("the in-cluster local endpoint port must be between 0 and 65535")
    if port and not endpoint_namespace:
        raise ValueError("the in-cluster local endpoint port needs a namespace")
    for namespace in (dns_namespace, endpoint_namespace):
        if namespace and namespace in protected_namespaces:
            raise ValueError(
                f"a selector may not name the {namespace!r} namespace, which a worker may "
                "never reach"
            )
    return ClusterEgress(
        dns_namespace=dns_namespace,
        dns_pod_labels=dns_labels,
        endpoint_namespace=endpoint_namespace,
        endpoint_pod_labels=endpoint_labels,
        endpoint_port=port,
    )


@dataclass(frozen=True, slots=True)
class AllowedAddresses:
    """What one running attempt's NetworkPolicy allows for its allowlisted names, and
    why each address is there (hades #205).

    `written` are the addresses the policy was written with at launch. The Pod's
    `hostAliases` pin its names to exactly those (hades #191), and a Pod's hostAliases
    cannot change, so they stay in the policy for as long as the Pod runs: dropping one
    would leave the Pod pinned to an address its policy no longer allows.

    `current` is each name's most recent answer, which is where a client that resolves
    the name itself rather than through the hosts file connects. `retiring` are
    addresses that were current once and have left the answer since, each with the
    monotonic time it left; they stay allowed for the overlap window and are dropped
    after it, unless the name answers them again first.

    `cidrs` is what the policy carries: the three sets in that order, each address once."""

    written: tuple[str, ...] = ()
    current: tuple[tuple[str, tuple[str, ...]], ...] = ()
    retiring: tuple[tuple[str, float], ...] = ()

    @property
    def cidrs(self) -> tuple[str, ...]:
        ordered: list[str] = list(self.written)
        for _host, addresses in self.current:
            ordered.extend(addresses)
        ordered.extend(address for address, _since in self.retiring)
        return tuple(dict.fromkeys(ordered))

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(host for host, _addresses in self.current)


def refresh_addresses(
    allowed: AllowedAddresses,
    answers: Mapping[str, Sequence[str]],
    *,
    now: float,
    overlap_seconds: float,
) -> AllowedAddresses:
    """One refresh of a running attempt's addresses (hades #205).

    `answers` holds the names that were looked up again, each with every address it
    resolved to; a name that is not in it, or that answered nothing, keeps what it had,
    because a resolver that did not answer is not a reason to narrow a running attempt's
    network. An address that left a name's answer, and is not one the policy was
    written with, starts retiring at `now`; one that is retiring and comes back is
    current again; one retiring for `overlap_seconds` or longer is dropped. Called with
    no answers it only drops what has retired, which is how the window stays bounded
    between lookups."""
    current: list[tuple[str, tuple[str, ...]]] = []
    for host, addresses in allowed.current:
        answer = tuple(dict.fromkeys(answers.get(host) or ()))
        current.append((host, answer or addresses))
    before = {address for _host, addresses in allowed.current for address in addresses}
    after = {address for _host, addresses in current for address in addresses}
    pinned = set(allowed.written)
    retiring: list[tuple[str, float]] = [
        (address, since)
        for address, since in allowed.retiring
        if address not in after and address not in pinned and now - since < overlap_seconds
    ]
    already = {address for address, _since in retiring}
    for address in sorted(before - after - pinned):
        if address not in already and overlap_seconds > 0:
            retiring.append((address, now))
    return AllowedAddresses(
        written=allowed.written, current=tuple(current), retiring=tuple(retiring)
    )


__all__ = [
    "DEFAULT_ADDRESS_OVERLAP_SECONDS",
    "DEFAULT_DNS_NAMESPACE",
    "DEFAULT_DNS_POD_LABELS",
    "SETTING_NAME",
    "AllowedAddresses",
    "ClusterEgress",
    "format_labels",
    "label_problem",
    "namespace_problem",
    "parse_cluster_egress",
    "parse_labels",
    "refresh_addresses",
]
