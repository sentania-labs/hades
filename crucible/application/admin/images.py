"""Image administration (13, 25, ADR 0018): list, promote, roll back.

Promotion is per harness, the operator's decision of 2026-09-25 (crucible#116): each
harness has its own default worker image, promoted and rolled back on its own. A worker
image may carry several harnesses; promoting it for one leaves every other harness's
default where it was. Every promotion and rollback is an explicit admin act recorded as
an `image_promoted` event."""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
)
from crucible.application.admin.harnesses import list_images as provider_images
from crucible.application.errors import ConflictError, NotFoundError
from crucible.application.harness_views import image_list
from crucible.domain.entities import HarnessImage
from crucible.domain.events import EventKind
from crucible.ports.execution import ImageInfo
from crucible.ports.harness import parse_version
from crucible.ports.repository import UnitOfWork


def offered_tag(reference: str) -> bool:
    """Whether the Images page offers an image: a release version or `latest`, never a
    CI proof tag (`ci-*`, crucible#111), which exists to prove a build and nothing
    else. A reference by digest alone is offered: it names one exact image."""
    name = reference.rsplit("/", 1)[-1]
    if "@" in name:
        return True
    tag = name.rsplit(":", 1)[1] if ":" in name else "latest"
    return not tag.startswith("ci-")


async def list_all(ctx: AdminContext, uow: UnitOfWork) -> list[dict[str, Any]]:
    images = await provider_images(ctx)
    return [i.model_dump(mode="json") for i in image_list(uow, ctx.harnesses, images).items]


def _entry(reference: str | None, digest: str | None, version: str | None) -> dict[str, str]:
    return {"reference": reference or "", "digest": digest or "", "version": version or ""}


def _image_tag(reference: str) -> str:
    """Return the tag portion of a reference (after the last colon)."""
    name = reference.rsplit("/", 1)[-1]
    if ":" in name:
        return name.rsplit(":", 1)[1]
    return "latest"


def _label_for_digest(
    group: list[dict[str, str]],
) -> tuple[dict[str, str], tuple[int, int, int]]:
    """Given a group of images that share one digest, pick the best label.

    Returns a (label-dict, sort-key) tuple.  The highest-release version
    is the label; if there is only one member the label is just the
    reference with no parenthetical suffix.
    """

    def _sort_key(e: dict[str, str]) -> tuple[int, int, int]:
        tag = _image_tag(e["reference"])
        try:
            return parse_version(tag)
        except ValueError:
            return (0, 0, 0)

    sorted_group = sorted(group, key=_sort_key)
    best = sorted_group[-1]
    if len(sorted_group) == 1:
        return best, _sort_key(best)
    # Others (excluding the best), in descending version order so newest listed first.
    others = [
        _image_tag(e["reference"])
        for e in sorted(
            [e for e in sorted_group if e is not best],
            key=_sort_key,
            reverse=True,
        )
    ]
    return (
        {
            "reference": f"{_image_tag(best['reference'])} (same image as {', '.join(others)})",
            "digest": best["digest"],
            "version": _image_tag(best["reference"]),
        },
        _sort_key(best),
    )


async def defaults(ctx: AdminContext, uow: UnitOfWork) -> list[dict[str, Any]]:
    """One row per harness: its default image, the image a rollback returns to, and the
    images it may be promoted to (they carry it at a version inside its adapter's
    tested range, and are a release or `latest`)."""
    images = await provider_images(ctx)
    rows: list[dict[str, Any]] = []
    for adapter in ctx.harnesses:
        current = uow.harness_images.get(adapter.name)
        digest_groups: dict[str, list[dict[str, str]]] = {}
        for _, image in images:
            version = image.version_of(adapter.name)
            if (
                version is None
                or not adapter.supported_versions.supports(version)
                or not offered_tag(image.reference)
            ):
                continue
            digest_groups.setdefault(image.digest, []).append(
                _entry(image.reference, image.digest, version)
            )
        # One entry per digest, labelled with highest release, sorted newest first.
        labelled: list[tuple[dict[str, str], tuple[int, int, int]]] = [
            _label_for_digest(g) for g in digest_groups.values()
        ]
        labelled.sort(key=lambda t: t[1], reverse=True)
        # Map digest → grouped label for the current-image cell.
        digest_to_label: dict[str, dict[str, str]] = {}
        for label, _sk in labelled:
            digest_to_label[label["digest"]] = label

        # Build the current-image cell using the grouped label when applicable.
        current_label: dict[str, str] | None = None
        if current is not None:
            if current.digest in digest_to_label:
                current_label = digest_to_label[current.digest]
            else:
                current_label = _entry(current.reference, current.digest, current.version)

        rows.append(
            {
                "harness": adapter.name,
                "supported_versions": adapter.supported_versions.text,
                "current": current_label,
                "previous": (
                    _entry(
                        current.previous_reference,
                        current.previous_digest,
                        current.previous_version,
                    )
                    if current is not None and current.previous_digest
                    else None
                ),
                "choices": [label for label, _sk in labelled],
            }
        )
    return rows


def _find(images: list[tuple[str, ImageInfo]], digest: str) -> ImageInfo:
    for _, image in images:
        if digest in (image.digest, image.reference):
            return image
    raise NotFoundError(f"no provider lists an image with digest or reference {digest!r}")


def _view(row: HarnessImage | None) -> dict[str, Any]:
    if row is None:
        return {"current": None, "previous": None}
    return {
        "current": _entry(row.reference, row.digest, row.version),
        "previous": (
            _entry(row.previous_reference, row.previous_digest, row.previous_version)
            if row.previous_digest
            else None
        ),
    }


def _result(row: HarnessImage) -> dict[str, Any]:
    return {
        "harness": row.harness,
        "digest": row.digest,
        "reference": row.reference,
        "version": row.version,
        "promotion_state": "default",
        "previous": _view(row)["previous"],
    }


async def promote(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    digest: str,
    reason: str | None,
) -> dict[str, Any]:
    """Make one image the default for one harness (13, ADR 0018). The image must carry
    that harness at a version inside its adapter's tested range; the other harnesses it
    carries are neither checked nor moved. The image it replaces becomes the one a
    rollback returns to."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"images promote {harness}"
    )
    adapter = ctx.harnesses.get(harness)
    if adapter is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    image = _find(await provider_images(ctx), digest)
    if not offered_tag(image.reference):
        raise ConflictError(
            f"{image.reference} is a CI proof tag, which proves a build and is not a "
            "candidate; promote a release or latest"
        )
    version = image.version_of(harness)
    if version is None:
        raise NotFoundError(
            f"image {image.reference} does not carry {harness} "
            f"(it carries {', '.join(sorted(image.harnesses)) or 'no harness'})"
        )
    if not adapter.supported_versions.supports(version):
        raise NotFoundError(
            f"image {image.reference} carries {harness} {version}, "
            f"outside the adapter's range {adapter.supported_versions.text}"
        )
    existing = uow.harness_images.get(harness)
    before = _view(existing)
    if existing is not None and existing.digest == image.digest:
        previous = (
            existing.previous_digest,
            existing.previous_reference,
            existing.previous_version,
        )
    elif existing is not None:
        previous = (existing.digest, existing.reference, existing.version)
    else:
        previous = (None, None, None)
    row = uow.harness_images.put(
        HarnessImage(
            harness=harness,
            digest=image.digest,
            reference=image.reference,
            version=version,
            updated_at=ctx.clock.now(),
            updated_by=principal,
            reason=reason,
            previous_digest=previous[0],
            previous_reference=previous[1],
            previous_version=previous[2],
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.IMAGE_PROMOTED,
        principal=principal,
        reason=reason,
        before=before,
        after=_view(row),
        harness=harness,
        digest=row.digest,
        reference=row.reference,
        version=row.version,
        rollback=False,
    )
    return _result(row)


async def rollback(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, harness: str, reason: str | None
) -> dict[str, Any]:
    """Return one harness to the image its last promotion replaced (ADR 0018). The two
    swap, so a second rollback undoes the first. Every other harness stays where it is."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"images rollback {harness}"
    )
    adapter = ctx.harnesses.get(harness)
    if adapter is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    existing = uow.harness_images.get(harness)
    if existing is None or not existing.previous_digest:
        raise ConflictError(f"{harness} has no previous worker image to roll back to")
    if not adapter.supported_versions.supports(existing.previous_version or ""):
        raise ConflictError(
            f"the previous image carries {harness} {existing.previous_version}, outside the "
            f"adapter's range {adapter.supported_versions.text}; promote a supported image"
        )
    listed = {image.digest for _, image in await provider_images(ctx)}
    if existing.previous_digest not in listed:
        raise ConflictError(
            f"no provider lists {existing.previous_reference} any more, so {harness} "
            "cannot go back to it; promote an image the providers see"
        )
    before = _view(existing)
    row = uow.harness_images.put(
        HarnessImage(
            harness=harness,
            digest=existing.previous_digest,
            reference=existing.previous_reference or "",
            version=existing.previous_version or "",
            updated_at=ctx.clock.now(),
            updated_by=principal,
            reason=reason,
            previous_digest=existing.digest,
            previous_reference=existing.reference,
            previous_version=existing.version,
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.IMAGE_PROMOTED,
        principal=principal,
        reason=reason,
        before=before,
        after=_view(row),
        harness=harness,
        digest=row.digest,
        reference=row.reference,
        version=row.version,
        rollback=True,
    )
    return _result(row)
