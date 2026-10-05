"""hades #429: a worker has no Docker, kind or kubectl and never will.

CONTRIBUTING.md names which tiers a worker runs and which are CI's, the identity's
Checks section carries the same sentence, and the contract model refuses a required
check that needs one of the three programs, naming it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.adapters.execution.identity import (
    WORKER_ABSENT_PROGRAMS_SENTENCE,
    render_identity_md,
)
from crucible.contracts.task_contract import (
    WORKER_ABSENT_PROGRAMS,
    TaskContractV1,
    worker_absent_program,
)
from tests.fixtures import contract_document

REPO = Path(__file__).resolve().parents[2]


def _render(contract: dict[str, Any]) -> str:
    return render_identity_md(
        contract=contract,
        policy={},
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
    )


def _errors(doc: dict[str, Any]) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        TaskContractV1.model_validate(doc)
    return [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.value.errors()]


# AC1: the rendered identity carries the sentence, in its Checks section.


def test_identity_checks_section_says_docker_kind_and_kubectl_are_cis() -> None:
    text = _render(contract_document())
    checks = text[text.index("## Checks") : text.index("## Report")]
    assert f"- {WORKER_ABSENT_PROGRAMS_SENTENCE}" in checks
    assert "Docker, kind and kubectl are absent in a worker and are CI's" in checks
    assert "not a reason to stop" in checks


def test_the_sentence_names_every_absent_program() -> None:
    for program in WORKER_ABSENT_PROGRAMS:
        assert program.lower() in WORKER_ABSENT_PROGRAMS_SENTENCE.lower()


def test_contributing_shares_the_identity_sentence() -> None:
    """The same words in the project's instructions and in the identity, so a worker
    reading either is told once that the missing programs are expected."""
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert " ".join(contributing.split()).count(WORKER_ABSENT_PROGRAMS_SENTENCE) == 1


# AC2: a contract whose required check needs docker, kind or kubectl is refused, and
# the reason names the program.


@pytest.mark.parametrize(
    ("command", "program"),
    [
        ("make deploy-kind", "kind"),
        ("make e2e-kind", "kind"),
        ("make first-run-kind", "kind"),
        ("make lint && make e2e-kind-self-hosting", "kind"),
        ("docker compose up -d", "docker"),
        ("docker build -t x .", "docker"),
        ("/usr/local/bin/docker ps", "docker"),
        ("docker-compose up", "docker"),
        ("FOO=1 docker run x", "docker"),
        ("sudo kind create cluster", "kind"),
        ("kubectl apply -f deploy/", "kubectl"),
        ("make manifests; kubectl get pods", "kubectl"),
    ],
)
def test_a_required_check_that_needs_an_absent_program_is_refused(
    command: str, program: str
) -> None:
    doc = contract_document()
    doc["required_verification"].append({"id": "V9", "command": command, "expect_exit": 0})
    errs = _errors(doc)
    refusal = [e for e in errs if "required_verification" in e and "V9" in e]
    assert refusal, errs
    assert f"needs {program}" in refusal[0], refusal
    assert command in refusal[0]
    assert "CI's" in refusal[0]


@pytest.mark.parametrize(
    "command",
    [
        "make lint",
        "make test-unit",
        "make scan",
        "uv run pytest -q tests/unit/test_issue_429_worker_local_tiers.py",
        "uv run pytest -q tests/unit/test_e2e_kind_readiness.py",
        "uv run pytest -q tests/unit/test_docker_provider.py",
        "make e2e",
        "grep --kind=x README.md",
        "make KIND_DUMP_SECONDS=10 test-unit",
    ],
)
def test_worker_local_checks_are_accepted(command: str) -> None:
    doc = contract_document()
    doc["required_verification"].append({"id": "V9", "command": command, "expect_exit": 0})
    contract = TaskContractV1.model_validate(doc)
    assert command in contract.verification_commands
    assert worker_absent_program(command) is None


def test_an_unbalanced_quote_still_names_the_program() -> None:
    assert worker_absent_program("docker run 'x") == "docker"


def test_the_self_hosting_policy_checks_are_worker_local() -> None:
    """The contract this repository's tasks run under requires only what the worker
    image can run (ADR 0020)."""
    for command in ("make lint", "make test-unit", "make scan"):
        assert worker_absent_program(command) is None


# AC3: CONTRIBUTING.md names the worker-local tiers and the CI-only tiers.


def test_contributing_names_the_worker_local_and_ci_only_tiers() -> None:
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = contributing[contributing.index("## When the author is a worker") :]
    section = section[: section.index("## Kubernetes manifests")]
    assert "Worker-local tiers" in section
    for local in ("`make lint`", "`make test-unit`", "`make scan`", "tests/unit/test_issue_"):
        assert local in section, local
    assert "CI-only tiers" in section
    for ci in ("compose smoke", "`make e2e`", "`make e2e-kind`", "image builds and digests"):
        assert ci in section, ci
    assert "`make deploy-kind`" in section
    assert "does not write `blocked.md`" in section


def test_contributing_no_longer_sends_a_worker_to_deploy_kind() -> None:
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    manifests = contributing[contributing.index("## Kubernetes manifests") :]
    manifests = manifests[: manifests.index("## Releases")]
    assert "a worker leaves both to CI" in manifests
    assert "never builds the image" in manifests
