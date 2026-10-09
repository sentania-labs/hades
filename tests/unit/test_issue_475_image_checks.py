"""Hades's own rootless BuildKit and the image checks that need it (hades #475).

Four things, each with its own proof:

- the pre-PR gate `image_checks_required`: a diff under the image build inputs fails
  unless the contract requires both `make images-check` and `make registry-check`;
- the per-attempt egress a contract that requires them earns: the BuildKit Service's
  pods in `hades-buildkit` on 1234, and HTTPS to GHCR and the host it redirects blob
  reads to; a contract that does not require them earns none of it;
- `images/build.sh` through `BUILDKIT_HOST`: the buildctl invocation carries exactly the
  build arguments, labels and outputs the docker-container path carries, so the tag
  and the digest cannot depend on the builder;
- the deployment: the rootless image pin, the isolated namespace, and the kind tier
  carrying the same.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.kubernetes import (
    BUILDKIT_HOST,
    BUILDKIT_NAMESPACE,
    BUILDKIT_POD_LABELS,
    IMAGE_CHECK_HOSTS,
    KubernetesProvider,
    requires_image_checks,
)
from crucible.application.gates import configured_pre_pr_gates
from crucible.contracts.policy import parse_policy
from crucible.domain.gates import (
    OPTIONAL_PRE_PR_GATES,
    PRE_PR_GATES,
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    evaluate_gate,
)
from tests.unit.kubernetes_fixtures import spec

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "policies" / "hades-self-hosting.yaml"
BUILDKIT_DIR = ROOT / "deploy" / "kubernetes" / "base" / "buildkit"
CHECKS = [
    {"id": "V4", "command": "make images-check", "expect_exit": 0},
    {"id": "V5", "command": "make registry-check", "expect_exit": 0},
]


def _pins() -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in (ROOT / "images" / "pins.env").read_text().splitlines()
        if line and not line.startswith("#")
    )


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _gate(contract: dict[str, Any], paths: list[str]) -> Any:
    evidence = EvidenceItem(1, "diff_paths", "crucible", True, {"paths": paths})
    return evaluate_gate(
        GateName.IMAGE_CHECKS_REQUIRED, GateInput(contract, {}, "a" * 40, (evidence,))
    )


# ----- the gate -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "images/worker/Dockerfile",
        "images/pins.env",
        "images/manifest.env",
        "images/build.sh",
        "tools/images/images.sh",
        "tools/harness/hermes.sh",
    ],
)
def test_an_image_input_change_without_both_checks_fails_naming_them(path: str) -> None:
    outcome = _gate({"required_verification": [{"command": "make lint"}]}, [path])
    assert outcome.result is GateResult.FAIL
    assert "make images-check" in outcome.detail
    assert "make registry-check" in outcome.detail


def test_one_check_without_the_other_still_fails_naming_the_missing_one() -> None:
    contract = {"required_verification": [{"command": "make images-check"}]}
    outcome = _gate(contract, ["images/pins.env"])
    assert outcome.result is GateResult.FAIL
    assert "['make registry-check']" in outcome.detail


def test_an_image_input_change_with_both_checks_passes() -> None:
    outcome = _gate({"required_verification": CHECKS}, ["tools/images/images.sh"])
    assert outcome.result is GateResult.PASS


def test_a_diff_outside_the_image_inputs_passes_whatever_the_contract_requires() -> None:
    contract = {"required_verification": [{"command": "make lint"}]}
    outcome = _gate(contract, ["crucible/domain/gates.py", "docs/images.md"])
    assert outcome.result is GateResult.PASS


def test_the_gate_is_optional_so_a_policy_from_before_it_is_not_judged_by_it() -> None:
    """A contract is judged by the gates its policy version lists (05b). The gate was
    added after versions became immutable, so a stored version that omits it, like the
    lab's hades-self-hosting versions before this change, keeps its list and the gate
    never runs for a contract admitted under it; the shipped version 2 lists it."""
    assert GateName.IMAGE_CHECKS_REQUIRED in OPTIONAL_PRE_PR_GATES
    assert GateName.IMAGE_CHECKS_REQUIRED not in PRE_PR_GATES
    document = yaml.safe_load(EXAMPLE.read_text())
    assert document["version"] == 2
    listed = parse_policy(document).gates.pre_pr
    assert "image_checks_required" in listed
    assert "image_checks_required" in configured_pre_pr_gates(document)
    older = {**document, "version": 1}
    older["gates"] = {
        **document["gates"],
        "pre_pr": [g for g in listed if g != "image_checks_required"],
    }
    assert "image_checks_required" not in parse_policy(older).gates.pre_pr
    assert "image_checks_required" not in configured_pre_pr_gates(older)
    assert {"buildctl", "crane"} <= set(document["repository"]["required_programs"])


# ----- the egress rule --------------------------------------------------------


def _provider() -> KubernetesProvider:
    provider = object.__new__(KubernetesProvider)
    provider.config = type(
        "Config",
        (),
        {
            "egress": type("E", (), {"endpoint_in_cluster": False})(),
            "namespace": "hades-workers",
            "control_namespace": "hades",
            "buildkit_namespace": BUILDKIT_NAMESPACE,
        },
    )()
    provider.harnesses = type("Harnesses", (), {"get": lambda *_: None})()
    return provider


def test_requires_image_checks_needs_both_commands() -> None:
    assert requires_image_checks({"required_verification": CHECKS})
    assert not requires_image_checks({"required_verification": CHECKS[:1]})
    assert not requires_image_checks({"required_verification": []})
    assert not requires_image_checks({})
    assert BUILDKIT_HOST == "tcp://hades-buildkit.hades-buildkit.svc:1234"


@pytest.mark.parametrize("role", [k8sspec.ROLE_WORKER, k8sspec.ROLE_VERIFIER])
def test_a_contract_requiring_the_image_checks_gets_buildkit_and_the_registry(
    role: str,
) -> None:
    provider = _provider()
    required = spec()
    required.contract["required_verification"] = CHECKS
    plan = provider._egress_plan(required, role)
    assert plan.buildkit_selector == k8sspec.PeerSelector.of(
        BUILDKIT_NAMESPACE, BUILDKIT_POD_LABELS
    )
    assert plan.buildkit_port == 1234
    assert set(IMAGE_CHECK_HOSTS) <= set(plan.hosts)
    assert "pkg-containers.githubusercontent.com" in plan.hosts
    # The selector is one the provider's own guard accepts: the builder's namespace is
    # neither the workers' nor the control plane's.
    k8sspec.check_selector(
        plan.buildkit_selector, what="BuildKit", protected_namespaces=provider._protected()
    )
    policy = k8sspec.egress_policy(
        name="image-checks",
        namespace="hades-workers",
        object_labels={},
        attempt_id="A1",
        role=role,
        plan=plan,
        dns_server="",
    )
    buildkit_rules = [
        rule
        for rule in policy["spec"]["egress"]
        if any("podSelector" in peer for peer in rule.get("to", []))
    ]
    assert len(buildkit_rules) == 1
    (rule,) = buildkit_rules
    assert rule["ports"] == [{"protocol": "TCP", "port": 1234}]
    assert rule["to"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": BUILDKIT_NAMESPACE}
            },
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "hades-buildkit"}},
        }
    ]


@pytest.mark.parametrize("role", [k8sspec.ROLE_WORKER, k8sspec.ROLE_VERIFIER])
def test_a_contract_without_the_image_checks_gets_neither(role: str) -> None:
    provider = _provider()
    for contract_checks in ([], CHECKS[:1], CHECKS[1:]):
        document = spec()
        document.contract["required_verification"] = contract_checks
        plan = provider._egress_plan(document, role)
        assert plan.buildkit_selector is None
        assert not set(IMAGE_CHECK_HOSTS) & set(plan.hosts)
        policy = k8sspec.egress_policy(
            name="no-image-checks",
            namespace="hades-workers",
            object_labels={},
            attempt_id="A1",
            role=role,
            plan=plan,
            dns_server="",
        )
        assert not any(
            "podSelector" in peer
            for rule in policy["spec"]["egress"]
            for peer in rule.get("to", [])
        )


def test_the_roles_that_never_run_the_checks_never_get_the_builder() -> None:
    provider = _provider()
    required = spec()
    required.contract["required_verification"] = CHECKS
    for role in (k8sspec.ROLE_PREPARER, k8sspec.ROLE_PUBLISHER, k8sspec.ROLE_COLLECTOR):
        plan = provider._egress_plan(required, role)
        assert plan.buildkit_selector is None
        assert not set(IMAGE_CHECK_HOSTS) & set(plan.hosts)


# ----- images/build.sh through BUILDKIT_HOST ----------------------------------

_FAKE_BUILDCTL = r"""#!/usr/bin/env bash
# Records every build invocation and writes the archives build.sh inspects afterwards.
set -euo pipefail
printf '%s\n' "$@" > "$FAKE_LOG_DIR/buildctl.$(date +%s%N).args"
addr=""; verb=""
while [ $# -gt 0 ]; do
    case "$1" in
        --addr) addr=$2; shift 2 ;;
        debug) verb=debug; shift ;;
        build) verb=build; shift; break ;;
        *) shift ;;
    esac
done
[ "$addr" = "$FAKE_BUILDKIT_HOST" ] || { echo "fake buildctl: wrong addr $addr" >&2; exit 2; }
[ "$verb" = build ] || exit 0
oci=""; docker=""
for arg in "$@"; do
    case "$arg" in
        type=oci,*dest=*) oci=${arg##*dest=} ;;
        type=docker,*dest=*) docker=${arg##*dest=} ;;
        type=local,dest=*) dest=${arg#type=local,dest=}; mkdir -p "${dest%%,*}" ;;
    esac
done
[ -n "$oci" ] && [ -n "$docker" ] || { echo "fake buildctl: outputs missing" >&2; exit 2; }
scratch=$(mktemp -d); trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/blobs/sha256"
manifest='{"config":{"digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111"}}'
hex=$(printf '%s' "$manifest" | sha256sum | cut -c1-64)
printf '%s' "$manifest" > "$scratch/blobs/sha256/$hex"
printf '{"manifests":[{"digest":"sha256:%s"}]}\n' "$hex" > "$scratch/index.json"
tar -cf "$oci" -C "$scratch" index.json blobs
printf 'docker archive' > "$docker"
"""

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# The docker-container path's Docker CLI: records the build and answers the rest.
set -euo pipefail
case "${1:-}:${2:-}" in
    buildx:inspect) exit 1 ;;
    buildx:create) exit 0 ;;
    buildx:--builder)
        printf '%s\n' "$@" > "$FAKE_LOG_DIR/docker.$(date +%s%N).args"
        oci=""
        for arg in "$@"; do
            case "$arg" in type=oci,*dest=*) oci=${arg##*dest=} ;; esac
        done
        scratch=$(mktemp -d); trap 'rm -rf "$scratch"' EXIT
        mkdir -p "$scratch/blobs/sha256"
        manifest='{"config":{"digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111"}}'
        hex=$(printf '%s' "$manifest" | sha256sum | cut -c1-64)
        printf '%s' "$manifest" > "$scratch/blobs/sha256/$hex"
        printf '{"manifests":[{"digest":"sha256:%s"}]}\n' "$hex" > "$scratch/index.json"
        tar -cf "$oci" -C "$scratch" index.json blobs
        ;;
    load:-q) exit 0 ;;
    image:inspect)
        case "$*" in
            *'{{.Id}}'*) printf 'sha256:%s\n' "$(printf '1%.0s' $(seq 64))" ;;
            *'{{.Size}}'*) printf '0\n' ;;
            *) exit 2 ;;
        esac
        ;;
    *) echo "fake docker: unexpected call: $*" >&2; exit 2 ;;
esac
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _staged_images(tmp_path: Path) -> Path:
    """A copy of images/ with every file newer than SOURCE_DATE_EPOCH, as a checkout is."""
    stage = tmp_path / "images"
    shutil.copytree(ROOT / "images", stage, ignore=shutil.ignore_patterns("out"))
    now = time.time()
    for path in stage.rglob("*"):
        os.utime(path, (now, now))
    return stage


def _run_build(stage: Path, bindir: Path, log: Path, extra_env: dict[str, str]) -> str:
    out = log / "out"
    out.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_LOG_DIR": str(log),
        "OUT": str(out),
        "MANIFEST": str(log / "manifest.env"),
        **extra_env,
    }
    env.pop("BUILDKIT_HOST", None)
    env.update(extra_env)
    result = subprocess.run(
        [str(stage / "build.sh"), "worker"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _flags(args: list[str], flag: str, prefix: str = "") -> set[str]:
    values = {args[i + 1] for i, arg in enumerate(args[:-1]) if arg == flag}
    return {v[len(prefix) :] for v in values if v.startswith(prefix)}


def test_build_sh_passes_buildctl_exactly_what_it_passes_buildx(tmp_path: Path) -> None:
    """AC3: with BUILDKIT_HOST set, build.sh builds through buildctl against that address,
    with the dockerfile frontend, the same context and Dockerfile, the same platform,
    build arguments, labels and the two rewritten-timestamp outputs as the
    docker-container path, and records the same tag."""
    stage = _staged_images(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "buildctl", _FAKE_BUILDCTL)
    _executable(bindir / "docker", _FAKE_DOCKER)
    host = "tcp://hades-buildkit.hades-buildkit.svc:1234"

    remote_log = tmp_path / "remote"
    remote_log.mkdir()
    remote_out = _run_build(
        stage, bindir, remote_log, {"BUILDKIT_HOST": host, "FAKE_BUILDKIT_HOST": host}
    )
    local_log = tmp_path / "local"
    local_log.mkdir()
    local_out = _run_build(stage, bindir, local_log, {"FAKE_BUILDKIT_HOST": ""})

    buildctl_calls = sorted(remote_log.glob("buildctl.*.args"))
    assert len(buildctl_calls) == 2, "one readiness probe of the daemon, then one build"
    probe = buildctl_calls[0].read_text().splitlines()
    assert probe == ["--addr", host, "debug", "workers"]
    remote = buildctl_calls[1].read_text().splitlines()
    assert not list(remote_log.glob("docker.*.args")), "the remote path never calls docker"
    (local_call,) = local_log.glob("docker.*.args")
    local = local_call.read_text().splitlines()

    assert remote[:3] == ["--addr", host, "build"]
    assert _flags(remote, "--frontend") == {"dockerfile.v0"}
    assert _flags(remote, "--local") == {f"context={stage}", f"dockerfile={stage / 'worker'}"}
    opts = _flags(remote, "--opt")
    assert "filename=Dockerfile" in opts
    assert "platform=linux/amd64" in opts
    assert local[-1] == str(stage) and local[local.index("-f") + 1] == str(
        stage / "worker" / "Dockerfile"
    )
    assert _flags(local, "--platform") == {"linux/amd64"}

    remote_args = {o[len("build-arg:") :] for o in opts if o.startswith("build-arg:")}
    local_args = _flags(local, "--build-arg")
    assert remote_args == local_args
    pins = _pins()
    for name in ("BUILDCTL_VERSION", "BUILDCTL_SHA256", "CRANE_VERSION", "CRANE_SHA256"):
        assert f"{name}={pins[name]}" in remote_args
    remote_labels = {o[len("label:") :] for o in opts if o.startswith("label:")}
    local_labels = _flags(local, "--label")
    assert remote_labels == local_labels
    assert any(label.startswith("crucible.build_inputs=sha256:") for label in remote_labels)
    assert any(label.startswith("crucible.harnesses=") for label in remote_labels)

    tag = shlex.split(local_out)[0]
    assert shlex.split(remote_out)[0] == tag
    remote_outputs = _flags(remote, "--output")
    local_outputs = _flags(local, "--output")
    for kind in ("oci", "docker"):
        (remote_output,) = {o for o in remote_outputs if o.startswith(f"type={kind},")}
        (local_output,) = {o for o in local_outputs if o.startswith(f"type={kind},")}
        assert (
            "rewrite-timestamp=true" in remote_output and "rewrite-timestamp=true" in local_output
        )
        assert f"name={tag}" in remote_output
        assert remote_output.rsplit("dest=", 1)[1].endswith(f".{kind}.tar")
        assert local_output.rsplit("dest=", 1)[1].endswith(f".{kind}.tar")
    assert "--provenance=false" in local and "--sbom=false" in local
    assert "--no-cache" not in remote and "--no-cache" not in local
    assert "-t" in local and local[local.index("-t") + 1] == tag

    remote_manifest = (remote_log / "manifest.env").read_text()
    local_manifest = (local_log / "manifest.env").read_text()
    assert [line for line in remote_manifest.splitlines() if not line.startswith("#")] == [
        line for line in local_manifest.splitlines() if not line.startswith("#")
    ]
    assert f"WORKER={tag}\n" in remote_manifest
    assert (
        tag
        == dict(
            line.split("=", 1)
            for line in (ROOT / "images" / "manifest.env").read_text().splitlines()
            if line and not line.startswith("#")
        )["WORKER"]
    ), "images/manifest.env names the tag of the committed inputs"


def test_build_sh_forwards_no_cache_and_the_layer_cache_to_buildctl(tmp_path: Path) -> None:
    stage = _staged_images(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "buildctl", _FAKE_BUILDCTL)
    host = "tcp://buildkit.example:1234"
    log = tmp_path / "log"
    log.mkdir()
    cache = tmp_path / "cache"
    (cache / "worker").mkdir(parents=True)
    _run_build(
        stage,
        bindir,
        log,
        {
            "BUILDKIT_HOST": host,
            "FAKE_BUILDKIT_HOST": host,
            "NO_CACHE": "1",
            "CACHE_DIR": str(cache),
        },
    )
    remote = sorted(log.glob("buildctl.*.args"))[-1].read_text().splitlines()
    assert "--no-cache" in remote
    assert _flags(remote, "--import-cache") == {f"type=local,src={cache / 'worker'}"}
    assert _flags(remote, "--export-cache") == {f"type=local,dest={cache / 'worker.new'},mode=max"}
    assert (cache / "worker").is_dir() and not (cache / "worker.new").exists(), (
        "the exported cache replaced the imported one, as on the docker-container path"
    )


def test_build_sh_refuses_a_buildkit_host_without_buildctl_or_without_a_daemon(
    tmp_path: Path,
) -> None:
    stage = _staged_images(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = {**os.environ, "PATH": str(bindir), "BUILDKIT_HOST": "tcp://nowhere:1234"}
    for tool in (
        "bash",
        "sed",
        "grep",
        "find",
        "sort",
        "cat",
        "sha256sum",
        "cut",
        "date",
        "mkdir",
        "tr",
        "paste",
        "uniq",
        "head",
        "env",
        "printf",
        "dirname",
        "readlink",
    ):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    result = subprocess.run(
        [str(stage / "build.sh"), "worker"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "buildctl is not on PATH" in result.stderr

    _executable(bindir / "buildctl", "#!/usr/bin/env bash\nexit 1\n")
    result = subprocess.run(
        [str(stage / "build.sh"), "worker"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "no BuildKit answers at tcp://nowhere:1234" in result.stderr


# ----- the deployment -----------------------------------------------------------


def test_the_base_ships_the_rootless_buildkit_in_its_own_namespace() -> None:
    pins = _pins()
    assert pins["BUILDKIT_ROOTLESS_IMAGE"].startswith("moby/buildkit:")
    assert pins["BUILDKIT_ROOTLESS_IMAGE"].endswith("-rootless")
    assert f"v{pins['BUILDCTL_VERSION']}-rootless" in pins["BUILDKIT_ROOTLESS_IMAGE"], (
        "the daemon is the same release as the worker's buildctl"
    )
    assert pins["BUILDKIT_IMAGE"] != pins["BUILDKIT_ROOTLESS_IMAGE"]

    kustomization = yaml.safe_load((BUILDKIT_DIR / "kustomization.yaml").read_text())
    assert kustomization["resources"] == ["namespace.yaml", "networkpolicy.yaml", "buildkit.yaml"]
    base = yaml.safe_load((ROOT / "deploy/kubernetes/base/kustomization.yaml").read_text())
    assert "buildkit" in base["resources"]

    (namespace,) = _documents(BUILDKIT_DIR / "namespace.yaml")
    assert namespace["metadata"]["name"] == BUILDKIT_NAMESPACE
    labels = namespace["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "privileged"
    assert labels["pod-security.kubernetes.io/warn"] == "baseline"
    (control,) = _documents(ROOT / "deploy/kubernetes/base/crucible/namespace.yaml")
    assert control["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"

    objects = {doc["kind"]: doc for doc in _documents(BUILDKIT_DIR / "buildkit.yaml")}
    assert set(objects) == {"PersistentVolumeClaim", "Service", "Deployment"}
    for doc in objects.values():
        assert doc["metadata"]["namespace"] == BUILDKIT_NAMESPACE
    service = objects["Service"]
    assert service["metadata"]["name"] == "hades-buildkit"
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == dict(BUILDKIT_POD_LABELS)
    assert service["spec"]["ports"] == [
        {"name": "buildkit", "port": 1234, "targetPort": "buildkit"}
    ]
    assert objects["PersistentVolumeClaim"]["spec"]["resources"]["requests"]["storage"] == "50Gi"

    pod = objects["Deployment"]["spec"]["template"]
    assert pod["metadata"]["labels"] == dict(BUILDKIT_POD_LABELS)
    assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
    assert pod["spec"]["securityContext"]["runAsUser"] == 1000
    assert pod["spec"]["securityContext"]["seccompProfile"] == {"type": "Unconfined"}
    (container,) = pod["spec"]["containers"]
    assert container["image"] == pins["BUILDKIT_ROOTLESS_IMAGE"]
    assert container["args"] == ["--addr", "tcp://0.0.0.0:1234", "--oci-worker-no-process-sandbox"]
    assert container["securityContext"]["privileged"] is False
    assert container["securityContext"]["appArmorProfile"] == {"type": "Unconfined"}
    assert container["readinessProbe"]["exec"]["command"] == [
        "buildctl",
        "--addr",
        "tcp://127.0.0.1:1234",
        "debug",
        "workers",
    ]
    assert container["resources"]["requests"] == {"cpu": "500m", "memory": "1Gi"}
    assert container["ports"] == [{"name": "buildkit", "containerPort": 1234}]
    assert pod["spec"]["volumes"] == [
        {"name": "cache", "persistentVolumeClaim": {"claimName": "hades-buildkit-cache"}}
    ]

    (policy,) = _documents(BUILDKIT_DIR / "networkpolicy.yaml")
    assert policy["metadata"]["namespace"] == BUILDKIT_NAMESPACE
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    (rule,) = policy["spec"]["ingress"]
    assert rule["from"] == [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "hades-workers"}}}
    ]
    assert rule["ports"] == [{"protocol": "TCP", "port": 1234}]


def test_the_kind_tier_starts_the_same_buildkit() -> None:
    pins = _pins()
    docs = _documents(ROOT / "deploy" / "kind" / "workers.yaml")
    by_kind = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}
    namespace = by_kind[("Namespace", BUILDKIT_NAMESPACE)]
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "privileged"
    assert ("Namespace", "hades") not in by_kind, "e2e-kind.sh creates it; nothing relabels it"
    deployment = by_kind[("Deployment", "hades-buildkit")]
    assert deployment["metadata"]["namespace"] == BUILDKIT_NAMESPACE
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    assert container["image"] == pins["BUILDKIT_ROOTLESS_IMAGE"]
    assert container["securityContext"]["privileged"] is False
    assert deployment["spec"]["template"]["spec"]["securityContext"]["seccompProfile"] == {
        "type": "Unconfined"
    }
    service = by_kind[("Service", "hades-buildkit")]
    assert service["metadata"]["namespace"] == BUILDKIT_NAMESPACE
    assert service["spec"]["ports"][0]["port"] == 1234
    claim = by_kind[("PersistentVolumeClaim", "hades-buildkit-cache")]
    assert claim["spec"]["storageClassName"] == "standard"
    policy = by_kind[("NetworkPolicy", "hades-buildkit")]
    assert policy["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "hades-workers"
    }
    kind_storage = _documents(ROOT / "deploy/kubernetes/overlays/kind/storage.yaml")
    assert any(
        doc["metadata"]["name"] == "hades-buildkit-cache"
        and doc["metadata"]["namespace"] == BUILDKIT_NAMESPACE
        and doc["spec"]["storageClassName"] == "standard"
        for doc in kind_storage
    )


def test_the_worker_image_pins_buildctl_and_crane_the_way_gitleaks_is_pinned() -> None:
    pins = _pins()
    dockerfile = (ROOT / "images" / "worker" / "Dockerfile").read_text()
    for name in ("BUILDCTL_VERSION", "BUILDCTL_SHA256", "CRANE_VERSION", "CRANE_SHA256"):
        assert f"ARG {name}={pins[name]}\n" in dockerfile
    assert len(pins["BUILDCTL_SHA256"]) == 64 and len(pins["CRANE_SHA256"]) == 64
    assert "--checksum=sha256:${BUILDCTL_SHA256}" in dockerfile
    assert (
        "https://github.com/moby/buildkit/releases/download/v${BUILDCTL_VERSION}/"
        "buildkit-v${BUILDCTL_VERSION}.linux-amd64.tar.gz" in dockerfile
    )
    assert "COPY --from=fetch-toolchain /fetch/bin/buildctl /usr/local/bin/buildctl" in dockerfile
    assert "COPY --from=fetch-toolchain /fetch/bin/crane /usr/local/bin/crane" in dockerfile
    service = (ROOT / "Dockerfile").read_text()
    assert f"ARG CRANE_VERSION={pins['CRANE_VERSION']}\n" in service
    assert f"ARG CRANE_SHA256={pins['CRANE_SHA256']}\n" in service
    build = (ROOT / "images" / "build.sh").read_text()
    for name in ("BUILDCTL_VERSION", "BUILDCTL_SHA256", "CRANE_VERSION", "CRANE_SHA256"):
        assert name in build.split("arg_names=(", 1)[1].split(")", 1)[0]


def test_crane_fetch_uses_the_pinned_crane_already_on_path(tmp_path: Path) -> None:
    """A worker Pod has no github.com: `make registry-check` there runs the image's crane."""
    pins = _pins()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "crane", f"#!/usr/bin/env bash\necho {pins['CRANE_VERSION']}\n")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    result = subprocess.run(
        [str(ROOT / "tools" / "crane" / "fetch.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(bindir / "crane")
    _executable(bindir / "crane", "#!/usr/bin/env bash\necho 0.0.1\n")
    _executable(bindir / "curl", "#!/usr/bin/env bash\necho 'fake curl: no network' >&2\nexit 7\n")
    result = subprocess.run(
        [str(ROOT / "tools" / "crane" / "fetch.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "another version is not the pinned binary; the fetch runs"
    assert "fake curl" in result.stderr
