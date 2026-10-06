"""`GET /harnesses` and `GET /images` (07, 13, 25): installed and supported versions,
the enable flags with their reasons, the sanitized credential state, and the images a
provider can see with their promotion state. Read-only; the mutations are C5b."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from crucible.application.harnesses import HarnessRegistry, credential_state
from crucible.contracts.api import (
    HarnessCredentialView,
    HarnessList,
    HarnessView,
    ImageList,
    ImageView,
)
from crucible.domain.entities import HarnessState
from crucible.ports.execution import ImageInfo
from crucible.ports.harness import CredentialSource, HarnessGate, SessionCompatibility
from crucible.ports.repository import UnitOfWork


def harness_list(
    uow: UnitOfWork,
    registry: HarnessRegistry | None,
    *,
    gates: Mapping[str, HarnessGate],
    sources: Mapping[str, CredentialSource],
    images: Sequence[ImageInfo],
) -> HarnessList:
    if registry is None:
        return HarnessList(items=[])
    states: dict[str, HarnessState] = {s.name: s for s in uow.harnesses.list_all()}
    defaults = {d.harness: d for d in uow.harness_images.list_all()}
    items: list[HarnessView] = []
    for adapter in registry:
        state = states.get(adapter.name)
        gate = gates.get(adapter.name, HarnessGate())
        by_admin = state.enabled if state is not None else True
        decided = state is not None and state.enabled_decided
        installed = sorted(
            {version for i in images if (version := i.version_of(adapter.name)) is not None}
        )
        spec = adapter.credential_spec()
        credential = credential_state(
            spec, sources.get(spec.harness if spec else adapter.name), state
        )
        default = defaults.get(adapter.name)
        # hades #174: an administrator's decision replaces the configuration default,
        # whose reason stays as a warning.
        gate_reason = gate.reason if not gate.enabled and not decided else ""
        reasons = [r for r in (gate_reason, state.reason if state else "") if r]
        items.append(
            HarnessView(
                name=adapter.name,
                enabled=by_admin if decided else gate.enabled and by_admin,
                enabled_by_configuration=gate.enabled,
                enabled_by_administrator=by_admin,
                decided_by_administrator=decided,
                warning=(gate.reason or "off in configuration") if not gate.enabled else "",
                reason="; ".join(reasons),
                supported_versions=adapter.supported_versions.text,
                installed_versions=installed,
                capabilities=adapter.capabilities().as_dict(),
                credential=HarnessCredentialView(
                    **credential.as_dict(),
                    session_compatibility=(
                        state.session_compatibility
                        if state is not None
                        else SessionCompatibility.UNVERIFIED.value
                    ),
                    refresh_requires_rw=state.refresh_requires_rw if state else None,
                    mount_mode_observed=state.mount_mode_observed if state else None,
                    last_validated_at=state.last_validated_at if state else None,
                    last_auth_failure_at=state.last_auth_failure_at if state else None,
                    last_launch_at=state.last_launch_at if state else None,
                    last_launch_outcome=state.last_launch_outcome if state else None,
                ),
                default_image=(
                    {
                        "reference": default.reference,
                        "digest": default.digest,
                        "version": default.version,
                    }
                    if default is not None
                    else None
                ),
                last_test=state.last_test if state is not None else None,
                previous_image=(
                    {
                        "reference": default.previous_reference or "",
                        "digest": default.previous_digest,
                        "version": default.previous_version or "",
                    }
                    if default is not None and default.previous_digest
                    else None
                ),
            )
        )
    return HarnessList(items=items)


def image_list(
    uow: UnitOfWork,
    registry: HarnessRegistry | None,
    images: Sequence[tuple[str, ImageInfo]],
) -> ImageList:
    """Every labelled image, with the harnesses it is the default or the rollback image
    of (13, ADR 0018). An image no harness has promoted is a `candidate`."""
    defaults = list(uow.harness_images.list_all())
    items: list[ImageView] = []
    for provider_name, image in images:
        supported_for = sorted(
            harness
            for harness, version in image.harnesses.items()
            if (adapter := registry.get(harness) if registry else None) is not None
            and adapter.supported_versions.supports(version)
        )
        default_for = sorted(d.harness for d in defaults if d.digest == image.digest)
        previous_for = sorted(d.harness for d in defaults if d.previous_digest == image.digest)
        items.append(
            ImageView(
                reference=image.reference,
                digest=image.digest,
                harnesses=dict(image.harnesses),
                supported=bool(image.harnesses) and len(supported_for) == len(image.harnesses),
                promotion_state=(
                    "default" if default_for else "retained" if previous_for else "candidate"
                ),
                provider=provider_name,
                supported_for=supported_for,
                default_for=default_for,
                previous_for=previous_for,
            )
        )
    return ImageList(items=items)
