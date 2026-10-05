"""The security-relevant fields of the rendered deployment manifests (C9, 26).

These are the assertions a manifest edit has to get past. They are about the properties
26 and 13 name, not about formatting: Pod Security admission at `restricted` on the
workers namespace, a default deny that is really there, no cluster-scoped permission, no
Docker socket, no hostPath, no `latest` tag on anything but the example Crucible image
itself, and the supervisor Role's verbs exactly as 26 records them.

The objects are the *rendered* ones, because that is what a cluster sees: kustomize's
`images` transformer is what puts the version on an image, and an overlay patch is what
puts a storage class on a claim.

`tools/manifests/validate.sh` renders them once and points `CRUCIBLE_RENDERED_MANIFESTS`
here. Run on their own, these tests render with `kubectl kustomize` themselves, and skip
when kubectl is absent so `make test` works on a machine that has no Kubernetes tooling.
`CRUCIBLE_MANIFESTS_REQUIRED=1`, which the CI job and `make manifests` both set, turns
that skip into a failure so CI can never lose the assertions quietly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from crucible.adapters.first_run import SECRET_NAME
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.settings import CredentialSettings, HarnessSettings, Settings

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "kubernetes"
TARGETS = ("base", "overlays/lab", "overlays/kind", "argocd")

# The base is an example and tracks latest; only the deployer's own lab overlay pin is
# exact, and it is a placeholder here, never a real tag or digest (24, docs/deployment.md).
BASE_IMAGE = "ghcr.io/sentania-labs/crucible:latest"
LAB_IMAGE = "ghcr.io/sentania-labs/crucible:REPLACE_ME_CRUCIBLE_TAG@REPLACE_ME_CRUCIBLE_DIGEST"

# 26, as the implemented provider needs it. Each entry is (apiGroup, resource) -> verbs.
SUPERVISOR_ROLE_RULES = {
    ("", "configmaps"): {"create", "get", "list", "watch", "delete"},
    ("", "pods"): {"create", "get", "list", "watch", "delete"},
    ("", "pods/exec"): {"create", "get"},
    ("", "pods/log"): {"get"},
    ("", "persistentvolumeclaims"): {"create", "get", "list", "watch", "patch", "delete"},
    ("", "resourcequotas"): {"get", "list"},
    ("", "events"): {"get", "list"},
    ("", "secrets"): {"create", "get", "list", "watch", "patch", "delete"},
    ("batch", "jobs"): {"create", "get", "list", "watch", "delete"},
    ("networking.k8s.io", "networkpolicies"): {"create", "get", "list", "watch", "delete"},
}

PSA_LABELS = {
    "pod-security.kubernetes.io/enforce": "restricted",
    "pod-security.kubernetes.io/audit": "restricted",
    "pod-security.kubernetes.io/warn": "restricted",
}


def _render(target: str) -> list[dict[str, Any]]:
    prepared = os.environ.get("CRUCIBLE_RENDERED_MANIFESTS")
    if prepared:
        path = Path(prepared) / f"{target.replace('/', '-')}.yaml"
        raw = path.read_text(encoding="utf-8")
    else:
        if shutil.which("kubectl") is None:
            message = "kubectl is needed to render the deployment manifests"
            if os.environ.get("CRUCIBLE_MANIFESTS_REQUIRED"):
                raise AssertionError(message)
            pytest.skip(message)
        raw = subprocess.run(
            ["kubectl", "kustomize", str(DEPLOY / target)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    return [doc for doc in yaml.safe_load_all(raw) if doc]


@pytest.fixture(scope="module")
def rendered() -> dict[str, list[dict[str, Any]]]:
    return {target: _render(target) for target in TARGETS}


def _of_kind(objects: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [o for o in objects if o.get("kind") == kind]


def _named(objects: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for obj in _of_kind(objects, kind):
        if obj["metadata"]["name"] == name:
            return obj
    raise AssertionError(f"no {kind}/{name} in the rendered manifests")


def _pod_specs(objects: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for obj in objects:
        kind = obj.get("kind")
        where = f"{kind}/{obj.get('metadata', {}).get('name')}"
        if kind in {"Deployment", "StatefulSet", "Job"}:
            out.append((where, obj["spec"]["template"]["spec"]))
        elif kind == "Pod":
            out.append((where, obj["spec"]))
    return out


@pytest.mark.parametrize("target", TARGETS)
def test_every_object_is_namespaced_to_crucible_or_is_a_namespace(
    rendered: dict[str, list[dict[str, Any]]], target: str
) -> None:
    """Nothing lands outside the two namespaces C9 owns.

    The Argo Application is the exception and lives in `argocd`, because an Application
    object belongs to the GitOps controller's namespace by definition.
    """
    allowed = {"crucible", "crucible-workers"}
    for obj in rendered[target]:
        kind = obj["kind"]
        if kind in {"Namespace", "Application"}:
            continue
        namespace = obj["metadata"].get("namespace")
        assert namespace in allowed, f"{kind}/{obj['metadata']['name']} is in {namespace!r}"


def test_workers_namespace_enforces_restricted_pod_security(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """26: admission enforces the pod shape independently of Crucible's own code."""
    for target in ("base", "overlays/lab", "overlays/kind"):
        namespace = _named(rendered[target], "Namespace", "crucible-workers")
        labels = namespace["metadata"]["labels"]
        for label, value in PSA_LABELS.items():
            assert labels.get(label) == value, f"{target}: {label} is {labels.get(label)!r}"


def test_workers_namespace_has_the_default_deny(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """26: the readiness probe refuses every launch without this, so losing it is not a
    silent weakening. It is still asserted here, because the probe is a runtime answer
    and this is the one that fails before an apply."""
    for target in ("base", "overlays/lab", "overlays/kind"):
        policies = [
            p
            for p in _of_kind(rendered[target], "NetworkPolicy")
            if p["metadata"]["namespace"] == "crucible-workers"
        ]
        assert len(policies) == 1, f"{target}: {len(policies)} NetworkPolicy objects"
        spec = policies[0]["spec"]
        assert spec["podSelector"] == {}
        assert set(spec["policyTypes"]) == {"Ingress", "Egress"}
        assert spec.get("ingress") == []
        assert spec.get("egress") == []


def test_no_cluster_scoped_permission_anywhere(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    for target in TARGETS:
        for obj in rendered[target]:
            assert obj["kind"] not in {"ClusterRole", "ClusterRoleBinding"}, (
                f"{target}: {obj['kind']}/{obj['metadata']['name']}"
            )


def test_supervisor_role_verbs_are_exactly_what_26_records(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    role = _named(rendered["base"], "Role", "crucible-supervisor")
    assert role["metadata"]["namespace"] == "crucible-workers"
    seen: dict[tuple[str, str], set[str]] = {}
    for rule in role["rules"]:
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                seen.setdefault((group, resource), set()).update(rule["verbs"])
    assert seen == SUPERVISOR_ROLE_RULES


def test_each_rolebinding_names_only_the_control_plane_account(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """A second subject, or a Group subject, is how a Role would reach something other
    than Crucible. The supervisor's Role in `crucible-workers` (26) and the GitHub App
    Secret's Role in `crucible` (ADR 0017) bind the control-plane account; the first-run
    Secret's two (ADR 0016) are checked on their own below."""
    subjects = [{"kind": "ServiceAccount", "name": "crucible-supervisor", "namespace": "crucible"}]
    for target in ("base", "overlays/lab", "overlays/kind"):
        bindings = {
            (b["metadata"]["namespace"], b["metadata"]["name"]): b
            for b in _of_kind(rendered[target], "RoleBinding")
        }
        assert set(bindings) == {
            ("crucible-workers", "crucible-supervisor"),
            ("crucible", "crucible-github-app"),
            *(("crucible", name) for name in FIRST_RUN_GRANTS),
        }, target
        for (namespace, name), binding in bindings.items():
            if namespace == "crucible" and name in FIRST_RUN_GRANTS:
                continue  # the migrate account's and the api's first-run grants, below
            assert binding["roleRef"] == {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": name,
            }
            assert binding["subjects"] == subjects, target


def test_the_control_plane_reaches_only_the_github_app_secret_in_its_own_namespace(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """ADR 0017: `get` and `patch` name the one Secret; `create` cannot be narrowed by
    name, and nothing lists, watches, replaces or deletes a Secret in `crucible`."""
    role = _named(rendered["base"], "Role", "crucible-github-app")
    assert role["metadata"]["namespace"] == "crucible"
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["crucible-github-app"],
            "verbs": ["get", "patch"],
        },
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
    ]
    roles = {
        (r["metadata"]["namespace"], r["metadata"]["name"])
        for r in _of_kind(rendered["base"], "Role")
    }
    assert roles == {
        ("crucible-workers", "crucible-supervisor"),
        ("crucible", "crucible-github-app"),
        *(("crucible", name) for name in FIRST_RUN_GRANTS),
    }


def test_gitops_delivers_no_github_app_secret(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """ADR 0017: the service writes the App credential, so a copy GitOps also applied
    would be a second writer. No rendered target carries one, and the mount that reads it
    stays optional because a fresh deployment has none until the operator connects."""
    for target in TARGETS:
        for obj in rendered[target]:
            if obj["kind"] in {"Secret", "SealedSecret", "ExternalSecret"}:
                assert obj["metadata"]["name"] != "crucible-github-app", target
    for name in ("crucible-api", "crucible-supervisor"):
        deployment = _named(rendered["base"], "Deployment", name)
        volumes = deployment["spec"]["template"]["spec"]["volumes"]
        github = next(v for v in volumes if v["name"] == "github")
        assert github["secret"]["optional"] is True


# ADR 0016: in the service namespace, the migrate account may create the first-run
# Secret (Kubernetes cannot narrow `create` by name) and patch that one, and the api's
# account may delete that one. Nothing in Crucible may read it.
FIRST_RUN_GRANTS = {
    "crucible-first-run-admin-writer": (
        "crucible-migrate",
        [
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "resourceNames": [SECRET_NAME],
                "verbs": ["patch"],
            },
        ],
    ),
    "crucible-first-run-admin-remover": (
        "crucible-supervisor",
        [
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "resourceNames": [SECRET_NAME],
                "verbs": ["delete"],
            }
        ],
    ),
}


def test_the_first_run_secret_grants_are_exactly_adr_0016s(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    for target in ("base", "overlays/lab", "overlays/kind"):
        roles = {
            r["metadata"]["name"]: r
            for r in _of_kind(rendered[target], "Role")
            if r["metadata"]["namespace"] == "crucible"
            and r["metadata"]["name"] != "crucible-github-app"
        }
        bindings = {
            b["metadata"]["name"]: b
            for b in _of_kind(rendered[target], "RoleBinding")
            if b["metadata"]["namespace"] == "crucible"
            and b["metadata"]["name"] != "crucible-github-app"
        }
        assert set(roles) == set(FIRST_RUN_GRANTS) == set(bindings), target
        for name, (account, rules) in FIRST_RUN_GRANTS.items():
            assert roles[name]["rules"] == rules, f"{target}: {name}"
            assert bindings[name]["roleRef"] == {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": name,
            }
            assert bindings[name]["subjects"] == [
                {"kind": "ServiceAccount", "name": account, "namespace": "crucible"}
            ], f"{target}: {name}"
        job = _named(rendered[target], "Job", "crucible-migrate")
        assert job["spec"]["template"]["spec"]["serviceAccountName"] == "crucible-migrate"


def test_the_kind_tier_applies_the_deployed_rbac_files() -> None:
    """The e2e support manifest carries no RBAC copy that can drift from deployment."""
    support = [
        doc for doc in yaml.safe_load_all((ROOT / "deploy/kind/workers.yaml").read_text()) if doc
    ]
    assert {doc["kind"] for doc in support}.isdisjoint({"Role", "RoleBinding"})
    tier = (ROOT / "tools/kind/e2e-kind.sh").read_text(encoding="utf-8")
    for path in ("role.yaml", "rolebinding.yaml"):
        assert f"deploy/kubernetes/base/workers/{path}" in tier


def test_the_worker_account_is_bound_to_nothing(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """26: `crucible-worker` is a no-permission account, and a token it never mounts."""
    account = _named(rendered["base"], "ServiceAccount", "crucible-worker")
    assert account["metadata"]["namespace"] == "crucible-workers"
    assert account["automountServiceAccountToken"] is False
    for binding in _of_kind(rendered["base"], "RoleBinding"):
        names = {s.get("name") for s in binding["subjects"]}
        assert "crucible-worker" not in names


def test_no_docker_socket_and_no_host_path_anywhere(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """13: not carried to Kubernetes. A hostPath is the other way a Pod reaches the node,
    and a PersistentVolume with one would reach it just as well."""
    for target in TARGETS:
        for obj in rendered[target]:
            assert obj["kind"] != "PersistentVolume", f"{target}: a PersistentVolume"
        for where, spec in _pod_specs(rendered[target]):
            for volume in spec.get("volumes") or []:
                assert "hostPath" not in volume, f"{target}: {where} mounts a hostPath"
            for container in _containers(spec):
                for mount in container.get("volumeMounts") or []:
                    assert "docker.sock" not in mount["mountPath"], f"{target}: {where}"
                for env in container.get("env") or []:
                    value = str(env.get("value", ""))
                    assert "docker.sock" not in value, f"{target}: {where} {env['name']}"


def _containers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return list(spec.get("initContainers") or []) + list(spec.get("containers") or [])


def test_no_pod_is_privileged_or_shares_a_host_namespace(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    for target in TARGETS:
        for where, spec in _pod_specs(rendered[target]):
            for field in ("hostNetwork", "hostPID", "hostIPC"):
                assert spec.get(field) in (None, False), f"{target}: {where} sets {field}"
            assert spec.get("securityContext", {}).get("runAsNonRoot") is True, where
            for container in _containers(spec):
                security = container.get("securityContext") or {}
                assert security.get("privileged") in (None, False), where
                assert security.get("allowPrivilegeEscalation") is False, where
                assert security.get("readOnlyRootFilesystem") is True, where
                assert security.get("capabilities", {}).get("drop") == ["ALL"], where
                assert not security.get("capabilities", {}).get("add"), where


def test_api_and_supervisor_pods_set_fsgroup_change_policy_on_root_mismatch(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """94: api and supervisor pods set fsGroupChangePolicy: OnRootMismatch."""
    for target in ("base", "overlays/lab", "overlays/kind"):
        for name in ("crucible-api", "crucible-supervisor"):
            deployment = _named(rendered[target], "Deployment", name)
            spec = deployment["spec"]["template"]["spec"]
            security = spec.get("securityContext") or {}
            assert security.get("fsGroup") == 1000, f"{target}: {name} fsGroup"
            assert security.get("fsGroupChangePolicy") == "OnRootMismatch", (
                f"{target}: {name} fsGroupChangePolicy"
            )


def _pinned(image: str) -> bool:
    """A reference carrying an explicit tag or digest.

    The tag separator is a colon in the last path segment. A registry host may carry a
    port, so a bare `host:5000/repo` has a colon and no tag at all, which is exactly the
    shape this has to refuse.
    """
    if "@sha256:" in image:
        return True
    last = image.rsplit("/", 1)[-1]
    return ":" in last


def test_every_image_but_the_example_crucible_one_is_pinned_and_none_is_latest(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """A deployment pins an exact tag; `latest` is not one (sdlc skill, 24). The one
    exception is `ghcr.io/sentania-labs/crucible` in this repository's own base and kind
    overlay: those are examples that track `latest` on purpose, and a real deployment's
    lab overlay is what carries the deployer's exact pin (test below)."""
    for target in TARGETS:
        for where, spec in _pod_specs(rendered[target]):
            for container in _containers(spec):
                image = container["image"]
                is_example_crucible = target in ("base", "overlays/kind") and (
                    image.rsplit(":", 1)[0] == "ghcr.io/sentania-labs/crucible"
                )
                if is_example_crucible:
                    continue
                assert not image.endswith(":latest"), f"{target}: {where} runs {image}"
                assert _pinned(image), f"{target}: {where} runs {image} untagged"


def test_no_setting_names_a_latest_image(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """26's readiness canary runs an image named in configuration, not in a pod spec, so
    the container walk above cannot see it. A `:latest` there is the same defect and the
    whole reason `kubernetes.probe_image` exists."""
    for target in ("base", "overlays/lab", "overlays/kind"):
        for config in _of_kind(rendered[target], "ConfigMap"):
            for key, value in (config.get("data") or {}).items():
                assert not str(value).rstrip().endswith(":latest"), f"{target}: {key}"


def test_the_crucible_image_tag_is_set_in_exactly_one_place() -> None:
    """The Deployments and the Job name the repository with no tag, so the kustomize
    `images` entry is the only line a version (or, in the base, `latest`) can be
    changed in."""
    sources = [p for p in (DEPLOY / "base").rglob("*.yaml") if p.name != "kustomization.yaml"]
    for path in sources:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("image:") and "sentania-labs/crucible" in stripped:
                assert stripped == "image: ghcr.io/sentania-labs/crucible", (
                    f"{path.name} carries a tag: {stripped}"
                )
    kustomization = yaml.safe_load((DEPLOY / "base" / "kustomization.yaml").read_text())
    assert kustomization["images"] == [
        {"name": "ghcr.io/sentania-labs/crucible", "newTag": "latest"}
    ]


def test_the_lab_overlay_carries_the_deployers_pin_as_a_placeholder() -> None:
    """26, 24: the base is an example; the lab overlay is where a real deployment's exact
    tag and digest go, and here they are placeholders a deployer replaces, never a value
    this repository fills in."""
    kustomization = yaml.safe_load((DEPLOY / "overlays/lab" / "kustomization.yaml").read_text())
    assert kustomization["images"] == [
        {
            "name": "ghcr.io/sentania-labs/crucible",
            "newTag": "REPLACE_ME_CRUCIBLE_TAG",
            "digest": "REPLACE_ME_CRUCIBLE_DIGEST",
        }
    ]


def test_the_lab_overlay_renders_from_a_copy_of_the_whole_tree(tmp_path: Path) -> None:
    """docs/deployment.md's instruction, proven: a deployer copies the whole
    `deploy/kubernetes` tree, keeping the relative paths, and `overlays/lab`'s
    `../../base` and `../../secret-shapes/sealed` references still resolve from that
    copy, wherever it lands. Copying `overlays/lab` alone would not."""
    if shutil.which("kubectl") is None:
        message = "kubectl is needed to render the deployment manifests"
        if os.environ.get("CRUCIBLE_MANIFESTS_REQUIRED"):
            raise AssertionError(message)
        pytest.skip(message)
    copy = tmp_path / "deploy" / "kubernetes"
    shutil.copytree(DEPLOY, copy)
    raw = subprocess.run(
        ["kubectl", "kustomize", str(copy / "overlays" / "lab")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [doc for doc in yaml.safe_load_all(raw) if doc]
    assert objects


def test_deploy_kind_derives_its_release_reference_from_the_base_pin() -> None:
    script = (ROOT / "tools/kind/deploy-kind.sh").read_text(encoding="utf-8")
    assert "deploy/kubernetes/base/kustomization.yaml" in script
    assert "release_image=$(awk" in script
    assert "ghcr.io/sentania-labs/crucible:latest" not in script


def test_the_rendered_crucible_containers_run_the_example_or_the_placeholder_pin(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """`latest` in the base and kind proof, the deployer's unresolved placeholder in the
    lab overlay: never a real tag or digest this repository chose."""
    expected = {"base": BASE_IMAGE, "overlays/lab": LAB_IMAGE, "overlays/kind": BASE_IMAGE}
    for target, image in expected.items():
        found = {
            container["image"]
            for _, spec in _pod_specs(rendered[target])
            for container in _containers(spec)
            if "sentania-labs/crucible" in container["image"]
        }
        assert found == {image}, f"{target}: {found}"


def test_the_app_key_is_mounted_on_the_control_plane_and_nowhere_else(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """12: the GitHub App key and the webhook secret are a Secret on the `crucible` pods
    only. Nothing in `crucible-workers` may name it, and the base ships no workload
    there at all."""
    for target in TARGETS:
        for obj in rendered[target]:
            if obj["kind"] not in {"Deployment", "StatefulSet", "Job", "Pod"}:
                continue
            namespace = obj["metadata"]["namespace"]
            spec = obj["spec"]["template"]["spec"] if obj["kind"] != "Pod" else obj["spec"]
            for volume in spec.get("volumes") or []:
                secret = (volume.get("secret") or {}).get("secretName")
                if secret == "crucible-github-app":
                    assert namespace == "crucible", f"{target}: {obj['metadata']['name']}"


def test_no_secret_value_is_committed(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """The lab overlay's Secret objects are SealedSecret placeholders and nothing else;
    the only plain Secret anywhere is the kind overlay's disposable database password."""
    for obj in rendered["overlays/lab"]:
        assert obj["kind"] != "Secret", "the lab overlay renders a plain Secret"
    for obj in _of_kind(rendered["overlays/lab"], "SealedSecret"):
        for value in obj["spec"]["encryptedData"].values():
            assert str(value).startswith("REPLACE_WITH_SEALED_"), obj["metadata"]["name"]
    plain = _of_kind(rendered["overlays/kind"], "Secret")
    assert {s["metadata"]["name"] for s in plain} == {"crucible-database"}


def test_gitops_delivers_no_harness_credential_secret(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """ADR 0015: the harness Secrets are the service's own, written by the login, the
    Hermes key and the sync-back. A copy GitOps also applied would drift, so no rendered
    target carries one in any form, and their documented shape is in no kustomization."""
    for target in TARGETS:
        for obj in rendered[target]:
            if obj["kind"] in {"Secret", "SealedSecret", "ExternalSecret"}:
                name = str(obj["metadata"]["name"])
                assert not name.startswith("crucible-harness-"), f"{target}: {name}"
    shapes = DEPLOY / "secret-shapes"
    assert (shapes / "service-owned-harnesses.yaml").is_file()
    for kustomization in DEPLOY.rglob("kustomization.yaml"):
        assert "harnesses.yaml" not in kustomization.read_text(), kustomization
    documented = yaml.safe_load((shapes / "service-owned-harnesses.yaml").read_text())
    assert documented is None, "the harness shape is documentation, never an object"


def test_the_argo_application_does_not_sync_automatically(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """C9: the first sync is a person looking at what is about to be created, because an
    unsealed placeholder is exactly what that look is for."""
    application = _named(rendered["argocd"], "Application", "crucible")
    policy = application["spec"].get("syncPolicy") or {}
    assert "automated" not in policy
    assert application["spec"]["source"]["path"] == "deploy/kubernetes/overlays/lab"


def test_the_ingress_names_no_real_host_and_creates_no_record(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """Operator rule 10 and 26 item 8: the route is provisioned and reported, and the
    public DNS record is the operator's alone. The base host is `.invalid`, which RFC
    2606 reserves, and the lab overlay's is a placeholder."""
    base = _named(rendered["base"], "Ingress", "crucible-api")
    assert [r["host"] for r in base["spec"]["rules"]] == ["crucible.invalid"]
    lab = _named(rendered["overlays/lab"], "Ingress", "crucible-api")
    assert [r["host"] for r in lab["spec"]["rules"]] == ["crucible.REPLACE_ME_LAB_DOMAIN"]


def test_the_supervisor_runs_exactly_one_replica(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """01, 26: one active supervisor, lease-guarded. Two would both be refused by the
    lease, so this is about not making the log say so every tick."""
    for target in ("base", "overlays/lab", "overlays/kind"):
        supervisor = _named(rendered[target], "Deployment", "crucible-supervisor")
        assert supervisor["spec"]["replicas"] == 1
        assert supervisor["spec"]["strategy"]["type"] == "Recreate"


def test_the_quota_reports_attempt_capacity_from_its_job_count(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """An attempt uses five Jobs, so the raw quota must be converted before display."""
    expected = {"base": "17", "overlays/lab": "32", "overlays/kind": "17"}
    for target, jobs in expected.items():
        quota = _named(rendered[target], "ResourceQuota", "crucible-workers")
        hard = quota["spec"]["hard"]
        assert hard["count/jobs.batch"] == jobs, target
        assert "pods" not in hard, target
        # The claim count is the structural attempt cap, one claim per attempt.
        assert "persistentvolumeclaims" in hard, target
        # Every quota leaves two extra Jobs of headroom above a multiple of five.
        assert int(hard["count/jobs.batch"]) % 5 == 2, target


LAB_STARTUP_PLACEHOLDER_KEYS = frozenset(
    {
        "CRUCIBLE_KUBERNETES__CLUSTER_DNS_IP",
        "CRUCIBLE_KUBERNETES__STORAGE_CLASS",
        "CRUCIBLE_KUBERNETES__IMAGE_PULL_SECRET",
        "CRUCIBLE_SERVICE__RENDER_TIMEZONE",
        "CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_NAMESPACE",
        "CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_POD_LABELS",
        "CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_PORT",
    }
)


def test_the_lab_config_has_no_unresolved_probe_image(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """Only cluster facts may remain unresolved in a setting wiring reads at startup."""
    settings = _named(rendered["overlays/lab"], "ConfigMap", "crucible-settings")["data"]
    assert settings["CRUCIBLE_KUBERNETES__PROBE_IMAGE"] == ""
    unresolved = {key: value for key, value in settings.items() if "REPLACE_ME_" in str(value)}
    assert set(unresolved) <= LAB_STARTUP_PLACEHOLDER_KEYS


def test_the_base_config_carries_no_lab_local_endpoint_address(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """The lab gateway's address is a cluster fact; the base is inherited by kind too."""
    for target in ("base", "overlays/kind"):
        settings = _named(rendered[target], "ConfigMap", "crucible-settings")["data"]
        assert settings["CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_CIDRS"] == "[]", target
        assert settings["CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_NAMESPACE"] == "", target


def test_every_config_omits_or_seeds_the_declared_claude_code_credential_mode(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """Mount mode is an admin setting; deployment input is an optional seed only."""
    declared_mode = ClaudeCodeAdapter().credential_spec().minimum_mode.value
    for target in ("base", "overlays/lab", "overlays/kind"):
        settings = _named(rendered[target], "ConfigMap", "crucible-settings")["data"]
        seed = settings.get("CRUCIBLE_CREDENTIALS__CLAUDE_CODE__MOUNT_MODE")
        assert seed is None or seed == declared_mode, target


def test_the_kind_overlay_renders_times_in_america_chicago(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """hades#148: the kind proofs are read in the lab's local zone; the base stays UTC."""
    kind = _named(rendered["overlays/kind"], "ConfigMap", "crucible-settings")["data"]
    base = _named(rendered["base"], "ConfigMap", "crucible-settings")["data"]
    assert kind["CRUCIBLE_SERVICE__RENDER_TIMEZONE"] == "America/Chicago"
    assert base["CRUCIBLE_SERVICE__RENDER_TIMEZONE"] == "UTC"


def test_every_config_allows_dns_by_the_resolvers_pods(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """crucible#91: the kube-dns selector is in every rendered config, so a cluster whose
    CNI translates the DNS service address first still resolves names."""
    for target in ("base", "overlays/lab", "overlays/kind"):
        settings = _named(rendered[target], "ConfigMap", "crucible-settings")["data"]
        assert settings["CRUCIBLE_KUBERNETES__DNS_NAMESPACE"] == "kube-system", target
        assert json.loads(settings["CRUCIBLE_KUBERNETES__DNS_POD_LABELS"]) == {
            "k8s-app": "kube-dns"
        }, target


def test_the_lab_overlay_allows_its_in_cluster_gateway_by_selector(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    """The lab runs Cilium with kube-proxy replacement and an in-cluster LiteLLM: the
    gateway is a selector the deployer fills in, and no address rule is left to rot."""
    settings = _named(rendered["overlays/lab"], "ConfigMap", "crucible-settings")["data"]
    assert settings["CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_CIDRS"] == "[]"
    assert settings["CRUCIBLE_KUBERNETES__LOCAL_ENDPOINT_NAMESPACE"].startswith("REPLACE_ME_")


def test_the_kubernetes_provider_is_on_and_docker_is_off(
    rendered: dict[str, list[dict[str, Any]]],
) -> None:
    for target in ("base", "overlays/lab", "overlays/kind"):
        settings = _named(rendered[target], "ConfigMap", "crucible-settings")["data"]
        assert settings["CRUCIBLE_KUBERNETES__ENABLED"] == "true"
        assert settings["CRUCIBLE_DOCKER__ENABLED"] == "false"
        assert settings["CRUCIBLE_KUBERNETES__WORKERS_NAMESPACE"] == "crucible-workers"
        assert settings["CRUCIBLE_KUBERNETES__SERVICE_ACCOUNT"] == "crucible-worker"


# Keys an overlay may add that are not Crucible settings at all. `SSL_CERT_FILE` is
# Python's own, and only the generated kind overlay sets it.
NON_SETTING_KEYS = frozenset({"SSL_CERT_FILE"})


def _resolve_setting(key: str) -> None:
    """Walk one environment key through the real Settings model, or raise.

    A setting pydantic-settings does not know is dropped in silence (`extra` is
    `ignore`), so a typo in a ConfigMap is a deployment that quietly runs on the default.
    """
    # `credentials` and `harnesses` are dicts keyed by harness name, so the segment after
    # one of them is a key of the operator's choosing and the one after that is a field
    # of the dict's value type.
    keyed = {"credentials": CredentialSettings, "harnesses": HarnessSettings}
    parts = key.removeprefix("CRUCIBLE_").lower().split("__")
    model: Any = Settings
    previous = ""
    for part in parts:
        if previous in keyed:
            model = keyed[previous]
            previous = part
            continue
        fields = getattr(model, "model_fields", None)
        if fields is None or part not in fields:
            raise AssertionError(f"{key} names no setting: {part!r} is not a field")
        model = fields[part].annotation
        previous = part
    if previous in keyed:
        raise AssertionError(f"{key} names a section and no field inside it")


@pytest.mark.parametrize("target", ("base", "overlays/lab", "overlays/kind"))
def test_every_configmap_setting_is_one_the_application_reads(
    rendered: dict[str, list[dict[str, Any]]], target: str
) -> None:
    """The overlays are where the typo risk lives, so all three are walked."""
    data = _named(rendered[target], "ConfigMap", "crucible-settings")["data"]
    for key in data:
        if key in NON_SETTING_KEYS:
            continue
        assert key.startswith("CRUCIBLE_"), key
        _resolve_setting(key)


def test_the_setting_walk_rejects_a_key_the_model_does_not_carry() -> None:
    """The walk is only worth having if it fails; a `break` on an unknown segment would
    make every key past the first one pass."""
    for key in (
        "CRUCIBLE_SERVICE__NONSENSE",
        "CRUCIBLE_SERVICE__BIND__NONSENSE",
        "CRUCIBLE_KUBERNETES__PROBE_IMAGE__NONSENSE",
        "CRUCIBLE_HARNESSES__CODEX__NONSENSE",
    ):
        with pytest.raises(AssertionError):
            _resolve_setting(key)
    # And the real shapes still resolve, including the two dict-keyed sections.
    for key in (
        "CRUCIBLE_SERVICE__BIND",
        "CRUCIBLE_KUBERNETES__PROBE_IMAGE",
        "CRUCIBLE_HARNESSES__CODEX__ENABLED",
        "CRUCIBLE_CREDENTIALS__CLAUDE_CODE__MOUNT_MODE",
    ):
        _resolve_setting(key)
