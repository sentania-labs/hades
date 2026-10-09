"""Shared fixture builders (plain functions, no pytest magic)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

REPOSITORY_URL = "https://github.com/example-org/example-service"


def contract_document(**overrides: Any) -> dict[str, Any]:
    """A valid TaskContractV1 for the fake provider. Overrides replace top-level keys."""
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "external_id": "EX-0001",
        "title": "Return 409 on duplicate import ID",
        "project": "example-service",
        "parent_external_id": None,
        "repository": {
            "name": "example-service",
            "base_ref": "main",
            "work_branch": "crucible/EX-0001",
        },
        "scope": {
            "allowed_paths": ["src/ledger/**", "tests/ledger/**"],
            "prohibited_paths": [".github/**"],
            "may_add_dependencies": False,
            "may_modify_ci": False,
        },
        "objective": "Importing a duplicate ID must fail with 409 and no partial write.",
        "context": [{"kind": "issue", "ref": f"{REPOSITORY_URL}/issues/17"}],
        "project_instructions": [{"kind": "file", "ref": "CONTRIBUTING.md"}],
        "acceptance_criteria": [
            {"id": "AC1", "text": "Duplicate ID import returns 409."},
            {"id": "AC2", "text": "Existing import tests pass."},
        ],
        "required_verification": [
            {"id": "V1", "command": "make lint", "expect_exit": 0},
            {"id": "V2", "command": "make test", "expect_exit": 0},
            {"id": "V3", "command": "make scan", "expect_exit": 0},
            {"id": "V4", "kind": "artifact", "path": "report/run-evidence.md"},
        ],
        "constraints": {
            "prohibited_actions": ["modify files outside allowed_paths"],
            "network": "policy",
        },
        "deliverables": [
            {
                "kind": "pull_request",
                "target": "main",
                "draft": False,
                "closes": [f"{REPOSITORY_URL}/issues/17"],
            }
        ],
        "reporting": {
            "report_schema": "CompletionClaimV1",
            "report_dir": "/crucible/report",
            "progress_events": True,
        },
        "escalation": {
            "conditions": ["a required verification command does not exist"],
            "action": "write report/blocked.md with the question and exit 75",
        },
        "policy": {"name": "default-software", "version": 2},
        "execution_request": {
            "tier": "standard",
            "effort": "high",
            "provider": "fake",
            "image": "crucible-worker:fake-succeed",
            "timeout_seconds": 3600,
            "rationale": "Mechanical change with clear tests.",
        },
        "lifecycle": {"max_attempts": 2, "retry_on": ["environment", "lost"], "cleanup": "policy"},
        "correction": None,
    }
    doc.update(overrides)
    return doc


class FakeClock:
    """A settable clock. Tests advance it to exercise timeouts and lease expiry."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


# ----- the bootstrap ledger handoff (15) -------------------------------------------
#
# Synthetic bundles in the exact shape `foundry-ledger export --format crucible` writes:
# the producer's nineteen task fields in its order, its six event fields, its local
# timestamps, its counts and its content hash. Every value here is invented; no real
# task text, no operator path, nothing secret-shaped.

BOOTSTRAP_TASK_FIELDS = (
    "id",
    "title",
    "parent",
    "project",
    "repository",
    "scope",
    "objective",
    "contract",
    "model",
    "harness",
    "execution",
    "state",
    "created",
    "updated",
    "refs",
    "last_report",
    "evidence",
    "blockers",
    "decisions_pending",
)


def bootstrap_content_sha256(tasks: list[Any], events: list[Any]) -> str:
    """The producer's hash, restated here with only json and hashlib so a test never
    proves the domain's implementation against itself."""
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    canonical = json.dumps(
        {"tasks": tasks, "events": events},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def bootstrap_task(external_id: str, state: str, **overrides: Any) -> dict[str, Any]:
    number = external_id.rsplit("-", 1)[-1]
    record: dict[str, Any] = {
        "id": external_id,
        "title": f"Synthetic task {number}",
        "parent": None,
        "project": "example",
        "repository": "example-service",
        "scope": "src/example only",
        "objective": f"An invented objective for synthetic task {number}.",
        "contract": {"acceptance": ["the synthetic criterion holds"], "constraints": "none"},
        "model": "example-model-1",
        "harness": "example-harness",
        "execution": f"example-harness session {number}",
        "state": state,
        "created": "2026-09-01 09:00 CDT",
        "updated": "2026-09-02 10:30 CDT",
        "refs": {"branch": f"crucible/{external_id}"},
        "last_report": None,
        "evidence": [],
        "blockers": [],
        "decisions_pending": [],
    }
    record.update(overrides)
    return record


def bootstrap_event(
    seq: int,
    task: str,
    event: str,
    *,
    ts: str = "2026-09-01 09:00 CDT",
    who: str = "operator",
    detail: str | None = "",
) -> dict[str, Any]:
    return {"seq": seq, "ts": ts, "task": task, "event": event, "who": who, "detail": detail}


def bootstrap_bundle(
    tasks: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    migrated: str | None = None,
    schema_version: str = "1.0",
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "source": {
            "tool": "foundry-ledger 0.1.0",
            "db_sha256": "0" * 64,
            "exported_at": "2026-09-03 08:00 CDT",
            "migrated": migrated,
        },
        "tasks": tasks,
        "events": events,
        "counts": {"tasks": len(tasks), "events": len(events)},
        "content_sha256": bootstrap_content_sha256(tasks, events),
    }


# One task per mapped source state (15 step 2), in the producer's lifecycle order, with
# a short event history each. `running` and `dispatched` are the two that become an
# unsupervised attempt.
SYNTHETIC_STATES = (
    ("SYN-0001", "proposed"),
    ("SYN-0002", "dispatched"),
    ("SYN-0003", "running"),
    ("SYN-0004", "reported"),
    ("SYN-0005", "accepted"),
    ("SYN-0006", "blocked"),
    ("SYN-0007", "abandoned"),
    ("SYN-0008", "done"),
)


def synthetic_bundle(**overrides: Any) -> dict[str, Any]:
    """Eight synthetic tasks covering every mapped state, fifteen events."""
    tasks = [bootstrap_task(external_id, state) for external_id, state in SYNTHETIC_STATES]
    events: list[dict[str, Any]] = []
    seq = 0
    for external_id, state in SYNTHETIC_STATES:
        seq += 1
        events.append(bootstrap_event(seq, external_id, "proposed", detail="opened"))
        if state != "proposed":
            seq += 1
            events.append(
                bootstrap_event(
                    seq,
                    external_id,
                    state,
                    ts="2026-09-02 10:30 CDT",
                    who="foundry",
                    detail=f"moved to {state}",
                )
            )
    bundle = bootstrap_bundle(tasks, events)
    bundle.update(overrides)
    return bundle


def promote_for_test(
    uow: Any,
    *,
    digest: str,
    reference: str,
    harnesses: dict[str, str],
    at: datetime,
    by: str = "tests",
    reason: str = "",
) -> None:
    """Make one image the default of every harness named (ADR 0018: one row each)."""
    from crucible.domain.entities import HarnessImage  # noqa: PLC0415

    for harness, version in harnesses.items():
        uow.harness_images.put(
            HarnessImage(
                harness=harness,
                digest=digest,
                reference=reference,
                version=version,
                updated_at=at,
                updated_by=by,
                reason=reason,
            )
        )


def migration_by_slug(slug: str) -> Any:
    """The alembic `Script` of the one revision whose id ends in `_<slug>`.

    A branch's new migration is numbered provisionally: Hades renumbers it past main's
    highest and points it at main's head when it merges main into the branch (hades
    #447, CONTRIBUTING). A test that imports the module by its number breaks at
    collection the moment that happens, so tests find the revision by its slug."""
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    from crucible.adapters.persistence import migrate  # noqa: PLC0415

    script = ScriptDirectory.from_config(migrate.alembic_config("postgresql://unused/unused"))
    matches = [r for r in script.walk_revisions() if r.revision.endswith(f"_{slug}")]
    if len(matches) != 1:
        raise AssertionError(f"expected one revision ending in _{slug}, found {matches}")
    return matches[0]
