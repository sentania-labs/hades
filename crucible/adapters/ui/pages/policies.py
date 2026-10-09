"""The Admin policies page (hades #606): every delivery policy and its versions, the
version in force in groups, an edit of those groups published as the next version, and
each version against the one before it. Administrators only.

Registered without a navigation link: base.html and render.py belong to the navigation
task, which adds the link."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _page, _redirect
from crucible.adapters.ui.session import _admin, _require
from crucible.application.admin import credentials as credentials_admin
from crucible.application.admin import policy_editor
from crucible.application.admin import routing as routing_admin
from crucible.domain.entities import Principal, Role

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)

PATH = "/ui/policies"


def _here(name: str | None, version: int | None = None) -> str:
    if name is None:
        return PATH
    target = f"{PATH}?name={quote(name)}"
    return f"{target}&version={version}" if version is not None else target


def _value_words(value: Any) -> str:
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    return str(value)


@router.get("/policies", response_class=HTMLResponse)
def policies_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    if principal.role is not Role.ADMIN:
        refused = _page(
            request,
            principal,
            csrf,
            active=PATH,
            heading="Policies",
            intro="Delivery policies are edited by an administrator.",
            sections=[
                {
                    "title": "Administrator only",
                    "note": "This page is for administrators. Ask one to change a policy.",
                }
            ],
        )
        refused.status_code = 403
        return refused
    raw_version = request.query_params.get("version", "")
    view = policy_editor.policies_view(
        uow,
        request.query_params.get("name"),
        int(raw_version) if raw_version.isdigit() else None,
    )
    name: str | None = view["name"]
    sections: list[dict[str, Any]] = [
        {
            "title": "Policies",
            "empty": "No delivery policy is stored.",
            "columns": ["Policy", "In force", "Versions", ""],
            "rows": [
                [
                    item["name"] + (" (shown)" if item["name"] == name else ""),
                    f"version {item['in_force']}" if item["in_force"] else "none: all retired",
                    str(item["versions"]),
                    {"kind": "link", "href": _here(item["name"]), "label": "Open"},
                ]
                for item in view["policies"]
            ],
        }
    ]
    if name is None:
        return _page(
            request,
            principal,
            csrf,
            active=PATH,
            heading="Policies",
            intro="Delivery policies and their versions.",
            sections=sections,
        )
    sections.append(
        {
            "title": f"Versions of {name}",
            "note": (
                "Newest first. The newest version not retired is in force: new tasks are "
                "admitted against it. A version is never changed once published."
            ),
            "columns": ["Version", "Published", "By", "Reason", ""],
            "rows": [
                [
                    str(item["version"])
                    + (" (in force)" if item["in_force"] else "")
                    + (" (retired)" if item["retired"] else ""),
                    item["created_at"],
                    item["published_by"] or "not recorded",
                    item["reason"] or "none given",
                    {
                        "kind": "link",
                        "href": _here(name, item["version"]),
                        "label": "What changed",
                    },
                ]
                for item in view["versions"]
            ],
        }
    )
    diff = view.get("diff")
    if diff is not None:
        sections.append(
            {
                "title": (
                    f"Version {diff['version']} against version {diff['previous']}"
                    if diff["previous"] is not None
                    else f"Version {diff['version']}"
                ),
                "anchor": "diff",
                "empty": (
                    "No setting differs from the version before it."
                    if diff["previous"] is not None
                    else "The first version: there is no earlier one to compare with."
                ),
                "columns": ["Setting", "Before", "After"],
                "rows": [
                    [
                        change["path"],
                        _value_words(change["before"]),
                        _value_words(change["after"]),
                    ]
                    for change in diff["changes"]
                ],
            }
        )
    current = view.get("current")
    if current is None:
        sections.append(
            {
                "title": "In force",
                "note": f"Every version of {name} is retired, so none is in force to edit.",
            }
        )
    else:
        for group in current["groups"]:
            sections.append(
                {
                    "title": f"{group['title']} (version {current['version']})",
                    "empty": "Nothing set.",
                    "columns": ["Setting", "Value"],
                    "rows": [
                        [field["label"], _value_words(field["value"])] for field in group["fields"]
                    ],
                }
            )
        sections.append(_edit_section(name, current))
    return _page(
        request,
        principal,
        csrf,
        active=PATH,
        heading="Policies",
        intro=(
            "Delivery policies and their versions. Publishing writes the next version of the "
            "policy, recorded with you and the reason."
        ),
        sections=sections,
    )


def _edit_section(name: str, current: dict[str, Any]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = [{"kind": "hidden", "name": "name", "value": name}]
    for group in current["groups"]:
        for field in group["fields"]:
            entry: dict[str, Any] = {
                "name": policy_editor.FIELD_PREFIX + field["path"],
                "label": f"{group['title']}: {field['label']}",
            }
            if field["kind"] == "checkbox":
                entry.update(kind="checkbox", value=field["value"] is True)
            elif field["kind"] == "number":
                entry.update(kind="number", value=field["shown"], required=True)
            elif field["kind"] == "list":
                entry.update(value=field["shown"], placeholder="none, or names comma separated")
            else:
                entry.update(value=field["shown"])
            fields.append(entry)
    fields.append({"name": "publish_reason", "label": "Publish reason", "required": True})
    return {
        "title": f"Edit {name}",
        "note": (
            f"Change the settings of version {current['version']} and publish them as the "
            "next version. Lists are comma separated. A service is declared only when its "
            "declared box is ticked. The publish reason is recorded with your name."
        ),
        "form": {
            "action": "/ui/actions/policy-publish",
            "return_to": _here(name),
            "label": "Publish new version",
            "collapsed": f"Edit and publish {name}",
            "fields": fields,
        },
    }


async def _actions(
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
    _admin(principal)
    name = form.get("name", "")
    before = routing_admin.routing_in_force(uow)
    published = policy_editor.publish_policy(
        ctx.admin,
        uow,
        principal=principal,
        name=name,
        form=form,
        reason=form.get("publish_reason"),
        concurrency_modes={
            harness: credentials_admin.mount_mode_value(ctx.admin, uow, harness).value
            for harness in ctx.admin.harnesses.names()
            if (adapter := ctx.admin.harnesses.get(harness)) is not None
            and adapter.credential_spec() is not None
        },
    )
    # A changed routing name, version or pinned flag changes the routing version in
    # force, so the worker egress is set for the version it now selects.
    routing_admin.sync_policy_egress(ctx.admin, uow, before=before)
    uow.commit()
    form = {**form, "return_to": _here(name, published["version"])}
    return _redirect(
        form,
        f"Published {name} version {published['version']} by {published['published_by']}, "
        f"{len(published['changes'])} setting(s) changed against version "
        f"{published['previous']}.",
    )


register("policy-publish", _actions)
