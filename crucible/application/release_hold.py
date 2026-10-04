"""The durable release hold set while a Crucible merge commit has red CI on main.

hades #411: one setting, `release.main_ci_hold`, carries the hold and the watch list it
is decided from. Every merge Crucible makes is added to `watching`; the delivery
coordinator judges each commit once its checks complete. `watermark_at` is the merge
time of the newest commit judged so far: anything merged before it is superseded and
dropped unpolled, except the held red commit itself, whose re-run can still clear it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from crucible.domain.entities import ProviderSetting
from crucible.domain.time import parse_rfc3339
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

SETTING_NAME = "release.main_ci_hold"


def hold_document(uow: UnitOfWork) -> dict[str, Any]:
    """A copy of the hold's document, `{}` before the first merge was watched."""
    setting = uow.provider_settings.get(SETTING_NAME)
    return dict(setting.document) if setting is not None else {}


def release_held(uow: UnitOfWork) -> bool:
    """What the tag and release path reads: no tag while this is true."""
    return hold_document(uow).get("held") is True


def watched(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(entry) for entry in document.get("watching", []) if isinstance(entry, dict)]


def prune_superseded(document: dict[str, Any]) -> None:
    """Drop every watched commit merged before the watermark but the held one."""
    watermark = document.get("watermark_at")
    if watermark is None:
        return
    limit = parse_rfc3339(str(watermark))
    held = str(document.get("merge_sha") or "") if document.get("held") else ""
    document["watching"] = [
        entry
        for entry in watched(document)
        if entry.get("merge_sha") == held or parse_rfc3339(str(entry["merged_at"])) > limit
    ]


def watch_merge(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task_id: str,
    repository: str,
    installation_id: int | None,
    base_ref: str,
    merge_sha: str,
    pull_request: int,
    merged_at: datetime,
) -> None:
    """Record one merge Crucible made, to be judged on main (hades #411)."""
    document = hold_document(uow)
    entries = watched(document)
    if any(entry.get("merge_sha") == merge_sha for entry in entries):
        return
    entries.append(
        {
            "task_id": task_id,
            "repository": repository,
            "installation_id": installation_id,
            "base_ref": base_ref,
            "merge_sha": merge_sha,
            "pull_request": pull_request,
            "merged_at": merged_at.isoformat(),
        }
    )
    document["watching"] = entries
    numbers = [int(n) for n in document.get("merged_pull_requests", [])]
    if pull_request not in numbers:
        numbers.append(pull_request)
    document["merged_pull_requests"] = numbers
    document.setdefault("held", False)
    set_release_hold(uow, clock, document=document)


def set_release_hold(uow: UnitOfWork, clock: Clock, *, document: dict[str, Any]) -> None:
    uow.provider_settings.put(
        ProviderSetting(
            name=SETTING_NAME,
            document=document,
            updated_at=clock.now(),
            updated_by="crucible",
            reason="main CI must be green before tagging",
        )
    )
