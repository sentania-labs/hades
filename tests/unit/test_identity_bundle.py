"""The identity bundle (06): what a worker is told, and what it is never told."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from crucible.adapters.execution.identity import bundle_sha256, write_bundle
from crucible.contracts.completion_claim import CompletionClaimV1
from tests.fixtures import contract_document

POLICY = {
    "git": {
        "author_name": "crucible-worker",
        "author_email": "crucible-worker@users.noreply.github.com",
        "commit_trailer": "Crucible-Attempt",
        "work_branch_pattern": "crucible/*",
        "protected_branches": ["main"],
    },
    "limits": {"grace_seconds": 60, "stall_fail_seconds": 1800},
    "gates": {"pre_pr": ["report_present", "verification_ran"]},
}


def build(tmp_path: Path, **kw: object) -> tuple[str, str, Path]:
    directory = tmp_path / "identity"
    contract = contract_document()
    text, digest = write_bundle(
        directory,
        contract=contract,
        policy=POLICY,
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
        report_schema=CompletionClaimV1.model_json_schema(),
        **kw,  # type: ignore[arg-type]
    )
    return text, digest, directory


def test_the_bundle_has_the_files_06_names(tmp_path: Path) -> None:
    _, _, directory = build(tmp_path)
    names = {p.name for p in directory.iterdir()}
    assert {
        "IDENTITY.md",
        "contract.yaml",
        "contract.sha256",
        "policy.md",
        "report-schema.json",
        "history",
    } <= names


def test_the_contract_travels_verbatim_in_both_forms(tmp_path: Path) -> None:
    _, _, directory = build(tmp_path)
    as_yaml = yaml.safe_load((directory / "contract.yaml").read_text())
    as_json = json.loads((directory / "contract.json").read_text())
    assert as_yaml == as_json == contract_document()


def test_identity_md_states_the_boundaries_and_the_protocol(tmp_path: Path) -> None:
    text, _, _ = build(tmp_path)
    assert "do not redefine, widen or delegate it" in text
    assert "`src/ledger/**`" in text
    assert "/crucible/report/report.yaml" in text
    assert "/crucible/report/blocked.md" in text
    assert "never push" in text
    assert "crucible/EX-0001" in text
    # 06: the verification list, verbatim.
    assert "`make lint`" in text and "`make test`" in text


def test_the_bundle_carries_no_credential_and_no_token(tmp_path: Path) -> None:
    """06: credentials, the Crucible API token and GitHub tokens are never in it."""
    _, _, directory = build(tmp_path)
    body = "\n".join(p.read_text() for p in directory.rglob("*") if p.is_file()).lower()
    for forbidden in ("bearer ", "authorization:", "api_key", "password", "oauth"):
        assert forbidden not in body, forbidden


def test_the_hash_covers_every_file(tmp_path: Path) -> None:
    _, digest, directory = build(tmp_path)
    assert digest == bundle_sha256(directory)
    (directory / "history" / "note.md").write_text("changed", encoding="utf-8")
    assert bundle_sha256(directory) != digest


def test_history_entries_are_written_when_there_are_any(tmp_path: Path) -> None:
    _, _, directory = build(tmp_path, history=[("attempt-1.md", "the first attempt said no")])
    assert (directory / "history" / "attempt-1.md").read_text() == "the first attempt said no"


def test_the_bundle_is_read_only(tmp_path: Path) -> None:
    _, _, directory = build(tmp_path)
    for path in directory.rglob("*"):
        if path.is_file():
            assert path.stat().st_mode & 0o222 == 0, path


def test_the_worker_is_told_the_report_format_version(tmp_path: Path) -> None:
    """hades #181: HT-0001's worker, told only "matching CompletionClaimV1", wrote
    schema_version: CompletionClaimV1 and the report did not parse. The prompt and
    the schema it points at both name the value."""
    text, _, directory = build(tmp_path)
    assert '`schema_version: "1.0"`' in text
    schema = json.loads((directory / "report-schema.json").read_text())
    version = schema["properties"]["schema_version"]
    assert version["examples"] == ["1.0"]
    assert version["pattern"] == r"^1\.[0-9]+$"
    assert "1.0" in version["description"]


def test_identity_md_names_the_criteria_to_map_and_the_report_check(tmp_path: Path) -> None:
    """hades #187: HT-0002 keyed `acceptance_mapping` by the verification ids. The
    bundle names the criterion ids to map. hades #215: the worker checks its report with
    `crucible-report` before it stops."""
    text, _, _ = build(tmp_path)
    criteria = [str(c["id"]) for c in contract_document()["acceptance_criteria"]]
    section = text[text.index("## Report") : text.index("## If you are stuck")]
    assert "one entry per criterion id: " + ", ".join(f"`{c}`" for c in criteria) in section
    for criterion in criteria:
        assert f"- `{criterion}`: " in text
    for name in ("summary", "proposed_pull_request", "limitations", "follow_ups"):
        assert f"`{name}`" in section
    assert "run `crucible-report check /crucible/report/report.yaml`" in section


def test_identity_md_is_short_and_carries_no_retired_instructions(tmp_path: Path) -> None:
    """FDY-0140: the operator's direction of 2026-09-29. No exit codes (a model cannot
    set its harness's), no precedence list, no author line, no log capture (Crucible
    re-runs the checks), and about 300 words for a typical contract."""
    text, _, _ = build(tmp_path)
    for retired in (
        "exit 0",
        "Exit codes",
        "precedence",
        "crucible-worker <",
        "log file",
    ):
        assert retired not in text, retired
    # hades #429 added the one-sentence note that Docker, kind and kubectl are CI's;
    # hades #393 the reason line of blocked.md and the one-paragraph stop rule.
    assert len(text.split()) <= 450, len(text.split())


def test_identity_md_renders_values_as_words_not_python(tmp_path: Path) -> None:
    """FDY-0140: instructions, booleans and lists printed as Python reprs; prohibited
    actions were read from `scope` though the contract keeps them in `constraints`; the
    repository printed `(unset)`; escalation conditions and context never arrived."""
    contract = contract_document()
    contract["context"] = [{"kind": "issue", "ref": "https://example.invalid/issues/17"}]
    contract["project_instructions"] = [{"kind": "file", "ref": "CONTRIBUTING.md"}]
    contract["constraints"]["prohibited_actions"] = ["delegate to other agents"]
    contract["escalation"]["conditions"] = ["a required check does not exist"]
    contract["scope"]["may_add_dependencies"] = True
    text, _ = write_bundle(
        tmp_path / "identity",
        contract=contract,
        policy=POLICY,
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
        report_schema=CompletionClaimV1.model_json_schema(),
    )
    for repr_marker in ("{'", "['", "True", "False", "None", "(unset)"):
        assert repr_marker not in text, repr_marker
    assert "- issue: `https://example.invalid/issues/17`" in text
    assert "- file: `CONTRIBUTING.md`" in text
    assert "- Do not delegate to other agents" in text
    assert "- a required check does not exist" in text
    assert "Add dependencies: yes. Change CI: no." in text
    assert f"repository `{contract['repository']['name']}`" in text
    policy_md = (tmp_path / "identity" / "policy.md").read_text()
    assert "`main`" in policy_md and "['" not in policy_md


def test_a_correction_reaches_the_worker(tmp_path: Path) -> None:
    contract = contract_document()
    contract["correction"] = {
        "of_version": 1,
        "reason": "external_review",
        "addresses": [{"kind": "review_comment", "id": "C1"}],
        "instructions": "Rename the helper the reviewer flagged.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    text, _ = write_bundle(
        tmp_path / "identity",
        contract=contract,
        policy=POLICY,
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
        report_schema=CompletionClaimV1.model_json_schema(),
    )
    assert "## This is a correction" in text
    assert "Rename the helper the reviewer flagged." in text
    assert "- review_comment: `C1`" in text


def test_identity_says_do_not_substitute_a_missing_tool(tmp_path: Path) -> None:
    """hades #183 point 1: the worker is told not to substitute missing tools and to
    exit 75 with blocked.md."""
    text, _, _ = build(tmp_path)
    assert "do not write a substitute for it" in text
    assert "report/blocked.md naming the program" in text
    assert "exit 75" in text
    checks_section = text[text.index("## Checks") : text.index("## Report")]
    assert "do not write a substitute for it" in checks_section
