"""Repository registration as an administrative mutation (25, 04).

04's `PUT /repositories/{name}` is the ordinary registration path. The row of 25's
operations table is the same registration under the administrative surface, and so it
obeys the two rules every administrative mutation obeys: a reason, and a live supervisor
lease. The event carries both, with the before-and-after summary, which is why this
wrapper exists rather than the router calling the legacy service directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.errors import ConflictError
from crucible.application.repositories import register_repository
from crucible.contracts.api import RepositoryRegistration
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def _view(uow: UnitOfWork, name: str) -> dict[str, Any] | None:
    existing = uow.repositories.get_by_name(name)
    if existing is None:
        return None
    return {
        "repository": existing.name,
        "url": existing.url,
        "default_branch": existing.default_branch,
        "policy_name": existing.policy_name,
        "installation_id": existing.installation_id,
        "external_review_attested": existing.external_review_attested,
        "private": existing.private,
    }


def list_all(uow: UnitOfWork) -> list[dict[str, Any]]:
    return [view for item in uow.repositories.list_all() if (view := _view(uow, item.name))]


def register(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    name: str,
    registration: RepositoryRegistration,
    reason: str | None,
) -> dict[str, Any]:
    """The admin path's registration: guarded, and the event says who, why, and what it
    replaced. The returned document is the same on both entry points."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"repositories register {name}"
    )
    before = _view(uow, name)
    repo = register_repository(
        uow,
        ctx.clock,
        principal_name=principal,
        name=name,
        registration=registration,
        reason=reason,
        before=before,
        github=ctx.github,
    )
    return {
        "repository": repo.name,
        "id": repo.id,
        "url": repo.url,
        "default_branch": repo.default_branch,
        "policy_name": repo.policy_name,
        "installation_id": repo.installation_id,
        "external_review_attested": repo.external_review_attested,
        "private": repo.private,
    }


def remove(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    name: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx,
        uow,
        reason,
        principal=principal,
        operation=f"repositories remove {name}",
        reason_required=True,
    )
    before = _view(uow, name)
    if before is None:
        raise ConflictError(f"repository {name!r} is not registered")
    if not uow.repositories.remove(name):
        raise ConflictError(f"repository {name!r} is referenced by tasks and cannot be removed")
    admin_event(
        uow,
        ctx,
        EventKind.REPOSITORY_REMOVED,
        principal=principal,
        reason=reason,
        before=before,
        after=None,
        repository=name,
    )
    return {"repository": name, "removed": True}


# ----- The installation picker's list (crucible#265) ----------------------------------

PICKER_PAGE_SIZE = 25
PICKER_SORTS = ("name", "registered")
PICKER_CHOICES = ("any", "yes", "no")


@dataclass(frozen=True)
class PickerFilter:
    """How one installation's list is narrowed, ordered and cut into pages: a name
    fragment, three yes/no/any choices, a sort, and a page. Anything unknown in a query
    falls back to its default rather than refusing the page."""

    name: str = ""
    registered: str = "any"
    private: str = "any"
    archived: str = "any"
    sort: str = "name"
    page: int = 1
    per_page: int = PICKER_PAGE_SIZE

    @classmethod
    def from_query(cls, values: Mapping[str, str]) -> PickerFilter:
        def choice(key: str) -> str:
            value = str(values.get(key) or "any").strip().lower()
            return value if value in PICKER_CHOICES else "any"

        sort = str(values.get("sort") or "name").strip().lower()
        try:
            page = max(1, int(str(values.get("page") or "1")))
        except ValueError:
            page = 1
        return cls(
            name=str(values.get("name") or "").strip(),
            registered=choice("registered"),
            private=choice("private"),
            archived=choice("archived"),
            sort=sort if sort in PICKER_SORTS else "name",
            page=page,
        )

    def query(self, *, page: int | None = None) -> dict[str, str]:
        """The query string that shows this filter again, without its defaults."""
        out = {
            "name": self.name,
            "registered": self.registered,
            "private": self.private,
            "archived": self.archived,
            "sort": self.sort,
            "page": str(page if page is not None else self.page),
        }
        defaults = PickerFilter()
        return {
            key: value
            for key, value in out.items()
            if value and value != str(getattr(defaults, key))
        }


def _wanted(choice: str, value: bool) -> bool:
    return choice == "any" or (choice == "yes") == value


def picker_matches(
    repositories: Sequence[Mapping[str, Any]], picked: PickerFilter
) -> list[Mapping[str, Any]]:
    """Every repository the filter keeps, in its order, across all pages."""
    needle = picked.name.lower()
    kept = [
        repo
        for repo in repositories
        if needle in str(repo.get("full_name") or "").lower()
        and _wanted(picked.registered, bool(repo.get("registered_as")))
        and _wanted(picked.private, repo.get("private") is True)
        and _wanted(picked.archived, repo.get("archived") is True)
    ]

    def by_name(repo: Mapping[str, Any]) -> str:
        return str(repo.get("full_name") or "").lower()

    kept.sort(key=by_name)
    if picked.sort == "registered":
        # Registered first, each half by name.
        kept.sort(key=lambda repo: not repo.get("registered_as"))
    return kept


def picker_page(repositories: Sequence[Mapping[str, Any]], picked: PickerFilter) -> dict[str, Any]:
    """One page of the filtered list, and where it sits among the rest."""
    kept = picker_matches(repositories, picked)
    pages = max(1, -(-len(kept) // picked.per_page))
    page = min(picked.page, pages)
    start = (page - 1) * picked.per_page
    return {
        "repositories": kept[start : start + picked.per_page],
        "matching": len(kept),
        "total": len(repositories),
        "page": page,
        "pages": pages,
    }
