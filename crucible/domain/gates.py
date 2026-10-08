"""Gates (11): names, phases, and the pure evaluators for the pre-PR set.

A gate evaluator is a pure function of a GateInput built from EvidenceV1 rows and the
contract. It never reads a database, a file, or a clock. Only `verified` evidence from a
source other than the worker is admissible: worker-asserted facts are shown to Foundry
and never satisfy a gate (11).

Each pre-PR gate is blocking or advisory (ADR 0024). A blocking failure stops the task;
an advisory one is recorded and carried in front of the internal reviewer, who decides.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any

from crucible.domain import injected
from crucible.domain.exit_class import CLEAN_EXIT_CLASSES
from crucible.domain.injected import (
    injected_prefix as _injected_prefix,
)
from crucible.domain.injected import (
    instruction_name_error,
)
from crucible.domain.secrets import redact


class GateName(StrEnum):
    # pre-PR (11)
    REPORT_PRESENT = "report_present"
    EXIT_CLEAN = "exit_clean"
    COMMITS_PRESENT = "commits_present"
    SCOPE_CONTAINED = "scope_contained"
    NO_INJECTED_FILES = "no_injected_files"
    NO_SECRETS = "no_secrets"
    EDITOR_LEFTOVERS = "editor_leftovers"
    VERIFICATION_RAN = "verification_ran"
    RUN_EVIDENCE_PRESENT = "run_evidence_present"
    CRITERIA_MAPPED = "criteria_mapped"
    DEPENDENCIES_UNCHANGED = "dependencies_unchanged"
    CI_UNCHANGED = "ci_unchanged"
    WORKSPACE_CLEAN = "workspace_clean"
    INTERNAL_REVIEW_RECORDED = "internal_review_recorded"
    # pre-PR, evaluated whatever the policy lists (hades FDY-0135)
    COMMIT_POLICY = "commit_policy"
    # publication (23)
    BRANCH_PUSHED_AT_HEAD = "branch_pushed_at_head"
    PR_EXISTS_HEAD_MATCHES = "pr_exists_head_matches"
    # post-PR (23)
    EXTERNAL_REVIEW_ROUNDS = "external_review_rounds"
    FEEDBACK_DISPOSITIONS_COMPLETE = "feedback_dispositions_complete"
    CI_GREEN_FOR_HEAD = "ci_green_for_head"


class GateResult(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    ERROR = "error"


PRE_PR_GATES: frozenset[str] = frozenset(
    {
        GateName.REPORT_PRESENT,
        GateName.EXIT_CLEAN,
        GateName.COMMITS_PRESENT,
        GateName.SCOPE_CONTAINED,
        GateName.NO_INJECTED_FILES,
        GateName.NO_SECRETS,
        GateName.EDITOR_LEFTOVERS,
        GateName.VERIFICATION_RAN,
        GateName.RUN_EVIDENCE_PRESENT,
        GateName.CRITERIA_MAPPED,
        GateName.DEPENDENCIES_UNCHANGED,
        GateName.CI_UNCHANGED,
        GateName.WORKSPACE_CLEAN,
        GateName.INTERNAL_REVIEW_RECORDED,
    }
)
# These gates always run, even when a stored policy omits them. Commit authorship is
# advisory (FDY-0143); the complete report and self-review block publication (#402).
ENFORCED_PRE_PR_GATES: frozenset[str] = frozenset({GateName.COMMIT_POLICY, GateName.REPORT_PRESENT})

# ADR 0024, the operator on 2026-09-29: "We need to let the review be our enforcement
# rather then dictating behavior". Hard gates stay where the damage is real or a claim is
# false; these three are information for the reviewer unless a policy says otherwise.
DEFAULT_ADVISORY_GATES: frozenset[str] = frozenset(
    {
        GateName.SCOPE_CONTAINED,
        GateName.CRITERIA_MAPPED,
        GateName.RUN_EVIDENCE_PRESENT,
    }
)
# The complete report, including self-review, is required for acceptance. A secret, once pushed,
# cannot be taken back, so no policy may send one to the reviewer instead of stopping.
ALWAYS_BLOCKING_GATES: frozenset[str] = frozenset({GateName.REPORT_PRESENT, GateName.NO_SECRETS})
# FDY-0143: the commit author is for the reviewer and the trailer is not checked at all,
# so no policy can make commit_policy stop a task.
ALWAYS_ADVISORY_GATES: frozenset[str] = frozenset({GateName.COMMIT_POLICY})


class GateClass(StrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


PUBLICATION_GATES: frozenset[str] = frozenset(
    {GateName.BRANCH_PUSHED_AT_HEAD, GateName.PR_EXISTS_HEAD_MATCHES}
)
POST_PR_GATES: frozenset[str] = frozenset(
    {
        GateName.EXTERNAL_REVIEW_ROUNDS,
        GateName.FEEDBACK_DISPOSITIONS_COMPLETE,
        GateName.CI_GREEN_FOR_HEAD,
    }
)
ALL_GATES: frozenset[str] = PRE_PR_GATES | PUBLICATION_GATES | POST_PR_GATES

# C3 shipped the verifier container, so no pre-PR gate is deferred any more. The marker
# stays so a reader of an older attempt's rows knows what `deferred` meant (11).
DEFERRED_TO_C3: frozenset[str] = frozenset()
DEFERRED_MARKER = "deferred:c3-verifier"
# A gate whose evidence the C2 collector cannot produce says so rather than claiming
# coverage it does not have. The fake provider does produce a diff, so this marker is
# what a future collector that cannot would trip.
COLLECTOR_MARKER = "incomplete:collector"

WORKER_SOURCE = "worker"
_HEX = re.compile(r"[0-9a-f]{7,64}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}")

# Shims and identity paths a worker must never leave behind (11).
# Where the identity bundle is mounted; the shim text names it (ports.execution).
SHIM_IDENTITY_MOUNT = "/crucible/identity"
INJECTED_NAMES = injected.INJECTED_NAMES
INJECTED_PREFIXES = injected.INJECTED_PREFIXES
CI_PATH_PREFIXES: tuple[str, ...] = (".github/workflows/", ".github/actions/", ".gitlab-ci")
DEPENDENCY_FILES: frozenset[str] = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
        "go.mod",
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "Gemfile.lock",
        "composer.lock",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One evidence row as a gate sees it. `id` is the database row id."""

    id: int
    kind: str
    source: str
    verified: bool
    payload: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None

    @property
    def admissible(self) -> bool:
        """Gates may consume only verified evidence, and never a worker assertion (11)."""
        return self.verified and self.source != WORKER_SOURCE


@dataclass(frozen=True, slots=True)
class GateInput:
    """Everything a pre-PR gate may look at. Pure data."""

    contract: dict[str, Any]
    policy: dict[str, Any]
    head_sha: str | None
    evidence: tuple[EvidenceItem, ...]
    internal_review_required: bool = True

    def of_kind(self, kind: str, *, role: str | None = None) -> list[EvidenceItem]:
        out = [e for e in self.evidence if e.admissible and e.kind == kind]
        if role is not None:
            out = [e for e in out if e.payload.get("role") == role]
        return out

    def one(self, kind: str, *, role: str | None = None) -> EvidenceItem | None:
        items = self.of_kind(kind, role=role)
        return items[-1] if items else None


@dataclass(frozen=True, slots=True)
class GateOutcome:
    result: GateResult
    detail: str
    evidence_ids: tuple[int, ...] = ()
    # A failure that stops the task even when the gate is advisory: a prohibited path
    # under scope_contained (ADR 0024).
    always_blocks: bool = False
    # Advisory findings for the reviewer that do not change the result, such as a
    # worker's report contradicting Crucible's own re-run.
    findings: tuple[str, ...] = ()


def _missing(kind: str, *, role: str | None = None) -> GateOutcome:
    """Gates run once the attempt is collected, so absent evidence is a failure, not a
    wait. 09: a failed attempt still reaches `reported`, and its gates then fail."""
    what = f"{kind} ({role})" if role else kind
    return GateOutcome(GateResult.FAIL, f"no verified {what} evidence was collected")


@lru_cache(maxsize=1024)
def _glob_re(pattern: str) -> re.Pattern[str]:
    """A path glob where `*` stops at a separator and `**` crosses one.

    `fnmatch` alone lets `src/*` match `src/a/b/c.py`, which would widen every
    allowed_paths entry silently."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                out.append("(?:.*/)?")
                index += 3
                continue
            if pattern.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            close = pattern.find("]", index + 1)
            if close != -1:
                out.append(pattern[index : close + 1])
                index = close + 1
                continue
        out.append(re.escape(char))
        index += 1
    return re.compile("".join(out) + r"\Z")


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    normalized = posixpath.normpath(path)
    for pattern in patterns:
        normalized_pattern = pattern.rstrip("/")
        if _glob_re(normalized_pattern).match(normalized):
            return True
        # `src/**` names the directory's contents, so `src/a/b.py` is inside it.
        if normalized_pattern.endswith("/**") and normalized.startswith(normalized_pattern[:-2]):
            return True
    return False


# ----- evaluators -------------------------------------------------------------


def _claim_notes(payload: dict[str, Any]) -> str:
    """hades #215: which fact fields Crucible filled, and where the worker's own value
    differed from Crucible's evidence. Information only; never a failure. The text was
    written by Crucible and repeats nothing the worker wrote but a hash or a number."""
    notes = ""
    filled = [str(f) for f in payload.get("filled_by_crucible") or []]
    if filled:
        notes += f"; Crucible filled {', '.join(filled)} from its own evidence"
    differences = [
        f"{d.get('field')} ({d.get('detail')})"
        for d in payload.get("differences") or []
        if isinstance(d, dict)
    ]
    if differences:
        notes += "; the report differs from Crucible's evidence: " + "; ".join(differences)
    return notes


def _parse_problems(errors: list[dict[str, Any]], shown: int = 3) -> str:
    """The first few parse problems, each as its field and message. The messages come
    from the parser's position and problem, not the report's text; they are redacted and
    cut short all the same."""
    if not errors:
        return ""
    parts = []
    for error in errors[:shown]:
        loc = ".".join(str(p) for p in error.get("loc") or [])
        msg = redact(str(error.get("msg") or ""))[:200]
        parts.append(f"{loc}: {msg}" if loc else msg)
    more = f"; and {len(errors) - shown} more" if len(errors) > shown else ""
    return ": " + "; ".join(parts) + more


def report_present(gi: GateInput) -> GateOutcome:
    """A complete report including the worker self-review is required to publish."""
    item = gi.one("artifact_present", role="completion_claim")
    if item is None:
        missing = _missing("artifact_present", role="completion_claim")
        return GateOutcome(missing.result, missing.detail, always_blocks=True)
    notes = _claim_notes(item.payload)
    if not item.payload.get("parsed_ok"):
        errors = [e for e in item.payload.get("parse_errors") or [] if isinstance(e, dict)]
        # Keep the missing review visible even when other schema problems fill the
        # bounded detail. The section name is the worker's actionable failure reason.
        errors.sort(key=lambda error: list(error.get("loc") or [])[:1] != ["self_review"])
        return GateOutcome(
            GateResult.FAIL,
            f"the report did not parse as CompletionClaimV1 ({len(errors)} problems)"
            + _parse_problems(errors)
            + notes,
            (item.id,),
            always_blocks=True,
        )
    if item.payload.get("self_review_checked") is not True:
        return GateOutcome(
            GateResult.FAIL,
            "the report has no validated self_review section; write self_review and rerun",
            (item.id,),
            always_blocks=True,
        )
    return GateOutcome(GateResult.PASS, "CompletionClaimV1 parsed" + notes, (item.id,))


def exit_clean(gi: GateInput) -> GateOutcome:
    item = gi.one("exit_info")
    if item is None:
        return _missing("exit_info")
    code = item.payload.get("exit_code")
    # Issue 128: an `incomplete` attempt exits 0 too, so the class must also be clean.
    if code == 0 and item.payload.get("exit_class") in CLEAN_EXIT_CLASSES:
        return GateOutcome(GateResult.PASS, "the worker exited 0 and completed", (item.id,))
    return GateOutcome(
        GateResult.FAIL,
        f"exit code {code!r}, class {item.payload.get('exit_class')!r}",
        (item.id,),
    )


def commits_present(gi: GateInput) -> GateOutcome:
    bundle = gi.one("bundle_head")
    if bundle is None:
        return _missing("bundle_head")
    ids = (bundle.id,)
    commits = int(bundle.payload.get("commits") or 0)
    if commits < 1:
        return GateOutcome(
            GateResult.FAIL, "the collected work_branch has no commit beyond base_ref", ids
        )
    if not bundle.payload.get("bundle_verified"):
        return GateOutcome(GateResult.FAIL, "git bundle verify failed on the collected branch", ids)
    collected = str(bundle.payload.get("head_sha") or "")
    if not collected:
        return GateOutcome(GateResult.FAIL, "the collected bundle names no head", ids)
    detail = f"{commits} commit(s), bundle verified at {collected}"
    # hades #187: the head is the collected bundle's, never a hash the worker copied. A
    # report whose `refs.head_sha` differs is noted here and does not fail the gate.
    claimed = str(bundle.payload.get("claimed_head_sha") or "")
    if not claimed:
        detail += "; the report named no head_sha"
    elif claimed != collected:
        # Worker-written: echoed only when it is a hash, never as free text.
        named = claimed[:12] if _HEX.fullmatch(claimed) else "another value"
        detail += f"; the report named {named}, which is not the collected head"
    return GateOutcome(GateResult.PASS, detail, ids)


def scope_contained(gi: GateInput) -> GateOutcome:
    """A path outside `allowed_paths` is for the reviewer when the gate is advisory; a
    path matching `prohibited_paths` always stops the task (ADR 0024)."""
    item = gi.one("diff_paths")
    if item is None:
        return _missing("diff_paths")
    scope = gi.contract.get("scope", {})
    allowed = [str(p) for p in scope.get("allowed_paths", [])]
    prohibited = [str(p) for p in scope.get("prohibited_paths", [])]
    bundle = gi.one("bundle_head")
    if bundle is None:
        return _missing("bundle_head")
    # The collector builds commit_paths from BASE..HEAD. That records the worker's
    # complete contribution (including paths later reverted) while excluding commits
    # reachable from the trusted prepared base, including a merge of that base.
    paths = list(dict.fromkeys(str(p) for p in bundle.payload.get("commit_paths", [])))
    outside = [p for p in paths if not _matches_any(p, allowed)]
    forbidden = [p for p in paths if _matches_any(p, prohibited)]
    if outside or forbidden:
        parts = []
        if forbidden:
            parts.append(f"matching prohibited_paths: {sorted(forbidden)[:10]}")
        if outside:
            parts.append(f"outside allowed_paths: {sorted(outside)[:10]}")
        return GateOutcome(
            GateResult.FAIL,
            "; ".join(parts),
            (item.id, bundle.id),
            always_blocks=bool(forbidden),
        )
    return GateOutcome(
        GateResult.PASS,
        f"all {len(paths)} worker commit path(s) inside allowed_paths",
        (item.id, bundle.id),
    )


def injected_name(path: str) -> bool:
    """Match normalized instruction names and harness directory entries (#400)."""
    return injected.injected_name(path)


_injected = injected_name


def injected_shim_text(identity_mount: str = SHIM_IDENTITY_MOUNT) -> str:
    """The line the preparer writes into every shim it creates (06), without the newline
    `printf '%s\\n'` adds."""
    return f"Read {identity_mount}/IDENTITY.md first; it is the task contract for this run."


@lru_cache(maxsize=1)
def _shim_blob_ids() -> frozenset[str]:
    """The git blob ids of the shim as the preparer writes it, for SHA-1 and SHA-256
    repositories, so a committed file is compared by id without its content."""
    body = (injected_shim_text() + "\n").encode("utf-8")
    header = f"blob {len(body)}\0".encode()
    return frozenset(
        {hashlib.sha1(header + body).hexdigest(), hashlib.sha256(header + body).hexdigest()}
    )


def _changes(raw: Any) -> list[tuple[str, str, str, str]] | None:
    """(path, status, blob, content classification) records, or None for evidence before
    hades #369, which carried no status."""
    if not isinstance(raw, list):
        return None
    out: list[tuple[str, str, str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(
                (
                    str(item.get("path", "")),
                    str(item.get("status", ""))[:1].upper(),
                    str(item.get("blob", "")).lower(),
                    str(item.get("classification", "")),
                )
            )
    return out


def _base_paths(raw: Any) -> frozenset[str]:
    """The injected-name paths the merge base has; none for evidence that did not record
    them, which leaves the decision to the commits' order alone."""
    return frozenset(str(p) for p in raw) if isinstance(raw, list) else frozenset()


def _injected_hits(
    paths: Sequence[str],
    diff_changes: list[tuple[str, str, str, str]] | None,
    commit_paths: Sequence[str],
    commit_changes: list[tuple[str, str, str, str]] | None,
    base_paths: frozenset[str] = frozenset(),
) -> set[str]:
    """hades #369: an injected-name path fails when the branch adds it relative to the
    base ref, or commits the shim's content into it. Deleting or editing a file the base
    already has is the repository's own work. A file turned into a symlink or back (T)
    counts as an add, since a link to the identity mount is a shim by another name. A
    path under an injected prefix always fails, and so does any path the evidence carries
    no status for, as before #369."""
    shim = _shim_blob_ids()
    hits: set[str] = set()
    for path in (*paths, *commit_paths):
        if error := instruction_name_error(path):
            hits.add(f"{path!a}: {error}")
    for path, _, _, classification in (diff_changes or []) + (commit_changes or []):
        if classification.startswith("error:"):
            hits.add(f"{path!r}: {classification}")
    diff_status: dict[str, str] = {}
    for path, status, blob, classification in diff_changes or []:
        diff_status[path] = status
        # Issue #377: check shim content on every added/modified path, not just injected names.
        if blob in shim and status in ("A", "T", "M"):
            hits.add(path)
        if _injected(path) and (
            status not in ("M", "D") or blob in shim or classification == "shim"
        ):
            hits.add(path)
    # Commits in `git log --diff-merges=separate --topo-order` order, every commit before
    # its parents and a merge once per parent: the last record of a path is the oldest,
    # and says whether the base had it, since only a path the base lacks starts with an
    # add. The order is the graph's, never the commit dates, which the worker sets.
    oldest_status: dict[str, str] = {}
    for path, status, blob, classification in commit_changes or []:
        oldest_status[path] = status
        # Issue #377: check shim content on every committed path, not just injected names.
        if blob in shim and status in ("A", "T", "M"):
            hits.add(path)
        if _injected(path) and (status == "T" or blob in shim or classification == "shim"):
            hits.add(path)
    for path, status in oldest_status.items():
        existed = diff_status.get(path) in ("M", "D") or status in ("M", "D") or path in base_paths
        if _injected(path) and not existed:
            hits.add(path)
    for path in paths:
        if _injected(path) and (
            _injected_prefix(path) or diff_changes is None or path not in diff_status
        ):
            hits.add(path)
    for path in commit_paths:
        if _injected(path) and (
            _injected_prefix(path) or commit_changes is None or path not in oldest_status
        ):
            hits.add(path)
    for path in (*diff_status, *oldest_status):
        if _injected_prefix(path):
            hits.add(path)
    return hits


def no_injected_files(gi: GateInput) -> GateOutcome:
    """Fail on instruction additions, harness paths and normalized shim content (#400).

    Names use casefold, NFC, removal of invisible format characters and common
    Cyrillic/Greek lookalikes. AGENTS*.md, CLAUDE*.md and GEMINI*.md match at any
    depth. Harness directory entries (including symlinks) and descendants always
    fail. The diff and every commit are checked. Existing repository instruction
    files may be edited or deleted (#369), unless their content normalizes to the
    shim after removing trailing whitespace and normalizing line endings/newlines.
    Unclassifiable names, instruction blobs or records fail closed with the collected
    reason. Empty lists and ordinary paths pass; the service classifies the shell
    collector's exported records without a Python dependency in worker images.

    Known limit: a base ancestor older than the merge base that once had an injected-name
    file excuses a history-only add of that path that is not the shim's content (a merge
    with that ancestor as its last parent, or a branch built from it), since the oldest
    record is then an edit or deletion. Normalized shim content still fails, and such
    a branch does not merge cleanly into the base."""
    diff = gi.one("diff_paths")
    bundle = gi.one("bundle_head")
    if diff is None:
        return _missing("diff_paths")
    if bundle is None:
        # 11 wants the diff and every commit on work_branch. Without the commit list the
        # gate has only half its evidence.
        return _missing("bundle_head")
    ids = tuple(e.id for e in (diff, bundle) if e is not None)
    over = [str(name) for name in diff.payload.get("over_limit", [])]
    if over:
        # hades #369: a list read only in part may hide a shim past its cut.
        return GateOutcome(
            GateResult.FAIL, f"path lists over their read limit, not fully read: {over}", ids
        )
    hits = sorted(
        _injected_hits(
            [str(p) for p in diff.payload.get("paths", [])],
            _changes(diff.payload.get("changes")),
            [str(p) for p in bundle.payload.get("commit_paths", [])],
            _changes(bundle.payload.get("commit_changes")),
            _base_paths(diff.payload.get("base_paths")),
        )
    )
    if hits:
        return GateOutcome(GateResult.FAIL, f"injected paths in the branch: {hits[:10]}", ids)
    return GateOutcome(
        GateResult.PASS, "no injected instruction, harness, or identity path in the branch", ids
    )


def no_secrets(gi: GateInput) -> GateOutcome:
    item = gi.one("scanner_result")
    if item is None:
        return _missing("scanner_result")
    findings = item.payload.get("findings") or []
    if findings:
        # Findings carry the location and the pattern name, never the matched value.
        where = [f"{f.get('where')}:{f.get('pattern')}:{f.get('excerpt', '')}" for f in findings][
            :10
        ]
        return GateOutcome(GateResult.FAIL, f"secret pattern matched at {where}", (item.id,))
    scanned = item.payload.get("scanned") or []
    unscanned = item.payload.get("unscanned") or []
    if unscanned:
        # hades #398: every changed file's content is scanned, or the gate waits.
        return GateOutcome(
            GateResult.PENDING,
            f"{COLLECTOR_MARKER}: the collector did not export the content of "
            f"{len(unscanned)} changed path(s) to scan: {unscanned[:10]}",
            (item.id,),
        )
    if not item.payload.get("diff_scanned"):
        # 11 wants the scanner over the diff itself. A collector that produced no diff
        # content leaves this gate waiting rather than claiming coverage it lacks.
        return GateOutcome(
            GateResult.PENDING,
            f"{COLLECTOR_MARKER}: the collector produced no diff content to scan",
            (item.id,),
        )
    if not scanned:
        return GateOutcome(
            GateResult.PENDING,
            f"{COLLECTOR_MARKER}: the scanner reported no inputs",
            (item.id,),
        )
    return GateOutcome(
        GateResult.PASS, f"scanner found nothing across {len(scanned)} input(s)", (item.id,)
    )


# Editor and merge leftover name patterns (issue 362).
EDITOR_LEFTOVERS_PATTERN: re.Pattern[str] = re.compile(
    r"(?:\.bak|\.orig|\.rej|\~)$|\.swp|(?:^|/)\.\#"
)


def _editor_leftover_path(path: str) -> bool:
    """Return True when *path* matches an editor or merge leftover name.

    Covers common patterns: *.bak, *.orig, *.rej, *~, .*.swp, .#* and friends.
    """
    return bool(EDITOR_LEFTOVERS_PATTERN.search(path))


def _diff_change_status(item: EvidenceItem) -> dict[str, str] | None:
    """Return a mapping of path -> status from diff_paths ``changes``, or None."""
    changes = item.payload.get("changes")
    if not changes:
        return None
    result: dict[str, str] = {}
    for c in changes:
        if isinstance(c, dict) and "path" in c and "status" in c:
            result[c["path"]] = c["status"]
    return result if result else None


def editor_leftovers(gi: GateInput) -> GateOutcome:
    """Fail when the diff adds editor or merge leftover files.

    Patterns: *.bak, *.orig, *.rej, *~, .*.swp, .#* and similar.
    Only newly added files (status ``A``) are checked. A leftover that
    exists on the base and is only edited or deleted does not fail.
    When change-status information is unavailable the gate conservatively
    falls back to all paths (the evidence did not include ``changes``).

    The failure always blocks so an advisory ``scope_contained`` gate
    still stops the task.
    """
    item = gi.one("diff_paths")
    if item is None:
        return _missing("diff_paths")
    paths = [str(p) for p in item.payload.get("paths", [])]

    # Determine which paths were newly added (status "A").
    change_status = _diff_change_status(item)
    if change_status is not None:
        added: set[str] = {p for p, s in change_status.items() if s == "A"}
        leftovers = [p for p in paths if _editor_leftover_path(p) and p in added]
    else:
        # No change-status information: fall back to all paths.
        leftovers = [p for p in paths if _editor_leftover_path(p)]

    if leftovers:
        return GateOutcome(
            GateResult.FAIL,
            f"editor or merge leftovers in the diff: {', '.join(sorted(leftovers))}",
            (item.id,),
            always_blocks=True,
        )
    return GateOutcome(
        GateResult.PASS,
        f"no editor or merge leftovers among {len(paths)} changed path(s)",
        (item.id,),
    )


def run_evidence_present(gi: GateInput) -> GateOutcome:
    harness_evidence = gi.one("artifact_present", role="harness_run_evidence")
    if harness_evidence is not None and not harness_evidence.payload.get("parsed_ok", False):
        return GateOutcome(
            GateResult.FAIL,
            f"harness run evidence is invalid: {harness_evidence.payload.get('error')}",
            (harness_evidence.id,),
        )
    required = [
        v
        for v in gi.contract.get("required_verification", [])
        if str(v.get("kind", "command")) == "artifact"
    ]
    if not required:
        return GateOutcome(GateResult.SKIPPED, "the contract requires no artifact verification")
    items = gi.of_kind("artifact_present", role="run_evidence")
    by_path = {posixpath.normpath(str(i.payload.get("path"))): i for i in items}
    ids: list[int] = []
    missing: list[str] = []
    empty: list[str] = []
    for verification in required:
        path = str(verification.get("path"))
        # The contract names a path; a file of the same name elsewhere is a different file.
        item = by_path.get(posixpath.normpath(path))
        if item is None:
            missing.append(path)
            continue
        ids.append(item.id)
        if int(item.payload.get("size") or 0) <= 0:
            empty.append(path)
    if missing:
        return GateOutcome(GateResult.FAIL, f"run evidence missing: {missing}", tuple(ids))
    if empty:
        return GateOutcome(GateResult.FAIL, f"run evidence is empty: {empty}", tuple(ids))
    return GateOutcome(
        GateResult.PASS,
        f"{len(required)} run-evidence artifact(s) present and non-empty",
        tuple(ids),
    )


def criteria_mapped(gi: GateInput) -> GateOutcome:
    item = gi.one("artifact_present", role="completion_claim")
    if item is None:
        return _missing("artifact_present", role="completion_claim")
    if not item.payload.get("parsed_ok"):
        return GateOutcome(
            GateResult.FAIL, "the report did not parse, so nothing is mapped", (item.id,)
        )
    mapped = {
        str(m.get("id")): str(m.get("status")) for m in item.payload.get("mapped_criteria", [])
    }
    required = [str(c.get("id")) for c in gi.contract.get("acceptance_criteria", [])]
    missing = [c for c in required if c not in mapped]
    if missing:
        return GateOutcome(
            GateResult.FAIL, f"acceptance criteria with no mapping: {missing}", (item.id,)
        )
    return GateOutcome(
        GateResult.PASS,
        "every acceptance criterion carries a status: "
        + ", ".join(f"{c}={mapped[c]}" for c in required),
        (item.id,),
    )


def dependencies_unchanged(gi: GateInput) -> GateOutcome:
    if gi.contract.get("scope", {}).get("may_add_dependencies"):
        return GateOutcome(GateResult.SKIPPED, "the contract permits dependency changes")
    item = gi.one("diff_paths")
    if item is None:
        return _missing("diff_paths")
    hits = sorted(
        {
            p
            for p in (str(x) for x in item.payload.get("paths", []))
            if posixpath.basename(posixpath.normpath(p)) in DEPENDENCY_FILES
        }
    )
    if hits:
        return GateOutcome(GateResult.FAIL, f"manifest or lockfile changed: {hits}", (item.id,))
    return GateOutcome(GateResult.PASS, "no manifest or lockfile in the diff", (item.id,))


def ci_unchanged(gi: GateInput) -> GateOutcome:
    if gi.contract.get("scope", {}).get("may_modify_ci"):
        return GateOutcome(GateResult.SKIPPED, "the contract permits CI changes")
    item = gi.one("diff_paths")
    if item is None:
        return _missing("diff_paths")
    hits = sorted(
        {
            p
            for p in (str(x) for x in item.payload.get("paths", []))
            if posixpath.normpath(p).startswith(CI_PATH_PREFIXES)
        }
    )
    if hits:
        return GateOutcome(GateResult.FAIL, f"CI definition changed: {hits}", (item.id,))
    return GateOutcome(GateResult.PASS, "no change under a workflow path", (item.id,))


def verification_ran(gi: GateInput) -> GateOutcome:
    """Crucible itself re-ran each required command from the collected tree (11).

    The worker's own check logs are a claim, stored and shown to Foundry; this gate
    reads only the verifier container's exits. When the worker claimed exit 0 but
    the re-run exited nonzero, the gate is blocking and names the false claim."""
    required = [
        v
        for v in gi.contract.get("required_verification", [])
        if str(v.get("kind", "command")) == "command"
    ]
    if not required:
        return GateOutcome(GateResult.SKIPPED, "the contract requires no command verification")
    runs = {str(i.payload.get("id")): i for i in gi.of_kind("verification_run")}
    ids: list[int] = []
    missing: list[str] = []
    failed: list[str] = []
    false_claims: list[str] = []
    for check in required:
        check_id = str(check.get("id"))
        item = runs.get(check_id)
        if item is None or not item.payload.get("ran"):
            # Why it did not run is what a reader needs: a verifier that timed out
            # reads very differently from one that was never asked.
            reason = str((item.payload.get("detail") if item else "") or "")
            missing.append(f"{check_id} ({reason})" if reason else check_id)
            if item is not None:
                ids.append(item.id)
            continue
        ids.append(item.id)
        expect = int(check.get("expect_exit", 0))
        actual = item.payload.get("exit_code")
        if actual != expect:
            failed.append(f"{check_id} exited {actual!r}, expected {expect}")
        # ADR 0024: the worker claimed this check passed while the re-run did not.
        false_claim = _find_false_claim(gi, check_id, item)
        if false_claim is not None:
            false_claims.append(false_claim)
    findings = contradicted_claims(gi, required, runs)
    if false_claims:
        # A false claim is blocking — the worker asserted a fact that proved false.
        return GateOutcome(
            GateResult.FAIL,
            "; ".join(false_claims),
            tuple(ids),
            findings=findings,
            always_blocks=True,
        )
    if missing:
        return GateOutcome(
            GateResult.FAIL,
            f"Crucible did not re-run: {sorted(missing)}",
            tuple(ids),
            findings=findings,
        )
    if failed:
        return GateOutcome(
            GateResult.FAIL, "; ".join(sorted(failed)), tuple(ids), findings=findings
        )
    return GateOutcome(
        GateResult.PASS,
        f"Crucible re-ran {len(required)} required command(s) in a verifier container",
        tuple(ids),
    )


def _find_false_claim(gi: GateInput, check_id: str, item: EvidenceItem) -> str | None:
    """ADR 0024: if the worker claimed exit 0 but the re-run exited nonzero, return
    the blocking message. Returns None when there is no false claim."""
    claim = gi.one("artifact_present", role="completion_claim")
    if claim is None:
        return None
    reported = {
        str(c.get("id")): c.get("exit")
        for c in claim.payload.get("claimed_checks") or []
        if isinstance(c, dict)
        and isinstance(c.get("exit"), int)
        and not isinstance(c.get("exit"), bool)
    }
    claimed_exit = reported.get(check_id)
    if claimed_exit == 0:
        rerun_exit = item.payload.get("exit_code")
        if rerun_exit not in (0, None):
            return f"the worker reported {check_id} exit 0; Crucible's re-run exited {rerun_exit}"
    return None


def contradicted_claims(
    gi: GateInput, required: list[dict[str, Any]], runs: dict[str, EvidenceItem]
) -> tuple[str, ...]:
    """ADR 0024: a check the worker reported passing that Crucible's own re-run failed is
    a trust problem, named plainly for the reviewer. Only the contract's own check ids
    are echoed, never text the worker wrote."""
    claim = gi.one("artifact_present", role="completion_claim")
    if claim is None:
        return ()
    reported = {
        str(c.get("id")): c.get("exit")
        for c in claim.payload.get("claimed_checks") or []
        if isinstance(c, dict)
        and isinstance(c.get("exit"), int)
        and not isinstance(c.get("exit"), bool)
    }
    out: list[str] = []
    for check in required:
        check_id = str(check.get("id"))
        item = runs.get(check_id)
        if item is None or not item.payload.get("ran") or check_id not in reported:
            continue
        expect = int(check.get("expect_exit", 0))
        if reported[check_id] == expect and item.payload.get("exit_code") != expect:
            out.append(f"the worker reported {check_id} passing; Crucible's re-run failed it")
    return tuple(sorted(out))


def workspace_clean(gi: GateInput) -> GateOutcome:
    """Nothing of this attempt is left running once the collector and verifier are
    removed (11). The provider's own container list is the evidence."""
    item = gi.one("workspace_state")
    if item is None:
        return _missing("workspace_state")
    if not item.payload.get("checked"):
        return GateOutcome(
            GateResult.FAIL,
            f"the provider could not check the workspace: {item.payload.get('detail')}",
            (item.id,),
        )
    leftover = [str(x) for x in item.payload.get("leftover", [])]
    if leftover:
        return GateOutcome(
            GateResult.FAIL, f"containers left for this attempt: {sorted(leftover)}", (item.id,)
        )
    return GateOutcome(
        GateResult.PASS, "no container or volume of this attempt is left behind", (item.id,)
    )


def internal_review_recorded(gi: GateInput) -> GateOutcome:
    # Retain the gate name for stored policies; report_present enforces the self-review.
    return GateOutcome(
        GateResult.SKIPPED,
        "the worker self-review is the internal review; report_present checks it",
    )


def _named_commit(sha: object) -> str:
    """A commit id from the collected branch, echoed only when it is a hash."""
    text = str(sha)
    return text[:12] if _HEX.fullmatch(text) else "a commit"


def commit_policy(gi: GateInput) -> GateOutcome:
    """Who authored the collected commits, for the reviewer (hades FDY-0135, FDY-0143).

    The collector checks every new commit's author against the policy's `author_email`.
    A commit by someone else fails this gate, which is always advisory, so the failure
    is listed for the reviewer and never stops the task. The attempt trailer is not
    checked: the operator decided on 2026-09-29 that the task record is the paper
    trail, and the publisher's guarantee is the sealed bundle at the reviewed and
    accepted head."""
    bundle = gi.one("bundle_head")
    if bundle is None:
        return _missing("bundle_head")
    ids = (bundle.id,)
    check = bundle.payload.get("commit_policy")
    if not isinstance(check, dict):
        return GateOutcome(
            GateResult.SKIPPED,
            "this attempt was collected before Crucible recorded commit authors",
            ids,
        )
    if not check.get("checked"):
        return GateOutcome(
            GateResult.FAIL,
            "Crucible could not read the collected commits' authors; check them in the diff",
            ids,
        )
    git = gi.policy.get("git", {})
    author = str(git.get("author_email") or "crucible-worker@users.noreply.github.com")
    authors = [a for a in check.get("author_problems") or [] if isinstance(a, dict)]
    if authors:
        named = ", ".join(
            f"{_named_commit(a.get('sha'))} by "
            + (
                str(a.get("author"))
                if _EMAIL.fullmatch(str(a.get("author")))
                else "another address"
            )
            for a in authors[:5]
        )
        return GateOutcome(
            GateResult.FAIL,
            f"{len(authors)} commit(s) not authored as {author} ({named})",
            ids,
        )
    return GateOutcome(GateResult.PASS, f"every commit is authored as {author}", ids)


PRE_PR_EVALUATORS: dict[str, Callable[[GateInput], GateOutcome]] = {
    GateName.REPORT_PRESENT: report_present,
    GateName.EXIT_CLEAN: exit_clean,
    GateName.COMMITS_PRESENT: commits_present,
    GateName.SCOPE_CONTAINED: scope_contained,
    GateName.NO_INJECTED_FILES: no_injected_files,
    GateName.NO_SECRETS: no_secrets,
    GateName.EDITOR_LEFTOVERS: editor_leftovers,
    GateName.VERIFICATION_RAN: verification_ran,
    GateName.RUN_EVIDENCE_PRESENT: run_evidence_present,
    GateName.CRITERIA_MAPPED: criteria_mapped,
    GateName.DEPENDENCIES_UNCHANGED: dependencies_unchanged,
    GateName.CI_UNCHANGED: ci_unchanged,
    GateName.WORKSPACE_CLEAN: workspace_clean,
    GateName.INTERNAL_REVIEW_RECORDED: internal_review_recorded,
    GateName.COMMIT_POLICY: commit_policy,
}


def evaluate_gate(gate: str, gi: GateInput) -> GateOutcome:
    """Run one pre-PR gate. An evaluator that raises is `error`, which counts as fail (09)."""
    evaluator = PRE_PR_EVALUATORS.get(gate)
    if evaluator is None:
        return GateOutcome(GateResult.ERROR, f"no evaluator for gate {gate!r}")
    try:
        return evaluator(gi)
    except Exception as exc:  # an evaluator that cannot run is `error`, treated as fail
        return GateOutcome(GateResult.ERROR, f"{type(exc).__name__}: {exc}")


def evaluate_pre_pr(gates: Sequence[str], gi: GateInput) -> dict[str, GateOutcome]:
    return {gate: evaluate_gate(gate, gi) for gate in gates}


def advisory_gates(policy: dict[str, Any]) -> frozenset[str]:
    """The pre-PR gates the policy marks advisory (ADR 0024). A policy version written
    before the field existed carries none, and takes the default."""
    listed = (policy.get("gates") or {}).get("advisory")
    if listed is None:
        return DEFAULT_ADVISORY_GATES | ALWAYS_ADVISORY_GATES
    return (frozenset(str(g) for g in listed) - ALWAYS_BLOCKING_GATES) | ALWAYS_ADVISORY_GATES


def gate_class(gate: str, advisory: frozenset[str]) -> GateClass:
    return GateClass.ADVISORY if gate in advisory else GateClass.BLOCKING


def _failed(outcome: GateOutcome) -> bool:
    return outcome.result in (GateResult.FAIL, GateResult.ERROR)


def stops_the_task(gate: str, outcome: GateOutcome, advisory: frozenset[str]) -> bool:
    """A fail or error (09 treats error as fail) on a blocking gate, or a failure that
    blocks whatever the gate's class."""
    return _failed(outcome) and (outcome.always_blocks or gate not in advisory)


def blocking(outcomes: dict[str, GateOutcome], advisory: frozenset[str] = frozenset()) -> list[str]:
    """Gates whose result stops the task."""
    return sorted(
        name for name, outcome in outcomes.items() if stops_the_task(name, outcome, advisory)
    )


def for_reviewer(
    outcomes: dict[str, GateOutcome], advisory: frozenset[str]
) -> list[dict[str, str]]:
    """Failed advisory gates and every advisory finding, each with its detail: what the
    internal reviewer is asked to weigh (ADR 0024)."""
    out = [
        {"gate": name, "detail": outcome.detail}
        for name, outcome in sorted(outcomes.items())
        if _failed(outcome) and not stops_the_task(name, outcome, advisory)
    ]
    out.extend(
        {"gate": name, "detail": finding}
        for name, outcome in sorted(outcomes.items())
        for finding in outcome.findings
    )
    return out


def waiting_for_review(outcomes: dict[str, GateOutcome]) -> bool:
    outcome = outcomes.get(GateName.INTERNAL_REVIEW_RECORDED)
    return outcome is not None and outcome.result is GateResult.PENDING


class PrePrVerdict(StrEnum):
    """Where the pre-PR gates send the task (09)."""

    FAILED = "pre_pr_gates_failed"
    REVIEW = "awaiting_internal_review"
    PASSED = "gates_passed"


def pre_pr_verdict(outcomes: dict[str, GateOutcome], advisory: frozenset[str]) -> PrePrVerdict:
    """A blocking failure stops the task; an advisory one never does (ADR 0024)."""
    if blocking(outcomes, advisory):
        return PrePrVerdict.FAILED
    if waiting_for_review(outcomes):
        return PrePrVerdict.REVIEW
    return PrePrVerdict.PASSED


# ----- publication and post-PR gates (23) --------------------------------


@dataclass(frozen=True, slots=True)
class DeliveryInput:
    """Everything a publication or post-PR gate may look at. Pure data.

    Separate from GateInput because these gates answer questions about the remote, not
    about the collected tree: the head Crucible pushed, the PR it opened, the review
    cycles it completed, and the certification it computed."""

    policy: dict[str, Any]
    accepted_head: str
    branch_pushed_sha: str | None = None
    pr_number: int | None = None
    pr_head_sha: str | None = None
    pr_state: str = ""
    completed_rounds: int = 0
    required_rounds: int = 1
    undispositioned: tuple[str, ...] = ()
    # Comments whose current disposition is `fix`. 09 advances out of
    # `external_feedback_received` only when every comment is dispositioned *and none is
    # fix*: a `fix` is Foundry saying the work is not done, and what follows it is a
    # correction, not advancement.
    fix_dispositions: tuple[str, ...] = ()
    comment_count: int = 0
    certification_state: str = ""
    certification_detail: str = ""
    # hades #476: the class `crucible.domain.certification.certify` recorded on the
    # computed certification, carried here so `ci_green_for_head` can record it too.
    # Never changes the gate's result.
    change_class: str = ""
    final_sha: tuple[bool, str] | None = None


def branch_pushed_at_head(di: DeliveryInput) -> GateOutcome:
    if not di.branch_pushed_sha:
        return GateOutcome(GateResult.PENDING, "the work branch has not been pushed yet")
    if di.branch_pushed_sha != di.accepted_head:
        return GateOutcome(
            GateResult.FAIL,
            f"the remote branch is at {di.branch_pushed_sha}, not the accepted head "
            f"{di.accepted_head}",
        )
    return GateOutcome(GateResult.PASS, f"the remote branch is at {di.accepted_head}")


def pr_exists_head_matches(di: DeliveryInput) -> GateOutcome:
    if di.pr_number is None:
        return GateOutcome(GateResult.PENDING, "no pull request has been opened for this task")
    if di.pr_head_sha != di.accepted_head:
        return GateOutcome(
            GateResult.FAIL,
            f"pull request #{di.pr_number} is at {di.pr_head_sha}, not the accepted head "
            f"{di.accepted_head}",
        )
    return GateOutcome(
        GateResult.PASS, f"pull request #{di.pr_number} is at the accepted head {di.accepted_head}"
    )


def external_review_rounds(di: DeliveryInput) -> GateOutcome:
    if di.required_rounds <= 0:
        return GateOutcome(GateResult.SKIPPED, "the policy requires no external review round (05b)")
    if di.final_sha is not None and not di.final_sha[0]:
        return GateOutcome(GateResult.PENDING, di.final_sha[1])
    if di.completed_rounds < di.required_rounds:
        return GateOutcome(
            GateResult.PENDING,
            f"{di.completed_rounds} of {di.required_rounds} review cycle(s) have completed",
        )
    return GateOutcome(
        GateResult.PASS,
        f"{di.completed_rounds} completed review cycle(s) satisfy the required "
        f"{di.required_rounds}",
    )


def feedback_dispositions_complete(di: DeliveryInput) -> GateOutcome:
    if di.fix_dispositions:
        return GateOutcome(
            GateResult.PENDING,
            f"{len(di.fix_dispositions)} review comment(s) are dispositioned `fix`; the "
            "task waits for the correction that addresses them (09)",
        )
    if not di.comment_count:
        return GateOutcome(GateResult.PASS, "no external review comment needs a disposition")
    if di.undispositioned:
        return GateOutcome(
            GateResult.PENDING,
            f"{len(di.undispositioned)} of {di.comment_count} review comment(s) have no "
            "recorded disposition",
        )
    return GateOutcome(
        GateResult.PASS,
        f"every one of {di.comment_count} review comment(s) is dispositioned and none is fix",
    )


def ci_green_for_head(di: DeliveryInput) -> GateOutcome:
    """A job the change classifier filtered out is not a missing job: the certification
    this reads already excludes a skipped run from its counted set (hades #476,
    `crucible.domain.certification._counts`), so this gate's PASS/FAIL/PENDING mapping
    never branches on `change_class`. It only names the class in the detail, so a
    reader of this gate's outcome sees which classification explains what ran."""
    mapping = {
        "green": GateResult.PASS,
        "failed": GateResult.FAIL,
        "skipped": GateResult.SKIPPED,
        "pending": GateResult.PENDING,
    }
    result = mapping.get(di.certification_state, GateResult.PENDING)
    detail = di.certification_detail or "no CI certification has been computed yet"
    if di.change_class:
        detail += f" (change class: {di.change_class})"
    return GateOutcome(result, detail)


DELIVERY_EVALUATORS: dict[str, Callable[[DeliveryInput], GateOutcome]] = {
    GateName.BRANCH_PUSHED_AT_HEAD: branch_pushed_at_head,
    GateName.PR_EXISTS_HEAD_MATCHES: pr_exists_head_matches,
    GateName.EXTERNAL_REVIEW_ROUNDS: external_review_rounds,
    GateName.FEEDBACK_DISPOSITIONS_COMPLETE: feedback_dispositions_complete,
    GateName.CI_GREEN_FOR_HEAD: ci_green_for_head,
}


def evaluate_delivery(gates: Sequence[str], di: DeliveryInput) -> dict[str, GateOutcome]:
    """Run the named publication or post-PR gates. An evaluator that raises is `error`."""
    out: dict[str, GateOutcome] = {}
    for gate in gates:
        evaluator = DELIVERY_EVALUATORS.get(gate)
        if evaluator is None:
            out[gate] = GateOutcome(GateResult.ERROR, f"no evaluator for gate {gate!r}")
            continue
        try:
            out[gate] = evaluator(di)
        except Exception as exc:
            out[gate] = GateOutcome(GateResult.ERROR, f"{type(exc).__name__}: {exc}")
    return out


def configured(policy: dict[str, Any], phase: str, default: frozenset[str]) -> list[str]:
    """The gates the policy names for a phase; with no policy section, the whole set."""
    gates = policy.get("gates", {}).get(phase)
    if gates is None:
        return sorted(default)
    return [str(g) for g in gates]
