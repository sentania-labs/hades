"""Turning a collected attempt into artifacts and evidence rows (11).

Everything a gate later reads is written here, once, at `collected`. Crucible's own
observations are `verified`; the worker's own claim is recorded too, as `source: worker`
and `verified: false`, so Foundry can read it and no gate can ever consume it."""

from __future__ import annotations

import json
import logging
from typing import Any

from crucible.application.transitions import record_event
from crucible.contracts.completion_claim import ClaimFacts, CompletedClaim
from crucible.contracts.evidence import (
    REVIEW_DIFF_NAME,
    REVIEW_DIFF_TYPE,
    ROLE_COMPLETION_CLAIM,
    ROLE_REVIEW_DIFF,
    ROLE_RUN_EVIDENCE,
    ROLE_WORKER_CLAIM,
    EvidenceKind,
    EvidenceSource,
)
from crucible.domain.acceptance_checks import ACCEPTANCE_CHECK_PREFIX
from crucible.domain.entities import Artifact, Attempt, EvidenceRecord, Task
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.gates import injected_name
from crucible.domain.ids import new_id
from crucible.domain.secrets import find_secrets, match_text, redact
from crucible.ports.artifacts import ArtifactStore, SecretInArtifactError
from crucible.ports.clock import Clock
from crucible.ports.execution import (
    BranchBundle,
    CollectedOutputs,
    PathChange,
    VerificationRun,
)
from crucible.ports.harness import ParsedReport
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.evidence")


def _add(
    uow: UnitOfWork,
    clock: Clock,
    *,
    attempt: Attempt,
    kind: EvidenceKind,
    source: EvidenceSource,
    payload: dict[str, Any],
    artifact_id: str | None = None,
) -> EvidenceRecord:
    record = EvidenceRecord(
        id=None,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        kind=kind.value,
        observed_at=clock.now(),
        source=source.value,
        # 11: only Crucible's own and verified GitHub observations are verified.
        verified=source is not EvidenceSource.WORKER,
        payload=payload,
        artifact_id=artifact_id,
    )
    return uow.evidence.add(record)


def _changes_payload(changes: tuple[PathChange, ...]) -> list[dict[str, str]]:
    return [
        {
            "path": c.path,
            "status": c.status,
            "blob": c.blob,
            **({"classification": c.classification} if c.classification else {}),
        }
        for c in changes
    ]


def _commit_changes_payload(bundle: BranchBundle) -> dict[str, Any]:
    """hades #369: the commits' status records for injected-name paths, the only ones
    `no_injected_files` reads, so a long branch does not carry every path of every
    commit twice. Absent when the collector recorded none, which the gate judges as
    before #369."""
    if bundle.commit_changes is None:
        return {}
    kept = tuple(
        c
        for c in bundle.commit_changes
        if injected_name(c.path) or c.classification.startswith("error:")
    )
    return {"commit_changes": _changes_payload(kept)}


def _commit_policy_payload(bundle: BranchBundle) -> dict[str, Any]:
    """What the `commit_policy` gate reads: whether the collector finished its author
    check, and what it found (hades FDY-0135)."""
    check = bundle.commit_policy
    if check is None:
        return {"checked": False}
    return {
        "checked": True,
        "author_problems": [{"sha": sha, "author": who} for sha, who in check.author_problems],
    }


def store_artifact(
    uow: UnitOfWork,
    clock: Clock,
    store: ArtifactStore,
    *,
    attempt: Attempt,
    name: str,
    artifact_type: str,
    content: bytes,
    content_type: str,
    created_by: str = PRINCIPAL_CRUCIBLE,
) -> Artifact:
    """Scan, store content-addressed, and record the row. Raises on a secret match."""
    blob = store.put(content)
    existing = uow.artifacts.find_by_sha256(blob.sha256, attempt.id)
    if existing is not None and existing.type == artifact_type and existing.filename == name:
        return existing
    artifact = Artifact(
        id=new_id(),
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        type=artifact_type,
        filename=name,
        path=blob.path,
        size=blob.size,
        sha256=blob.sha256,
        content_type=content_type,
        created_at=clock.now(),
        created_by=created_by,
    )
    uow.artifacts.add(artifact)
    record_event(
        uow,
        clock,
        EventKind.ARTIFACT_STORED,
        principal=created_by,
        task_id=attempt.task_id,
        execution_id=attempt.execution_id,
        attempt_id=attempt.id,
        payload={
            "artifact_id": artifact.id,
            "type": artifact_type,
            "name": name,
            "sha256": blob.sha256,
            "size": blob.size,
        },
    )
    return artifact


def claim_facts(task: Task, outputs: CollectedOutputs) -> ClaimFacts:
    """Crucible's own values for the report's fact fields (hades #215): the task, the
    collected diff and branch, the verifier's re-runs, and the run evidence copied out."""
    bundle = outputs.bundle
    return ClaimFacts(
        task_external_id=task.external_id,
        changed_files=tuple(outputs.diff_paths),
        # No refs of Crucible's own without a collected branch that names itself and its
        # head; the worker's, if any, then stands as written.
        refs=(
            {"branch": bundle.work_branch, "head_sha": bundle.head_sha, "commits": bundle.commits}
            if bundle is not None and bundle.work_branch and bundle.head_sha
            else None
        ),
        checks=tuple(
            {
                "id": run.id,
                "command": run.command,
                "exit": run.exit_code,
                "log": f"verify/{run.id}.log",
            }
            for run in outputs.verifications
            # hades #449: a criterion check's run is the acceptance_checks gate's
            # evidence, not one of the report's required checks.
            if run.ran and not run.id.startswith(ACCEPTANCE_CHECK_PREFIX)
        ),
        run_evidence=tuple(a.name for a in outputs.artifacts if a.type == "run_evidence"),
    )


def _scanner_findings(
    outputs: CollectedOutputs, claim: dict[str, Any] | None
) -> list[dict[str, str]]:
    """The secret scanner over the diff, every commit message, and the report (11).

    A finding names where and which pattern, never the value."""
    findings: list[dict[str, str]] = []
    if claim is not None:
        findings.extend(
            {
                "where": f"report.{m.path}" if m.path else "report",
                "pattern": m.pattern,
                "excerpt": m.excerpt,
            }
            for m in find_secrets(claim)
        )
    elif outputs.report_raw:
        # A report that did not parse is still the worker's text, and its parse error
        # goes to the reviewer (ADR 0024).
        hit = match_text(outputs.report_raw, path="report")
        if hit:
            findings.append({"where": hit.path, "pattern": hit.pattern, "excerpt": hit.excerpt})
    if outputs.blocked_md:
        hit = match_text(outputs.blocked_md, path="report/blocked.md")
        if hit:
            findings.append({"where": hit.path, "pattern": hit.pattern, "excerpt": hit.excerpt})
    if outputs.diff_text is not None:
        # The content, not the path list: a credential committed into a file is what
        # this gate exists to catch (11).
        hit = match_text(outputs.diff_text, path="diff")
        if hit:
            findings.append({"where": hit.path, "pattern": hit.pattern, "excerpt": hit.excerpt})
    if outputs.diff_findings is not None:
        # hades #398: the adapter streamed the whole diff and every blob the worker
        # added or changed through the scanner; each match names its path.
        findings.extend(
            {"where": m.path, "pattern": m.pattern, "excerpt": m.excerpt}
            for m in outputs.diff_findings
        )
    for path in outputs.diff_paths:
        hit = match_text(path, path=f"diff-path:{path}")
        if hit:
            findings.append({"where": hit.path, "pattern": hit.pattern, "excerpt": hit.excerpt})
    if outputs.bundle is not None:
        for index, message in enumerate(outputs.bundle.commit_messages):
            hit = match_text(message, path=f"commit[{index}].message")
            if hit:
                findings.append({"where": hit.path, "pattern": hit.pattern, "excerpt": hit.excerpt})
    for artifact in outputs.artifacts:
        # The review diff is a presentation copy of the authoritative collected patch.
        # Scanning it again would judge deleted and context lines (#488).
        if artifact.name == REVIEW_DIFF_NAME:
            continue
        hit = match_text(
            artifact.content.decode("utf-8", "replace"), path=f"artifact:{artifact.name}"
        )
        if hit:
            findings.append({"where": hit.path, "pattern": hit.pattern, "excerpt": hit.excerpt})
    return findings


def _scrubbed(error: dict[str, Any]) -> dict[str, Any]:
    """A parse error as the reviewer sees it: the message redacted and cut short, in
    depth behind the scan above."""
    return {**error, "msg": redact(str(error.get("msg", "")))[:300]}


def _claimed_checks(claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Each check id and integer exit the worker's own report named, nothing else."""
    checks = claim.get("checks")
    if not isinstance(checks, list):
        return []
    return [
        {"id": str(c["id"]), "exit": c["exit"]}
        for c in checks
        if isinstance(c, dict)
        and isinstance(c.get("id"), str)
        and isinstance(c.get("exit"), int)
        and not isinstance(c.get("exit"), bool)
    ]


def _emit_false_claims(
    uow: UnitOfWork,
    clock: Clock,
    attempt: Attempt,
    claim: dict[str, Any] | None,
    verifications: tuple[VerificationRun, ...],
    task: Task,
) -> None:
    """ADR 0024: for each required check the worker reported exit 0 while the
    re-run exited nonzero, record a FALSE_CLAIM evidence row."""
    if claim is None:
        return
    reported: dict[str, int] = {}
    for c in claim.get("checks") or []:
        if (
            isinstance(c, dict)
            and isinstance(c.get("id"), str)
            and isinstance(c.get("exit"), int)
            and not isinstance(c.get("exit"), bool)
        ):
            reported[c["id"]] = c["exit"]
    rerun: dict[str, dict[str, Any]] = {}
    for run in verifications:
        if run.ran:
            rerun[run.id] = {"exit_code": run.exit_code, "command": run.command}
    for cid, info in rerun.items():
        if reported.get(cid) == 0 and info["exit_code"] not in (0, None):
            _add(
                uow,
                clock,
                attempt=attempt,
                kind=EvidenceKind.FALSE_CLAIM,
                source=EvidenceSource.CRUCIBLE,
                payload={
                    "check": cid,
                    "claimed_exit": 0,
                    "rerun_exit": info["exit_code"],
                    "command": info.get("command", ""),
                },
            )


def _diff_read(outputs: CollectedOutputs) -> bool:
    return outputs.diff_text is not None or outputs.diff_findings is not None


def _scanned_inputs(outputs: CollectedOutputs, claim: dict[str, Any] | None) -> list[str]:
    scanned = ["report" if claim is not None or outputs.report_raw else "report:absent"]
    if _diff_read(outputs):
        scanned.append("diff")
    scanned.extend(f"diff-path:{p}" for p in outputs.diff_paths)
    if outputs.bundle is not None:
        scanned.extend(f"commit[{i}].message" for i in range(len(outputs.bundle.commit_messages)))
    scanned.extend(f"artifact:{a.name}" for a in outputs.artifacts)
    return scanned


def record_collection_evidence(
    uow: UnitOfWork,
    clock: Clock,
    store: ArtifactStore,
    *,
    attempt: Attempt,
    task: Task,
    outputs: CollectedOutputs,
    claim: dict[str, Any] | None,
    claim_parsed_ok: bool,
    parse_errors: list[dict[str, Any]],
    parsed_report: ParsedReport | None = None,
    completed: CompletedClaim | None = None,
    unparsed_errors: list[dict[str, Any]] | None = None,
) -> str | None:
    """Write the artifacts and evidence a pre-PR gate consumes. Returns the collected head.

    `claim` is the document the worker wrote; `completed` is that document with
    Crucible's own facts in place (hades #215), which is what the stored report and the
    gates read. The worker's own values are kept as the worker's claim.
    `unparsed_errors` says a report file was there and was not a YAML mapping: it is
    recorded as a present report that did not parse, not as no report (ADR 0024)."""
    findings = _scanner_findings(outputs, claim)
    _add(
        uow,
        clock,
        attempt=attempt,
        kind=EvidenceKind.EXIT_INFO,
        source=EvidenceSource.CRUCIBLE,
        payload={
            "exit_code": attempt.exit_code,
            "exit_class": attempt.exit_class.value if attempt.exit_class else None,
            "termination_reason": attempt.termination_reason,
            "stall_shape": attempt.stall_shape,
            "termination_detail": attempt.termination_detail,
            "blocked_reason": attempt.blocked_reason,
        },
    )
    claim_artifact_id: str | None = None
    report = completed.document if completed is not None else claim
    if claim is not None and not findings:
        try:
            artifact = store_artifact(
                uow,
                clock,
                store,
                attempt=attempt,
                name="report/completion-claim.json",
                artifact_type="completion_claim",
                content=json.dumps(report, sort_keys=True, indent=2).encode("utf-8"),
                content_type="application/json",
            )
            claim_artifact_id = artifact.id
        except SecretInArtifactError as exc:
            findings.append({"where": "report/completion-claim.json", "pattern": exc.pattern})
    if claim is not None and report is not None:
        # The head and commit count the worker itself wrote, if any: commits_present
        # compares them with the collected branch (hades #187).
        refs = claim.get("refs", {}) if isinstance(claim.get("refs"), dict) else {}
        # A report the scanner matched is never copied into a row: 14 says no table ever
        # holds a secret, and the gate that reads this one has already failed.
        redacted = bool(findings)
        payload: dict[str, Any] = {
            "role": ROLE_COMPLETION_CLAIM,
            "parsed_ok": claim_parsed_ok,
            "self_review_checked": claim_parsed_ok and isinstance(report.get("self_review"), dict),
            "parse_errors": parse_errors,
            "redacted": redacted,
        }
        if not redacted:
            mapping = report.get("acceptance_mapping")
            if not isinstance(mapping, list):
                mapping = []
            payload.update(
                {
                    "claimed_head_sha": refs.get("head_sha"),
                    "claimed_commits": refs.get("commits"),
                    "mapped_criteria": [
                        {"id": m.get("id"), "status": m.get("status")}
                        for m in mapping
                        if isinstance(m, dict)
                    ],
                    "run_evidence": report.get("run_evidence") or [],
                    "changed_files": report.get("changed_files") or [],
                    "filled_by_crucible": list(completed.filled) if completed else [],
                    "differences": [dict(d) for d in completed.differences] if completed else [],
                    # The exits the worker itself reported, which verification_ran
                    # compares with Crucible's re-run (ADR 0024).
                    "claimed_checks": _claimed_checks(claim),
                }
            )
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.CRUCIBLE,
            payload=payload,
            artifact_id=claim_artifact_id,
        )
        # The worker's own claim, recorded unverified. A gate never reads this row (11).
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.WORKER,
            payload={"role": ROLE_WORKER_CLAIM, "asserted": {} if findings else claim},
        )
    elif unparsed_errors is not None:
        redacted = bool(findings)
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "role": ROLE_COMPLETION_CLAIM,
                "parsed_ok": False,
                "parse_errors": [] if redacted else [_scrubbed(e) for e in unparsed_errors],
                "redacted": redacted,
            },
        )
    if parsed_report is not None and parsed_report.run_evidence_error is not None:
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "role": "harness_run_evidence",
                "parsed_ok": False,
                "error": parsed_report.run_evidence_error,
            },
        )
    if parsed_report is not None and parsed_report.limit_reached is not None:
        # FDY-0140: recorded, not failed on. The gates and the review judge the work.
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.CRUCIBLE,
            payload={"role": "harness_limit_reached", "detail": parsed_report.limit_reached},
        )
    head_sha: str | None = None
    if outputs.bundle is not None:
        bundle = outputs.bundle
        head_sha = bundle.head_sha
        # The worker's own refs, when it wrote a mapping; `refs: null` or text is no head.
        claimed_refs = claim.get("refs") if claim else None
        claimed = claimed_refs.get("head_sha") if isinstance(claimed_refs, dict) else None
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.BUNDLE_HEAD,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "head_sha": bundle.head_sha,
                "base_ref": bundle.base_ref,
                "work_branch": bundle.work_branch,
                "commits": bundle.commits,
                "bundle_verified": bundle.verified,
                "bundle_sha256": bundle.sha256,
                "commit_paths": list(bundle.commit_paths),
                "commit_messages": list(bundle.commit_messages),
                "claimed_head_sha": claimed,
                "commit_policy": _commit_policy_payload(bundle),
                **_commit_changes_payload(bundle),
            },
        )
    if outputs.diff_paths or outputs.bundle is not None:
        diff_payload: dict[str, Any] = {"paths": list(outputs.diff_paths)}
        if outputs.diff_changes is not None:
            # Only the injected-name records the gate reads, as for the commits (#369).
            kept = tuple(
                c
                for c in outputs.diff_changes
                if injected_name(c.path) or c.classification.startswith("error:")
            )
            diff_payload["changes"] = _changes_payload(kept)
        if outputs.base_paths is not None:
            diff_payload["base_paths"] = [p for p in outputs.base_paths if injected_name(p)]
        if outputs.over_limit:
            diff_payload["over_limit"] = list(outputs.over_limit)
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.DIFF_PATHS,
            source=EvidenceSource.CRUCIBLE,
            payload=diff_payload,
        )
    for collected in outputs.artifacts:
        if collected.type == REVIEW_DIFF_TYPE and collected.name == REVIEW_DIFF_NAME:
            role = ROLE_REVIEW_DIFF
        elif collected.type == "run_evidence":
            role = ROLE_RUN_EVIDENCE
        else:
            continue
        artifact_id: str | None = None
        try:
            stored = store_artifact(
                uow,
                clock,
                store,
                attempt=attempt,
                name=collected.name,
                artifact_type=collected.type,
                content=collected.content,
                content_type=collected.content_type,
            )
            artifact_id = stored.id
        except SecretInArtifactError as exc:
            findings.append({"where": f"artifact:{collected.name}", "pattern": exc.pattern})
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "role": role,
                "path": collected.name,
                "size": len(collected.content),
            },
            artifact_id=artifact_id,
        )
    # 11: Crucible's own re-run of every required command, in a verifier container from
    # the collected tree. The worker's logs are a claim; these are the evidence.
    verification_logs = {a.name: a for a in outputs.artifacts if a.type == "verification_log"}
    for run in outputs.verifications:
        verify_artifact_id: str | None = None
        collected_log = verification_logs.get(f"verify/{run.id}.log")
        if collected_log is not None and collected_log.content:
            try:
                verify_artifact_id = store_artifact(
                    uow,
                    clock,
                    store,
                    attempt=attempt,
                    name=collected_log.name,
                    artifact_type=collected_log.type,
                    content=collected_log.content,
                    content_type=collected_log.content_type,
                ).id
            except SecretInArtifactError as exc:
                findings.append({"where": f"artifact:{collected_log.name}", "pattern": exc.pattern})
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.VERIFICATION_RUN,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "id": run.id,
                "command": run.command,
                "expect_exit": run.expect_exit,
                "exit_code": run.exit_code,
                "ran": run.ran,
                "detail": run.detail,
                "seconds": run.seconds,
            },
            artifact_id=verify_artifact_id,
        )
        record_event(
            uow,
            clock,
            EventKind.VERIFICATION_COMPLETED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload={
                "id": run.id,
                "command": run.command,
                "exit_code": run.exit_code,
                "expect_exit": run.expect_exit,
                "ran": run.ran,
            },
        )
    # ADR 0024: detect false claims — worker reported exit 0, re-run exited nonzero.
    _emit_false_claims(uow, clock, attempt, claim, outputs.verifications, task)
    if outputs.workspace_state is not None:
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.WORKSPACE_STATE,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "checked": outputs.workspace_state.checked,
                "leftover": list(outputs.workspace_state.leftover),
                "detail": outputs.workspace_state.detail,
            },
        )
    # 08: the collector records each file it refused to copy out.
    for rejection in outputs.copy_rejections:
        record_event(
            uow,
            clock,
            EventKind.COLLECTOR_REJECTED_FILE,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload={"reason": rejection.get("reason"), "path": rejection.get("path")},
        )
    _add(
        uow,
        clock,
        attempt=attempt,
        kind=EvidenceKind.SCANNER_RESULT,
        source=EvidenceSource.CRUCIBLE,
        payload={
            "findings": findings,
            "scanned": _scanned_inputs(outputs, claim),
            # The gate reports `pass` only when the diff itself was read (11). A
            # collector that produced no diff, or a changed file whose content was not
            # exported to scan (hades #398), leaves this false and the gate waits.
            "diff_scanned": _diff_read(outputs) and not outputs.diff_unscanned,
            "unscanned": list(outputs.diff_unscanned[:50]),
        },
    )
    record_event(
        uow,
        clock,
        EventKind.EVIDENCE_RECORDED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        execution_id=attempt.execution_id,
        attempt_id=attempt.id,
        payload={
            "head_sha": head_sha,
            "diff_paths": len(outputs.diff_paths),
            "artifacts": len(outputs.artifacts),
            "scanner_findings": len(findings),
        },
    )
    return head_sha
