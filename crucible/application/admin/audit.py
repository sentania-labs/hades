"""The audit tail (25): admin events only, with a cursor."""

from __future__ import annotations

from typing import Any

from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork

ADMIN_KINDS: frozenset[str] = frozenset(
    {
        EventKind.AUTO_MERGE_UPDATED.value,
        EventKind.HARNESS_ENABLED.value,
        EventKind.HARNESS_DISABLED.value,
        EventKind.CREDENTIAL_VALIDATED.value,
        EventKind.CREDENTIAL_PROBED.value,
        EventKind.CREDENTIAL_SET.value,
        EventKind.CREDENTIAL_LOGIN_STARTED.value,
        EventKind.CREDENTIAL_LOGIN_CODE_SUBMITTED.value,
        EventKind.CREDENTIAL_LOGIN_CANCELLED.value,
        EventKind.CREDENTIAL_LOGIN_FINISHED.value,
        EventKind.CREDENTIAL_ROTATED.value,
        EventKind.CREDENTIAL_REMOVED.value,
        EventKind.CREDENTIAL_RETIRED_SHREDDED.value,
        EventKind.IMAGE_PROMOTED.value,
        EventKind.GITHUB_CHECKED.value,
        EventKind.ADMIN_REFUSED.value,
        EventKind.PRINCIPAL_CREATED.value,
        EventKind.PRINCIPAL_REVOKED.value,
        EventKind.PRINCIPAL_RENAMED.value,
        EventKind.REPOSITORY_REGISTERED.value,
        EventKind.REPOSITORY_REMOVED.value,
        EventKind.REPOSITORY_ATTESTATION_RECORDED.value,
        EventKind.POLICY_UPLOADED.value,
        EventKind.ROUTING_POLICY_UPLOADED.value,
        EventKind.BOOTSTRAP_IMPORT_VERIFIED.value,
        EventKind.BOOTSTRAP_IMPORT_COMMITTED.value,
        EventKind.BOOTSTRAP_IMPORT_DISCARDED.value,
        EventKind.POOL_EXHAUSTION_CLEARED.value,
        EventKind.LOCAL_ENDPOINT_UPDATED.value,
        EventKind.KUBERNETES_EGRESS_UPDATED.value,
        EventKind.KUBERNETES_TIMEOUTS_UPDATED.value,
        EventKind.LOCAL_GATEWAY_UPDATED.value,
        EventKind.GITHUB_APP_CONNECTED.value,
        EventKind.GITHUB_APP_MANIFEST_STARTED.value,
        EventKind.GITHUB_EXTERNAL_URL_UPDATED.value,
        EventKind.COMMAND_TIMEOUT_UPDATED.value,
        # hades #424: the operator's answers to a proposed task, each with its reason.
        EventKind.TASK_APPROVED.value,
        EventKind.TASK_SENT_BACK.value,
        EventKind.TASK_PROPOSAL_REJECTED.value,
    }
)


PAGE_SIZE = 200
MAX_PAGES = 20


def tail(uow: UnitOfWork, *, cursor: int | None, limit: int) -> dict[str, Any]:
    """Admin events after the cursor, oldest first.

    `next_cursor` is how far the scan reached, not the last matching item. The stream
    holds every kind of event, so a stretch of non-admin events longer than the scan
    budget yields an empty page; a cursor taken from the last item would then sit where
    it already was and every later admin event would be unreachable. The scan position
    always moves forward, so the next call resumes past the gap.
    """
    items: list[dict[str, Any]] = []
    after = cursor or 0
    for _ in range(MAX_PAGES):
        if len(items) >= limit:
            break
        batch = uow.events.list_global(after_seq=after, kind=None, since=None, limit=PAGE_SIZE)
        if not batch:
            break
        for event in batch:
            after = event.seq or after
            if event.kind in ADMIN_KINDS:
                items.append(
                    {
                        "seq": event.seq,
                        "ts": event.ts.isoformat(),
                        "kind": event.kind,
                        "principal": event.principal,
                        "payload": event.payload,
                    }
                )
                if len(items) >= limit:
                    break
    return {"items": items, "next_cursor": after}
