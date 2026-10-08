"""PolicyV1 and RoutingPolicyV1 validation (05b). Each rule has a failing fixture."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from crucible.adapters.persistence.migrations.versions import (
    _0001_walking_skeleton as m1,
)
from crucible.adapters.persistence.migrations.versions import (
    _0004_gates_and_acceptance as m4,
)
from crucible.adapters.persistence.migrations.versions import (
    _0009_administration as m9,
)
from crucible.adapters.persistence.migrations.versions import _0011_class_routing as m11
from crucible.application.errors import ContractValidationError
from crucible.application.policies import validate_policy
from crucible.contracts.policy import (
    PolicyV1,
    RoutingPolicyV1,
    parse_policy,
    parse_routing_policy,
    window_seconds,
)
from crucible.domain.gates import ALL_GATES, DEFAULT_ADVISORY_GATES

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "policies" / "default-software.yaml"


def seeded_policy() -> dict[str, Any]:
    """The C1 seed as migrations 0004 and 0007 leave it: the routing section 0001 lacked,
    the round counting 0004 corrected, and the accepted signals 0007 corrected."""
    document: dict[str, Any] = copy.deepcopy(m1.DEFAULT_POLICY)
    document["routing"] = {"policy": {"name": "default-routing", "version": 1}}
    document["external_review"]["round_counting"] = "completed_cycles"
    # 23: the provider's summary comment arrives before any verdict and is edited in
    # place, so a comment is not a round-completing signal (C4's correction round).
    document["external_review"]["accepted_signals"] = ["review", "reaction:+1"]
    return document


def seeded_policy_v2() -> dict[str, Any]:
    """default-software version 2 as migration 0009 seeds it (C5b): version 1 naming
    default-routing version 2, the verified roster."""
    document = seeded_policy()
    document["version"] = 2
    document["description"] = m9.POLICY_V2_DESCRIPTION
    document["routing"] = {"policy": {"name": "default-routing", "version": 2}}
    return document


def seeded_policy_v3() -> dict[str, Any]:
    document = seeded_policy_v2()
    document["version"] = 3
    document["description"] = m11.POLICY_V3_DESCRIPTION
    document["routing"] = {"policy": {"name": "default-routing", "version": 3}}
    return document


def test_the_seeded_policy_validates() -> None:
    policy = parse_policy(seeded_policy())
    assert policy.name == "default-software" and policy.version == 1
    assert policy.external_review.request_on_publish is True
    assert policy.external_review.trigger_comment == "@codex review"
    assert policy.operator_only_settings() == []


def test_t_auth_5_codex_parallel_policy_requires_renewer_mode() -> None:
    document = seeded_policy()
    document["concurrency"]["per_harness"]["codex"] = 2
    assert (
        validate_policy(document, name="default-software", version=1).concurrency.per_harness[
            "codex"
        ]
        == 2
    )
    with pytest.raises(ContractValidationError, match="policy failed validation"):
        validate_policy(
            document,
            name="default-software",
            version=1,
            concurrency_modes={"codex": "rw-narrow"},
        )


def test_the_seeded_policy_v2_validates() -> None:
    policy = parse_policy(seeded_policy_v2())
    assert policy.version == 2 and policy.routing.policy.version == 2


def _without_advisory(document: dict[str, Any]) -> dict[str, Any]:
    """ADR 0024: the examples list `gates.advisory` explicitly; a seeded version has no
    such field and takes the same set as its default."""
    out = copy.deepcopy(document)
    advisory = out["gates"].pop("advisory")
    assert frozenset(advisory) == DEFAULT_ADVISORY_GATES
    return out


def test_the_example_policy_matches_the_seed() -> None:
    """The shipped example is the current seed, version 3, with the advisory gates the
    seed takes by default written out."""
    document = yaml.safe_load(EXAMPLE.read_text())
    parse_policy(document)
    assert json.loads(json.dumps(_without_advisory(document), sort_keys=True)) == json.loads(
        json.dumps(seeded_policy_v3(), sort_keys=True)
    )


SELF_HOSTING = EXAMPLE.parent / "hades-self-hosting.yaml"


def test_the_self_hosting_policy_is_default_software_with_the_worker_checks() -> None:
    """hades #184: the example differs from the seed only where its header says."""
    document = yaml.safe_load(SELF_HOSTING.read_text())
    policy = parse_policy(document)
    document = _without_advisory(document)
    # Version 2 since hades #475: a referenced version is immutable, and the gate is new.
    assert (policy.name, policy.version) == ("hades-self-hosting", 2)
    assert policy.repository.required_checks == ["make lint", "make test-unit", "make scan"]
    assert {"uv", "python3.12", "gitleaks"} <= set(policy.repository.required_programs)
    assert {"pypi.org", "files.pythonhosted.org"} <= set(policy.network.egress_allowlist)
    assert "branch CI" in policy.description and "2026-09-28" in policy.description
    assert policy.ci_certification.require_green_on_final_sha
    assert "image_checks_required" in document["gates"]["pre_pr"]
    # hades #558, #85: the worker's integration tier needs a database, so the example
    # declares the Postgres service; the seed declares none.
    assert [s.kind for s in policy.services] == ["postgres"]
    assert document["services"] == [{"kind": "postgres"}]
    seed = seeded_policy_v3()
    ignored = {
        "name",
        "version",
        "description",
        "repository",
        "network",
        "routing",
        "gates",
        "services",
    }
    assert {k: v for k, v in document.items() if k not in ignored} == {
        k: v for k, v in seed.items() if k not in ignored
    }


def test_required_programs_default_to_none_and_take_bare_names() -> None:
    assert parse_policy(seeded_policy()).repository.required_programs == []
    document = seeded_policy()
    document["repository"]["required_programs"] = ["uv", "gitleaks"]
    assert parse_policy(document).repository.required_programs == ["uv", "gitleaks"]
    for bad in ("", " uv", "uv run", "a\tb"):
        document["repository"]["required_programs"] = [bad]
        assert any("single program name" in e for e in _errors(document)), bad


def test_the_seeded_routing_policy_validates() -> None:
    routing = parse_routing_policy(m4.DEFAULT_ROUTING)
    assert routing.model("gpt-5-codex-mini", "codex") is not None
    assert routing.model("nope", "codex") is None


def _errors(document: dict[str, Any]) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        PolicyV1.model_validate(document)
    return [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.value.errors()]


def test_unknown_field_rejected() -> None:
    document = seeded_policy()
    document["surprise"] = 1
    assert any("surprise" in e for e in _errors(document))


def test_missing_field_rejected() -> None:
    document = seeded_policy()
    del document["cleanup"]
    assert any(e.startswith("cleanup") for e in _errors(document))


def test_gates_must_partition_the_gate_set() -> None:
    document = seeded_policy()
    document["gates"]["pre_pr"].remove("no_secrets")
    assert any("gates in no group" in e and "no_secrets" in e for e in _errors(document))


def test_a_gate_in_two_groups_is_rejected() -> None:
    document = seeded_policy()
    document["gates"]["skipped"].append("no_secrets")
    assert any("more than one group" in e for e in _errors(document))


def test_an_unknown_gate_is_rejected() -> None:
    document = seeded_policy()
    document["gates"]["skipped"].append("invented_gate")
    assert any("unknown gates" in e for e in _errors(document))


def test_a_gate_listed_in_the_wrong_phase_is_rejected() -> None:
    document = seeded_policy()
    document["gates"]["pre_pr"].append("ci_green_for_head")
    document["gates"]["post_pr"].remove("ci_green_for_head")
    assert any("another phase" in e for e in _errors(document))


def test_every_gate_of_11_and_23_is_in_the_seed() -> None:
    document = seeded_policy()
    listed = set().union(
        *(set(document["gates"][k]) for k in ("pre_pr", "publication", "post_pr", "skipped"))
    )
    assert listed == ALL_GATES


def test_retry_classes_must_be_exit_classes() -> None:
    document = seeded_policy()
    document["retry"]["eligible_classes"].append("not_a_class")
    assert any("retry.eligible_classes" in e for e in _errors(document))


def test_egress_allowlist_rejects_wildcards() -> None:
    document = seeded_policy()
    document["network"]["egress_allowlist"].append("*.example.com")
    assert any("bare hostname" in e for e in _errors(document))


def test_zero_external_rounds_must_skip_the_external_gates() -> None:
    document = seeded_policy()
    document["external_review"]["required_rounds"] = 0
    assert any("must be listed in gates.skipped" in e for e in _errors(document))
    document["gates"]["skipped"] = ["external_review_rounds", "feedback_dispositions_complete"]
    document["gates"]["post_pr"] = ["ci_green_for_head"]
    parse_policy(document)


def test_reviewer_logins_required_above_zero_rounds() -> None:
    document = seeded_policy()
    document["external_review"]["reviewer_logins"] = []
    assert any("reviewer_logins" in e for e in _errors(document))


def test_requested_provider_without_a_trigger_comment_is_refused() -> None:
    document = seeded_policy()
    document["external_review"]["provider"] = "other-reviewer"
    document["external_review"].pop("trigger_comment", None)
    errors = _errors(document)
    assert any("other-reviewer" in error and "trigger_comment" in error for error in errors)


def test_codex_provider_supplies_its_builtin_trigger_comment() -> None:
    document = seeded_policy()
    document["external_review"]["provider"] = "codex"
    document["external_review"].pop("trigger_comment", None)
    assert parse_policy(document).external_review.trigger_comment == "@codex review"


def test_operator_only_settings_are_reported() -> None:
    document = seeded_policy()
    document["ci_certification"]["allow_no_ci"] = True
    document["deliverables"]["allow_branch_only"] = True
    document["release"]["require_operator_approval"] = False
    assert parse_policy(document).operator_only_settings() == [
        "ci_certification.allow_no_ci",
        "deliverables.allow_branch_only",
        "release.require_operator_approval",
    ]


def test_timeout_bounds_must_be_ordered() -> None:
    document = seeded_policy()
    document["limits"]["timeout_seconds"] = {"min": 600, "max": 300, "default": 400}
    assert any("min <= default <= max" in e for e in _errors(document))


def test_resources_request_fractions_default_and_validate() -> None:
    """Issue 93: a policy that omits the new request fractions still validates (a
    seeded policy predates them), and a fraction outside (0, 1] is rejected."""
    policy = parse_policy(seeded_policy())
    assert policy.resources.cpu_request_fraction == 0.5
    assert policy.resources.memory_request_fraction == 1.0

    document = seeded_policy()
    document["resources"]["cpu_request_fraction"] = 0
    assert _errors(document)

    document = seeded_policy()
    document["resources"]["cpu_request_fraction"] = 1.5
    assert _errors(document)

    document = seeded_policy()
    document["resources"]["memory_request_fraction"] = 0.25
    assert parse_policy(document).resources.memory_request_fraction == 0.25


def routing_document() -> dict[str, Any]:
    routing: dict[str, Any] = copy.deepcopy(m4.DEFAULT_ROUTING)
    return routing


def _routing_errors(document: dict[str, Any]) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        RoutingPolicyV1.model_validate(document)
    return [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.value.errors()]


def test_local_model_must_carry_an_endpoint_url() -> None:
    document = routing_document()
    document["models"][5].pop("endpoint_url")
    assert any("endpoint_url" in e for e in _routing_errors(document))


@pytest.mark.parametrize(
    "endpoint_url",
    ["file:///v1", "http://example.invalid/not-v1", "http://user@example.invalid/v1"],
)
def test_local_model_endpoint_url_must_be_a_valid_http_base(endpoint_url: str) -> None:
    document = routing_document()
    document["models"][5]["endpoint_url"] = endpoint_url
    assert any("http(s) base URL ending in /v1" in e for e in _routing_errors(document))


def test_subscription_model_must_not_carry_an_endpoint_url() -> None:
    document = routing_document()
    document["models"][0]["endpoint_url"] = "http://example.invalid/v1"
    assert any("must not carry endpoint_url" in e for e in _routing_errors(document))


def test_model_pool_must_exist() -> None:
    document = routing_document()
    document["models"][0]["pool"] = "nowhere"
    assert any("pools the policy does not define" in e for e in _routing_errors(document))


def test_duplicate_model_id_rejected() -> None:
    document = routing_document()
    document["models"].append(copy.deepcopy(document["models"][0]))
    assert any("duplicate model id" in e for e in _routing_errors(document))


def test_tier_prefer_must_be_a_subset_of_allowed() -> None:
    document = routing_document()
    document["tiers"]["trivial"]["prefer"] = ["frontier"]
    assert any("capabilities the tier does not allow" in e for e in _routing_errors(document))


@pytest.mark.parametrize(("window", "seconds"), [("5h", 18000), ("24h", 86400), ("90m", 5400)])
def test_window_seconds(window: str, seconds: int) -> None:
    assert window_seconds(window) == seconds


def test_ci_required_checks_defaults_to_no_narrowing() -> None:
    document = seeded_policy()
    del document["ci_certification"]["required_checks"]
    assert parse_policy(document).ci_certification.required_checks == []


def test_a_service_digest_must_have_exactly_64_hexadecimal_digits() -> None:
    document = seeded_policy()
    document["services"] = [{"kind": "postgres", "image": "postgres@sha256:" + "z" * 64}]
    assert any("must be pinned by digest" in error for error in _errors(document))
