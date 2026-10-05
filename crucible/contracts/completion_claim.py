"""CompletionClaimV1 (11): the worker's report. A claim, never an acceptance.
C1 parses only; gates that consume it are C2.

hades #215: the worker writes judgement and Crucible derives facts. The judgement
fields (summary, acceptance_mapping, the proposed pull request's title and body,
limitations, risks, blockers, follow_ups) are the worker's to write and stay required.
The fact fields (task_external_id, changed_files, refs, checks, run_evidence) are
optional: Crucible fills them from its own evidence, and a value the worker did write is
only compared with Crucible's and any difference recorded as information. The form is
lenient where the meaning is plain: `acceptance_mapping` may be an object keyed by
criterion id as well as a list."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from crucible.contracts.common import StrictModel, check_major_version

# The fields Crucible derives from its own evidence, and the ones only the worker can
# write. images/worker/crucible-report.py mirrors both lists; a unit test holds them equal.
FACT_FIELDS: tuple[str, ...] = (
    "task_external_id",
    "changed_files",
    "refs",
    "checks",
    "run_evidence",
)
JUDGEMENT_FIELDS: tuple[str, ...] = (
    "summary",
    "self_review",
    "acceptance_mapping",
    "proposed_pull_request",
    "limitations",
    "risks",
    "blockers",
    "follow_ups",
)

_FILLED = "Optional: Crucible fills it from its own evidence and only notes a different value."


class ClaimRefs(StrictModel):
    branch: str = Field(min_length=1)
    # hades #187: Crucible takes the head from the collected branch; this is a note.
    head_sha: str = Field(
        min_length=1,
        description="`git rev-parse HEAD` after the final commit. Crucible reads the "
        "head from the collected branch itself; a different value is only noted.",
    )
    commits: int = Field(ge=0)


class ClaimCheck(StrictModel):
    id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    exit: int
    log: str = Field(min_length=1)


class AcceptanceMapping(StrictModel):
    id: str = Field(min_length=1)
    status: Literal["met", "not_met", "not_exercised", "partial"]
    evidence: str


class SelfReview(StrictModel):
    """The worker's internal review of its own completed change (hades #402)."""

    documentation: list[str] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceMapping]
    omissions: list[str]

    @field_validator("acceptance_criteria", mode="before")
    @classmethod
    def _mapping(cls, value: Any) -> Any:
        return normalise_mapping(value)

    @field_validator("documentation", "omissions")
    @classmethod
    def _nonblank_notes(cls, value: list[str]) -> list[str]:
        if any(not note.strip() for note in value):
            raise ValueError("self_review notes must not be blank")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _reviewed_criteria(cls, value: list[AcceptanceMapping]) -> list[AcceptanceMapping]:
        if len({entry.id for entry in value}) != len(value):
            raise ValueError("self_review must map each acceptance criterion exactly once")
        if any(not entry.evidence.strip() for entry in value):
            raise ValueError("self_review must give evidence for every acceptance criterion")
        return value


class ProposedPullRequest(StrictModel):
    title: str = Field(min_length=1)
    body: str
    # Crucible keeps only the contract's own closing references (23), so a worker that
    # leaves this out loses nothing.
    closes: list[str] = Field(default_factory=list)


class FindingDisposition(StrictModel):
    """A correcting worker's disposition of one Codex inline finding (hades #401)."""

    review_comment_id: str = Field(min_length=1)
    disposition: Literal["fixed", "declined"]
    commit: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _evidence_for_disposition(self) -> FindingDisposition:
        if self.disposition == "fixed" and not self.commit:
            raise ValueError("a fixed finding names the commit that fixed it")
        if self.disposition == "declined" and not self.reason:
            raise ValueError("a declined finding gives the reason")
        return self


def normalise_mapping(value: Any) -> Any:
    """`{AC1: {status: met, evidence: ...}}` as `[{id: AC1, status: met, ...}]`.

    A bare status (`{AC1: met}`) is read as that status with no evidence text. Anything
    else is returned unchanged, for the model to reject in its own words."""
    if not isinstance(value, dict):
        return value
    out: list[Any] = []
    for key, entry in value.items():
        if isinstance(entry, dict):
            out.append({"id": key, **{k: v for k, v in entry.items() if k != "id"}})
        elif isinstance(entry, str):
            out.append({"id": key, "status": entry, "evidence": ""})
        else:
            out.append(entry)
    return out


class CompletionClaimV1(StrictModel):
    # The format version, "1.0". A worker handed only the schema's name wrote
    # "CompletionClaimV1" here (HT-0001, hades #181), so the JSON schema in the identity
    # bundle names the value; the validator below still decides.
    schema_version: str = Field(
        description='The report format version, "1.0". Not the schema name.',
        examples=["1.0"],
        json_schema_extra={"pattern": r"^1\.[0-9]+$"},
    )
    task_external_id: str | None = Field(default=None, min_length=1, description=_FILLED)
    summary: str = Field(min_length=1)
    self_review: SelfReview
    changed_files: list[str] | None = Field(default=None, description=_FILLED)
    refs: ClaimRefs | None = Field(default=None, description=_FILLED)
    checks: list[ClaimCheck] | None = Field(default=None, description=_FILLED)
    acceptance_mapping: list[AcceptanceMapping] = Field(
        description="One entry per contract acceptance criterion, keyed by its id "
        "(AC1, AC2, ...), never by a verification id (V1, ...). A list of entries, or an "
        "object keyed by criterion id."
    )
    run_evidence: list[str] | None = Field(default=None, description=_FILLED)
    proposed_pull_request: ProposedPullRequest
    limitations: list[str]
    risks: list[str]
    blockers: list[str]
    follow_ups: list[str]
    finding_dispositions: list[FindingDisposition] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return check_major_version(value)

    @field_validator("acceptance_mapping", mode="before")
    @classmethod
    def _mapping(cls, value: Any) -> Any:
        return normalise_mapping(value)


def parse_claim(
    document: object, *, criteria: Sequence[str] | None = None
) -> tuple[CompletionClaimV1 | None, list[dict[str, Any]]]:
    """Parse a report document. Returns (claim, errors); errors is empty on success."""
    if not isinstance(document, dict):
        return None, [{"loc": [], "msg": "report is not a mapping"}]
    try:
        claim = CompletionClaimV1.model_validate(document)
        if criteria is not None:
            reviewed = {entry.id for entry in claim.self_review.acceptance_criteria}
            missing = sorted(set(criteria) - reviewed)
            extra = sorted(reviewed - set(criteria))
            if missing or extra:
                return None, [
                    {
                        "loc": ["self_review", "acceptance_criteria"],
                        "msg": (
                            "self_review must map every contract criterion; "
                            f"missing: {missing}; unknown: {extra}"
                        ),
                        "type": "criteria",
                    }
                ]
        return claim, []
    except ValidationError as exc:
        errors = [
            {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors(include_url=False, include_input=False)
        ]
        return None, errors


def load_report(raw: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """`report.yaml` as a mapping, or why it is not one (ADR 0024).

    The reason names the parser's problem and its line and column, never the text of the
    file: a report that does not parse is shown to the reviewer, and its content is
    whatever the worker wrote."""
    try:
        loaded = yaml.safe_load(raw)
    except yaml.MarkedYAMLError as exc:
        where = exc.problem_mark or exc.context_mark
        at = f" at line {where.line + 1}, column {where.column + 1}" if where else ""
        problem = exc.problem or exc.context or "a syntax error"
        return None, [{"loc": [], "msg": f"report.yaml is not YAML: {problem}{at}", "type": "yaml"}]
    except yaml.YAMLError:
        return None, [{"loc": [], "msg": "report.yaml is not YAML", "type": "yaml"}]
    if isinstance(loaded, dict):
        return loaded, []
    return None, [{"loc": [], "msg": "report is not a mapping", "type": "shape"}]


@dataclass(frozen=True, slots=True)
class ClaimFacts:
    """What Crucible itself knows of an attempt at collection (hades #215): the task, the
    collected diff and branch, its own re-run of each check, and the run evidence it
    copied out. `refs` is None when no branch was collected."""

    task_external_id: str
    changed_files: tuple[str, ...]
    refs: dict[str, Any] | None
    checks: tuple[dict[str, Any], ...]
    run_evidence: tuple[str, ...]

    def value(self, name: str) -> Any:
        if name == "task_external_id":
            return self.task_external_id
        if name == "refs":
            return dict(self.refs) if self.refs is not None else None
        if name == "checks":
            return [dict(c) for c in self.checks]
        return list(getattr(self, name))


@dataclass(frozen=True, slots=True)
class CompletedClaim:
    """The worker's document with Crucible's facts in place, and what that changed."""

    document: dict[str, Any]
    filled: tuple[str, ...] = ()
    differences: tuple[dict[str, str], ...] = field(default_factory=tuple)


_HEX = re.compile(r"[0-9a-f]{7,64}")


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


REPORT_PREFIX = "report/"
_REPORT_MOUNT_PREFIX = "/crucible/report/"


def _as_report_path(path: str) -> str:
    """A run-evidence path as Crucible names it, `report/<name>`. IDENTITY.md tells the
    worker paths resolve against the report directory, so `V1.log`,
    `report/V1.log` and `/crucible/report/V1.log` are the same file."""
    if path.startswith(_REPORT_MOUNT_PREFIX):
        path = path[len(_REPORT_MOUNT_PREFIX) :]
    return path if path.startswith(REPORT_PREFIX) else REPORT_PREFIX + path


def _paths_difference(
    worker: Any, crucible: list[str], what: str, *, report_paths: bool = False
) -> str | None:
    """Changed files are compared both ways. Run evidence only one way: Crucible collects
    every file in the report directory (logs, progress, transcript), and the worker lists
    the ones that are evidence, so only a listed file Crucible did not collect is news."""
    if not isinstance(worker, list) or not all(isinstance(p, str) for p in worker):
        return "not a list of paths"
    listed, actual = set(worker), set(crucible)
    if report_paths:
        listed = {_as_report_path(p) for p in listed}
    parts = []
    if listed - actual:
        parts.append(_count(len(listed - actual), "path", "paths") + f" listed that {what}")
    if not report_paths and actual - listed:
        parts.append(_count(len(actual - listed), "path", "paths") + " not listed")
    return "; ".join(parts) or None


def _refs_difference(worker: Any, crucible: dict[str, Any]) -> str | None:
    if not isinstance(worker, dict):
        return "not a mapping of branch, head_sha and commits"
    parts = []
    if worker.get("branch") != crucible.get("branch"):
        parts.append("branch is not the collected work branch")
    head = worker.get("head_sha")
    if head != crucible.get("head_sha"):
        # Worker-written: echoed only when it is a hash, never as free text.
        named = str(head)[:12] if isinstance(head, str) and _HEX.fullmatch(head) else None
        parts.append(
            f"head_sha {named} is not the collected head"
            if named
            else "head_sha is not the collected head"
        )
    commits = worker.get("commits")
    if commits != crucible.get("commits"):
        shown = commits if isinstance(commits, int) else "another value"
        parts.append(f"commits {shown}, collected {crucible.get('commits')}")
    return "; ".join(parts) or None


def _checks_difference(worker: Any, crucible: list[dict[str, Any]]) -> str | None:
    if not isinstance(worker, list) or not all(isinstance(c, dict) for c in worker):
        return "not a list of checks"
    ran = {str(c["id"]): c for c in crucible}
    reported = {str(c.get("id")): c for c in worker}
    parts = []
    for check_id in sorted(ran):
        if check_id not in reported:
            parts.append(f"{check_id} not reported")
            continue
        exit_code = reported[check_id].get("exit")
        if exit_code != ran[check_id]["exit"]:
            shown = exit_code if isinstance(exit_code, int) else "another value"
            parts.append(
                f"{check_id} reported exit {shown}, Crucible's re-run {ran[check_id]['exit']}"
            )
    unknown = [c for c in reported if c not in ran]
    if unknown:
        parts.append(_count(len(unknown), "check", "checks") + " Crucible did not re-run")
    return "; ".join(parts) or None


def _difference(name: str, worker: Any, crucible: Any) -> str | None:
    if name == "task_external_id":
        return None if worker == crucible else "names another task"
    if name == "changed_files":
        return _paths_difference(worker, crucible, "the collected diff does not change")
    if name == "run_evidence":
        return _paths_difference(worker, crucible, "Crucible did not collect", report_paths=True)
    if name == "refs":
        return _refs_difference(worker, crucible)
    return _checks_difference(worker, crucible)


def complete_claim(document: dict[str, Any], facts: ClaimFacts) -> CompletedClaim:
    """Put Crucible's facts into the worker's document (hades #215).

    Each fact field takes Crucible's value. One the worker left out is listed as
    filled; one the worker wrote is compared, and a difference is described in plain
    words that never repeat the worker's free text. A fact Crucible does not have (no
    collected branch, so no refs) leaves the worker's value, if any, to be validated as
    written. The mapping is normalised to its list form."""
    out = dict(document)
    filled: list[str] = []
    differences: list[dict[str, str]] = []
    for name in FACT_FIELDS:
        crucible = facts.value(name)
        if crucible is None:
            continue
        if name not in document or document[name] is None:
            filled.append(name)
        else:
            detail = _difference(name, document[name], crucible)
            if detail:
                differences.append({"field": name, "detail": detail})
        out[name] = crucible
    if "acceptance_mapping" in out:
        out["acceptance_mapping"] = normalise_mapping(out["acceptance_mapping"])
    return CompletedClaim(out, tuple(filled), tuple(differences))
