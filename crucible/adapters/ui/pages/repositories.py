from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _page, _redirect
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    github,
    repositories,
)
from crucible.application.admin.repositories import PickerFilter, picker_matches, picker_page
from crucible.contracts.api import (
    ExternalReviewAttestation,
    RepositoryRegistration,
)
from crucible.domain.entities import Principal, Role

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("/repositories", response_class=HTMLResponse)
def repositories_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    admin = principal.role is Role.ADMIN
    names = (list(uow.policies.list_names()) or ["default-software"]) if admin else []
    items = list(uow.repositories.list_all())
    sections: list[dict[str, Any]] = [
        {
            "title": "Registered repositories",
            "columns": [
                "Name",
                "URL",
                "Default branch",
                "Policy",
                "Installation",
                "Private",
                "External review",
                "",
            ],
            "rows": [
                [
                    item.name,
                    item.url,
                    item.default_branch,
                    item.policy_name,
                    item.installation_id,
                    item.private,
                    item.external_review_attested,
                    # The row's own removal, never a typed name (crucible#127); refused
                    # while tasks reference it, and it asks for a reason (crucible#117).
                    {
                        "kind": "form",
                        "action": "/ui/actions/repository-remove",
                        "label": "Remove",
                        "danger": True,
                        "reason": True,
                        "hidden": {"name": item.name},
                    }
                    if principal.role is Role.ADMIN
                    else "",
                ]
                for item in items
            ],
        }
    ]
    if admin and (reference := request.query_params.get("batch_result")):
        result = github.batch_result(uow, reference, principal=principal.name)
        sections.insert(
            0,
            {
                "title": "Batch registration result",
                "note": batch_message(result) if result else "Batch result not found.",
            },
        )
    if ctx.admin is not None:
        sections.extend(_picker_sections(request, ctx.admin, uow, admin=admin, policies=names))
    if admin:
        sections.extend(
            [
                {
                    "title": "Register or update",
                    "note": (
                        "For a repository the picker above cannot show. The picker "
                        "fills the installation ID, the default branch and whether it is "
                        "private from GitHub. A private repository is cloned with a "
                        "read-only token from the GitHub App, so it needs the App connected "
                        "and the installation ID that covers it."
                    ),
                    "form": {
                        "action": "/ui/actions/repository-register",
                        "label": "Save registration",
                        "fields": [
                            {"name": "name", "label": "Name", "required": True},
                            {"name": "url", "label": "Clone URL", "required": True},
                            {
                                "name": "default_branch",
                                "label": "Default branch",
                                "value": "main",
                                "required": True,
                            },
                            {
                                "name": "policy_name",
                                "label": "Policy",
                                "kind": "select",
                                "options": [(n, n) for n in names],
                                "value": "default-software",
                                "required": True,
                            },
                            {
                                "name": "installation_id",
                                "label": "GitHub installation ID",
                                "kind": "number",
                            },
                            {
                                "name": "private",
                                "label": "Private (clone with the GitHub App's read-only token)",
                                "kind": "checkbox",
                            },
                            {
                                "name": "attested_all_prs",
                                "label": "External reviewer covers all PRs",
                                "kind": "checkbox",
                            },
                            {"name": "attested_by", "label": "Attested by"},
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
            ]
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/repositories",
        data_page="repositories",
        heading="Repositories",
        intro=(
            "Delivery registrations, and the repositories each GitHub installation covers: "
            "filter them, tick the ones to deliver to, and register them together."
        ),
        sections=sections,
    )


async def _action_repository_register(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    repositories.register(
        ctx.admin,
        uow,
        principal=principal.name,
        name=form.get("name", ""),
        registration=RepositoryRegistration(
            url=form.get("url", ""),
            default_branch=form.get("default_branch", "main"),
            policy_name=form.get("policy_name", "default-software"),
            installation_id=int(form["installation_id"]) if form.get("installation_id") else None,
            external_review=ExternalReviewAttestation(
                attested_all_prs=form.get("attested_all_prs") == "true",
                attested_by=form.get("attested_by") or None,
            ),
            private=form.get("private") == "true",
        ),
        reason=reason,
    )
    return None


register("repository-register", _action_repository_register)


async def _action_repository_remove(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    repositories.remove(
        ctx.admin, uow, principal=principal.name, name=form.get("name", ""), reason=reason
    )
    return None


register("repository-remove", _action_repository_remove)


# ----- The installation picker (crucible#120, moved here from GitHub by crucible#265) --

PICKER_CHOICES = [("any", "any"), ("yes", "yes"), ("no", "no")]
PICK_PREFIX = "pick:"


def _picker_link(installation_id: int, picked: PickerFilter, *, page: int | None = None) -> str:
    query = {"installation": str(installation_id), **picked.query(page=page)}
    return "/ui/repositories?" + urlencode(query)


def _picker_sections(
    request: Request, admin_ctx: Any, uow: UoW, *, admin: bool, policies: list[str]
) -> list[dict[str, Any]]:
    """Each installation's repositories, one page at a time: a filter by name,
    registered, private and archived, a sort by name or registered first, the counts,
    and (for an administrator) a tick per repository with select-all on the filter,
    registered together under one policy, one attestation and one reason."""
    view = github.installations_view(admin_ctx, uow)
    if view["error"]:
        return [
            {
                "title": "Pick repositories",
                "note": view["error"],
                "button": {"href": "/ui/github", "label": "Open GitHub"},
            }
        ]
    if not view["installations"]:
        return [
            {
                "title": "Pick repositories",
                "note": "The App is not installed anywhere yet. Install it from the GitHub "
                "page, then pick repositories here.",
                "button": {"href": "/ui/github", "label": "Open GitHub"},
            }
        ]
    query = request.query_params
    try:
        focused = int(query.get("installation") or "0")
    except ValueError:
        focused = 0
    out: list[dict[str, Any]] = []
    for installation in view["installations"]:
        installation_id = int(installation["id"])
        picked = (
            PickerFilter.from_query(dict(query)) if installation_id == focused else PickerFilter()
        )
        out.append(_installation_section(installation, picked, admin=admin, policies=policies))
    return out


def _counts_note(installation: dict[str, Any]) -> str:
    counts = installation["counts"]
    note = (
        f"Covers {counts['covered']}, registered {counts['registered']}, registered but no "
        f"longer covered {counts['no_longer_covered']}."
    )
    if installation["no_longer_covered"]:
        note += " No longer covered: " + ", ".join(installation["no_longer_covered"]) + "."
    if installation["error"]:
        note = f"{installation['error'].capitalize()}. {note}"
    return note


def _installation_section(
    installation: dict[str, Any], picked: PickerFilter, *, admin: bool, policies: list[str]
) -> dict[str, Any]:
    installation_id = int(installation["id"])
    shown = picker_page(installation["repositories"], picked)
    links = []
    if shown["page"] > 1:
        links.append(
            {
                "href": _picker_link(installation_id, picked, page=shown["page"] - 1),
                "label": "Previous page",
            }
        )
    if shown["page"] < shown["pages"]:
        links.append(
            {
                "href": _picker_link(installation_id, picked, page=shown["page"] + 1),
                "label": "Next page",
            }
        )
    section: dict[str, Any] = {
        "title": (
            f"{installation.get('account')} ({installation.get('account_type') or 'account'}), "
            f"installation {installation_id}"
        ),
        "anchor": f"installation-{installation_id}",
        "note": _counts_note(installation),
        "filter": {
            "action": "/ui/repositories",
            "label": "Filter",
            "fields": [
                {"name": "installation", "kind": "hidden", "value": installation_id},
                {
                    "name": "name",
                    "label": "Name contains",
                    "value": picked.name,
                    "placeholder": "for example widgets",
                },
                {
                    "name": "registered",
                    "label": "Registered",
                    "kind": "select",
                    "options": PICKER_CHOICES,
                    "value": picked.registered,
                },
                {
                    "name": "private",
                    "label": "Private",
                    "kind": "select",
                    "options": PICKER_CHOICES,
                    "value": picked.private,
                },
                {
                    "name": "archived",
                    "label": "Archived",
                    "kind": "select",
                    "options": PICKER_CHOICES,
                    "value": picked.archived,
                },
                {
                    "name": "sort",
                    "label": "Sort by",
                    "kind": "select",
                    "options": [("name", "name"), ("registered", "registered first")],
                    "value": picked.sort,
                },
            ],
        },
        "pager": {
            "summary": (
                f"Page {shown['page']} of {shown['pages']}: {shown['matching']} of "
                f"{shown['total']} match this filter."
            ),
            "links": links,
        },
    }
    rows = shown["repositories"]

    def why_not(repo: Any) -> str:
        return str(repo.get("unsupported") or repo["archived"])

    if not admin:
        section["columns"] = [
            "Repository",
            "Default branch",
            "Private",
            "Archived",
            "Registered as",
        ]
        section["rows"] = [
            [
                repo["full_name"],
                repo["default_branch"],
                repo["private"],
                why_not(repo),
                repo["registered_as"] or "not registered",
            ]
            for repo in rows
        ]
        section["empty"] = "No repository matches this filter."
        return section
    section["form"] = {
        "action": "/ui/actions/repository-register-batch",
        "label": "Register selected",
        "return_to": _picker_link(installation_id, picked),
        "fields": [
            {"name": "installation_id", "kind": "hidden", "value": installation_id},
            *[
                {"name": f"filter_{key}", "kind": "hidden", "value": value}
                for key, value in (
                    ("name", picked.name),
                    ("registered", picked.registered),
                    ("private", picked.private),
                    ("archived", picked.archived),
                )
            ],
            {
                "kind": "grid",
                "label": "Repositories on this page"
                if rows
                else "No repository matches this filter.",
                "columns": [
                    "Register",
                    "Repository",
                    "Default branch",
                    "Private",
                    "Archived",
                    "Registered as",
                ],
                "rows": [
                    [
                        {
                            "kind": "checkbox",
                            "name": PICK_PREFIX + repo["full_name"],
                            "label": f"Register {repo['full_name']}",
                        }
                        if not (repo["registered_as"] or repo.get("unsupported"))
                        else {"value": ""},
                        {"value": repo["full_name"]},
                        {"value": repo["default_branch"]},
                        {"value": repo["private"]},
                        {"value": why_not(repo)},
                        {"value": repo["registered_as"] or "not registered"},
                    ]
                    for repo in rows
                ],
            },
            {
                "name": "select_all",
                "label": f"Select all {shown['matching']} matching this filter, on every page",
                "kind": "checkbox",
            },
            {
                "name": "policy_name",
                "label": "Policy (for every one)",
                "kind": "select",
                "options": [(n, n) for n in policies],
                "value": "default-software",
                "required": True,
            },
            {
                "name": "attested_all_prs",
                "label": "External reviewer covers all PRs (for every one)",
                "kind": "checkbox",
            },
            {"name": "attested_by", "label": "Attested by"},
            {"name": "reason", "label": "Reason", "required": True},
        ],
    }
    return section


def batch_message(result: dict[str, Any]) -> str:
    """The per-repository result in one line: what was registered, then what was
    skipped and why."""
    registered = result["registered"]
    skipped = result["skipped"]
    parts = []
    if registered:
        parts.append(
            f"Registered {len(registered)}: "
            + ", ".join(f"{r['full_name']} as {r['repository']}" for r in registered)
            + "."
        )
    else:
        parts.append("Registered none.")
    if skipped:
        parts.append(
            f"Skipped {len(skipped)}: "
            + "; ".join(f"{s['repository']} ({s['reason']})" for s in skipped)
            + "."
        )
    return " ".join(parts)


async def _action_repository_register_batch(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    """The picker's Register selected (crucible#265): the ticked repositories, or with
    select-all every repository the filter matches across all its pages."""
    assert ctx.admin is not None
    installation_id = int(form.get("installation_id") or "0")
    chosen = [
        key[len(PICK_PREFIX) :]
        for key, value in form.items()
        if key.startswith(PICK_PREFIX) and value == "true"
    ]
    if form.get("select_all") == "true":
        picked = PickerFilter.from_query(
            {
                key: form.get(f"filter_{key}", "")
                for key in ("name", "registered", "private", "archived")
            }
        )
        view = github.installations_view(ctx.admin, uow)
        installation = next(
            (i for i in view["installations"] if int(i["id"]) == installation_id), None
        )
        if installation is None:
            return _redirect(
                form,
                view["error"] or f"installation {installation_id} is not one the App has",
                kind="bad",
            )
        chosen = [
            str(repo["full_name"]) for repo in picker_matches(installation["repositories"], picked)
        ]
    result = github.add_repositories(
        ctx.admin,
        uow,
        principal=principal.name,
        installation_id=installation_id,
        repositories=chosen,
        policy_name=form.get("policy_name") or "default-software",
        attested_all_prs=form.get("attested_all_prs") == "true",
        attested_by=form.get("attested_by") or None,
        reason=reason,
    )
    uow.commit()
    # The outcome and registrations commit together. Only a bounded reference goes
    # in Location, so large batches can always follow the redirect to their results.
    picked = PickerFilter.from_query(dict(parse_qsl(urlsplit(form.get("return_to", "")).query)))
    return RedirectResponse(
        _picker_link(installation_id, picked)
        + "&"
        + urlencode({"batch_result": result["result_seq"]}),
        status_code=303,
    )


register("repository-register-batch", _action_repository_register_batch)


async def _action_github_add_repository(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    """One pick under a name of the operator's choosing (crucible#120)."""
    assert ctx.admin is not None
    added = github.add_repository(
        ctx.admin,
        uow,
        principal=principal.name,
        installation_id=int(form.get("installation_id") or "0"),
        repository=form.get("repository", ""),
        name=form.get("name") or None,
        policy_name=form.get("policy_name") or "default-software",
        attested_all_prs=form.get("attested_all_prs") == "true",
        attested_by=None,
        reason=reason,
    )
    uow.commit()
    return _redirect(
        form,
        f"Registered {added['repository']} ({added['url']}, default branch "
        f"{added['default_branch']}, installation {added['installation_id']}).",
    )


register("github-add-repository", _action_github_add_repository)
