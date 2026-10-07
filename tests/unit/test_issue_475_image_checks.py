"""Regression coverage for Hades's own rootless BuildKit (issue 475)."""

from pathlib import Path

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.kubernetes import BUILDKIT_HOST, KubernetesProvider
from crucible.domain.gates import EvidenceItem, GateInput, GateName, GateResult, evaluate_gate
from tests.unit.kubernetes_fixtures import spec

ROOT = Path(__file__).resolve().parents[2]
CHECKS = [
    {"id": "V4", "command": "make images-check", "expect_exit": 0},
    {"id": "V5", "command": "make registry-check", "expect_exit": 0},
]


def test_image_diff_gate_names_both_missing_checks() -> None:
    contract = {"required_verification": [{"command": "make lint"}]}
    evidence = EvidenceItem(1, "diff_paths", "crucible", True, {"paths": ["images/pins.env"]})
    outcome = evaluate_gate(
        GateName.IMAGE_CHECKS_REQUIRED, GateInput(contract, {}, "a" * 40, (evidence,))
    )
    assert outcome.result is GateResult.FAIL
    assert "make images-check" in outcome.detail
    assert "make registry-check" in outcome.detail


def test_image_diff_gate_passes_with_both_checks() -> None:
    contract = {"required_verification": CHECKS}
    evidence = EvidenceItem(
        1, "diff_paths", "crucible", True, {"paths": ["tools/images/images.sh"]}
    )
    outcome = evaluate_gate(
        GateName.IMAGE_CHECKS_REQUIRED, GateInput(contract, {}, "a" * 40, (evidence,))
    )
    assert outcome.result is GateResult.PASS


def test_buildkit_egress_only_when_both_checks_are_required() -> None:
    provider = object.__new__(KubernetesProvider)
    provider.config = type(
        "Config", (), {"egress": type("E", (), {"endpoint_in_cluster": False})()}
    )()
    provider.harnesses = type("Harnesses", (), {"get": lambda *_: None})()

    required = spec()
    required.contract["required_verification"] = CHECKS
    plan = provider._egress_plan(required, k8sspec.ROLE_VERIFIER)
    assert plan.buildkit_selector == k8sspec.PeerSelector.of(
        "crucible", {"app.kubernetes.io/name": "crucible-buildkit"}
    )
    assert "ghcr.io" in plan.hosts
    policy = k8sspec.egress_policy(
        name="image-checks",
        namespace="crucible-workers",
        object_labels={},
        attempt_id="A1",
        role=k8sspec.ROLE_VERIFIER,
        plan=plan,
        dns_server="",
    )
    buildkit_rule = next(
        rule
        for rule in policy["spec"]["egress"]
        if any("podSelector" in peer for peer in rule.get("to", []))
    )
    assert buildkit_rule["ports"] == [{"protocol": "TCP", "port": 1234}]
    assert buildkit_rule["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "crucible"
    }

    ordinary = spec()
    plan = provider._egress_plan(ordinary, k8sspec.ROLE_VERIFIER)
    assert plan.buildkit_selector is None
    assert "ghcr.io" not in plan.hosts


def test_images_script_has_remote_buildctl_and_unchanged_local_buildx_paths() -> None:
    script = (ROOT / "images" / "build.sh").read_text()
    assert 'buildctl --addr "$BUILDKIT_HOST" build' in script
    assert 'buildx --builder "$builder" build' in script
    assert "type=oci,rewrite-timestamp=true" in script
    assert "type=docker,rewrite-timestamp=true" in script
    assert BUILDKIT_HOST == "tcp://crucible-buildkit.crucible.svc:1234"
