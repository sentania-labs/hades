"""The commands Crucible's verifier container re-runs from the collected tree (11).

Every `required_verification` command, then every executable check an acceptance
criterion carries (hades #449). A criterion's check runs under the id
`acceptance:<criterion id>`, so it never collides with a required check's id, and its
exit is recorded as a `verification_run` row like any other re-run. The
`acceptance_checks` gate reads those rows.
"""

from __future__ import annotations

from typing import Any

ACCEPTANCE_CHECK_PREFIX = "acceptance:"


def acceptance_check_id(criterion_id: str) -> str:
    return f"{ACCEPTANCE_CHECK_PREFIX}{criterion_id}"


def criterion_checks(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Each acceptance criterion that carries an executable check, as a verifier check:
    `id` (the prefixed run id), `criterion`, `command` and `expect_exit`."""
    out: list[dict[str, Any]] = []
    for criterion in contract.get("acceptance_criteria") or []:
        if not isinstance(criterion, dict):
            continue
        check = criterion.get("check")
        if not isinstance(check, dict) or not str(check.get("command") or "").strip():
            continue
        criterion_id = str(criterion.get("id"))
        out.append(
            {
                "id": acceptance_check_id(criterion_id),
                "criterion": criterion_id,
                "command": str(check["command"]),
                "expect_exit": int(check.get("expect_exit", 0)),
            }
        )
    return out


def verifier_checks(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Every command check the verifier re-runs: the required command checks, then the
    criterion checks. Each has `id`, `command` and `expect_exit`."""
    required = [
        {
            "id": str(v.get("id")),
            "command": str(v.get("command")),
            "expect_exit": int(v.get("expect_exit", 0)),
        }
        for v in contract.get("required_verification") or []
        if isinstance(v, dict) and str(v.get("kind", "command")) == "command"
    ]
    return required + criterion_checks(contract)
