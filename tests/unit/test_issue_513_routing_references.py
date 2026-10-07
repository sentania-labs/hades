"""Hades #513/#512/#514 routing references and operator audit behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from crucible.adapters.persistence.migrations.versions import _0051_routing_model_references as m51
from crucible.application.admin.context import require_reason
from crucible.application.errors import ContractValidationError
from crucible.application.policies import validate_routing_policy
from crucible.contracts.policy import RoutingPolicyV1

URL = "http://gateway.internal:4000/v1"


def _entry(harness: str, model: str = "coder") -> dict[str, object]:
    return {
        "model": model,
        "harness": harness,
        "endpoint": "local",
        "endpoint_url": URL,
        "capability": "mid",
        "cost": "none",
        "speed": "fast",
        "pool": "lab-local",
        "weight": 1,
        "enabled": True,
    }


def _document() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "name": "route",
        "version": 8,
        "tiers": {"standard": {"allowed_capability": ["mid"], "prefer": ["mid"]}},
        "models": [_entry("hermes"), _entry("qwen_code"), _entry("codex")],
        "pools": {
            "lab-local": {
                "window": "1h",
                "budget_units": "attempts",
                "soft_limit": 0,
                "max_concurrency": 4,
            }
        },
        "rotation": {
            "strategy": "weighted-least-recent",
            "quality_feedback": True,
            "quality_window": 20,
        },
    }


def test_pair_uniqueness_allows_three_harnesses_to_share_coder() -> None:
    routing = RoutingPolicyV1.model_validate(_document())
    assert [(entry.harness, entry.model) for entry in routing.models] == [
        ("hermes", "coder"),
        ("qwen_code", "coder"),
        ("codex", "coder"),
    ]


def test_duplicate_pair_is_refused() -> None:
    document = _document()
    models = cast(list[dict[str, object]], _document()["models"])
    document["models"] = [*models, deepcopy(_entry("qwen_code"))]
    with pytest.raises(ValueError, match=r"\(harness, model\)"):
        RoutingPolicyV1.model_validate(document)


def test_unknown_local_model_names_model_and_checked_listing() -> None:
    document = _document()
    document["models"] = [_entry("qwen_code", "unknown")]
    with pytest.raises(ContractValidationError) as refused:
        validate_routing_policy(
            document, name="route", version=8, local_model_listing=["coder", "reasoner"]
        )
    assert "unknown" in refused.value.errors[0]["message"]
    assert "coder" in refused.value.errors[0]["message"]
    assert "reasoner" in refused.value.errors[0]["message"]


def test_migration_rewrites_populated_qwen_route_without_changing_version() -> None:
    populated = _document()
    populated["version"] = 7
    populated["models"] = [{**_entry("qwen_code"), "id": "qwen-coder", "model_name": "coder"}]
    models = cast(list[dict[str, object]], populated["models"])
    models[0].pop("model")

    migrated = m51._current(populated)

    assert migrated["version"] == 7
    assert migrated["models"][0]["harness"] == "qwen_code"
    assert migrated["models"][0]["model"] == "coder"
    assert "id" not in migrated["models"][0]


def test_operator_reason_is_optional_and_typed_words_are_verbatim() -> None:
    assert require_reason(None, required=False) == ""
    assert require_reason("  keep my spacing  ", required=False) == "  keep my spacing  "
