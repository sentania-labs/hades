from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.threaded_router import ThreadedAPIRouter
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import (
    _base,
    _check_words,
    _document_section,
    _page,
    _redirect,
    templates,
)
from crucible.adapters.ui.session import _admin, _require, _session
from crucible.application.admin import (
    github,
    github_manifest,
)
from crucible.application.errors import (
    ApplicationError,
    ConflictError,
)
from crucible.domain.entities import Principal, Role
from crucible.ports.repository import UnitOfWork

router = ThreadedAPIRouter(prefix="/ui", include_in_schema=False)


@router.get("/github", response_class=HTMLResponse)
def github_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    """crucible#120, #168: create the App with one click and install it from its own
    link. Picking repositories from what each installation covers is on Repositories
    (crucible#265), which this page links to."""
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    assert ctx.admin is not None
    state = github.status(ctx.admin, uow)
    apps = github.apps_view(ctx.admin, uow, repositories=False) if state["configured"] else None
    admin = principal.role is Role.ADMIN
    # crucible#115: the connection in plain words and the registered repositories first;
    # the stored-credential document is behind Details.
    connection: dict[str, Any] = {
        "title": "Connection",
        "columns": ["Part", "State"],
        "rows": [
            [
                "App",
                {
                    "kind": "status",
                    "value": f"connected, App {state['app_id']}"
                    if state["configured"]
                    else "not connected",
                    "tone": "ok" if state["configured"] else "warn",
                },
            ],
            ["Private key", state["key_fingerprint"] or "none stored"],
            ["Webhook", "on" if state["webhook_enabled"] else "off"],
            *[
                [
                    f"Repository {repo['repository']}",
                    {
                        "kind": "note",
                        "value": "covered by the installation"
                        if repo["installation_covers"]
                        else "no installation covers it",
                        "hint": _check_words(repo.get("last_check")),
                    },
                ]
                for repo in state["repositories"]
            ],
        ],
        "details": [_document_section("Stored App and every repository", state)],
    }
    if state["configured"]:
        connection["rows"].append(
            [
                "Repositories",
                {
                    "kind": "link",
                    "href": "/ui/repositories",
                    "label": "Pick and register repositories on Repositories",
                },
            ]
        )
    if admin and state["configured"]:
        # A read-only check: no reason is asked for (crucible#117).
        connection["form"] = {
            "action": "/ui/actions/github-check",
            "label": "Check every repository",
            "fields": [{"name": "reason", "label": "Reason"}],
        }
    sections: list[dict[str, Any]] = [connection]
    if apps is not None:
        if apps["error"]:
            sections.append({"title": "Installations", "note": apps["error"]})
        if apps["install_url"]:
            installed = bool(apps["installations"])
            install_section: dict[str, Any] = {
                "title": "Install the App" if not installed else "Install it somewhere else",
                "note": (
                    "GitHub asks which account or organization, and which of its "
                    "repositories, the App may see, then sends you back to Repositories "
                    "to pick them."
                    if not installed
                    else "To deliver to another account or organization, or to more of "
                    "its repositories, install or configure the App there; GitHub sends "
                    "you back to Repositories."
                ),
                "button": {"href": apps["install_url"], "label": "Install on GitHub"},
                "columns": ["App", "Install link"],
                "rows": [
                    [
                        (apps["app"] or {}).get("name") or (apps["app"] or {}).get("slug"),
                        {"href": apps["install_url"], "label": apps["install_url"]},
                    ]
                ],
            }
            # AC2: show whether the App is public or private
            app_info: list[list[Any]] = [
                [
                    "App visibility",
                    {"kind": "note", "value": "public" if apps.get("app_public") else "private"},
                ]
            ]
            if apps.get("app_public") and apps.get("install_target_url"):
                # AC1: install-on-another-account link
                app_info.append(
                    [
                        "Install on another account",
                        {
                            "kind": "link",
                            "href": apps["install_target_url"],
                            "label": "Select another account or organization",
                        },
                    ]
                )
            else:
                # AC2: make-public guidance for private Apps
                app_info.append(
                    [
                        "Install on another account",
                        {
                            "kind": "note",
                            "value": (
                                "The App is private and can only be installed on one account. "
                                "Anyone with the App's private key can install it on a public "
                                "account or organization. Making the App public exposes its name, "
                                "slug, description, URL, and permissions to everyone. Hades acts "
                                "only on registered repositories, so nothing outside those is "
                                "affected by a public installation."
                            ),
                        },
                    ]
                )
            install_section["rows"] = app_info + install_section["rows"]
            sections.append(install_section)
        if apps["installations"]:
            sections.append(
                {
                    "title": "Installations",
                    "note": "Pick the repositories each installation covers on Repositories.",
                    "button": {"href": "/ui/repositories", "label": "Pick repositories"},
                    "columns": ["Account", "Installation", ""],
                    "rows": [
                        [
                            f"{installation.get('account')} "
                            f"({installation.get('account_type') or 'account'})",
                            installation["id"],
                            {
                                "kind": "link",
                                "href": f"/ui/repositories?installation={installation['id']}"
                                f"#installation-{installation['id']}",
                                "label": "Pick repositories",
                            },
                        ]
                        for installation in apps["installations"]
                    ],
                }
            )
    if admin:
        sections.append(_github_create_section(configured=state["configured"]))
        sections.append(_github_external_url_section(ctx, uow))
    return _page(
        request,
        principal,
        csrf,
        active="/ui/github",
        data_page="github",
        heading="GitHub",
        intro=(
            "Create the App and install it. Pick the repositories Crucible delivers to on "
            "Repositories."
        ),
        sections=sections,
        badge="connected" if state["configured"] else "not connected",
        badge_kind="ok" if state["configured"] else "warn",
    )


def _github_create_section(*, configured: bool) -> dict[str, Any]:
    """crucible#168: Create GitHub App, GitHub's manifest flow, the only way to connect
    an App (the operator, 2026-09-27)."""
    return {
        "title": "Create the GitHub App" if not configured else "Replace the App",
        "note": (
            "One button: GitHub opens its own create-an-App page with everything filled in "
            "(the name, the permissions Crucible needs, no webhook). Confirm there, and "
            "GitHub sends your browser back here; Crucible keeps the App's key itself and "
            "never shows it. Then install the App and pick repositories."
            if not configured
            else "Create a new App to replace the connected one. The connected App keeps "
            "working until the new one is stored. A new App has new installations: install "
            "it, then Crucible rebinds every registered repository the new App can see "
            "and names any repository left unchanged."
        ),
        "form": {
            "action": "/ui/actions/github-create-app",
            "label": "Create GitHub App",
            "collapsed": "Create a new App instead" if configured else None,
            "fields": [
                {
                    "name": "app_name",
                    "label": "App name (unique on GitHub; edit it if you like)",
                    "value": github_manifest.default_app_name(),
                    "required": True,
                },
                {
                    "name": "organization",
                    "label": "Organization (empty: your personal account)",
                    "placeholder": "for example octo-lab",
                },
                {"name": "reason", "label": "Reason"},
            ],
        },
    }


def _github_external_url_section(ctx: Any, uow: UnitOfWork) -> dict[str, Any]:
    """The `github.external_url` setting (crucible#168): where GitHub sends the browser
    back. Empty uses the address the browser used for this page."""
    view = github_manifest.external_url_view(ctx.admin, uow)
    return {
        "title": "Return address",
        "note": (
            f"GitHub sends your browser back to {view['url']} (saved)."
            if view["url"]
            else "GitHub sends your browser back to the address you are using for this page. "
            "Only your browser needs to reach it; no public DNS record is needed."
        ),
        "form": {
            "action": "/ui/actions/github-external-url",
            "label": "Save return address",
            "collapsed": "Change the return address",
            "fields": [
                {
                    "name": "external_url",
                    "label": "Crucible's address as your browser reaches it (empty: this page's)",
                    "value": view["url"] or "",
                    "placeholder": "https://hades.example.internal",
                },
                {"name": "reason", "label": "Reason"},
            ],
        },
    }


def _browser_url(request: Request) -> str:
    """The origin the operator's browser used: a form post's `Origin`, else the URL
    this request arrived on."""
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin
    return str(request.base_url).rstrip("/")


def _github_return(request: Request, ctx: Any, uow: UnitOfWork) -> Response | None:
    """GitHub's redirect is cross-site, so the Strict session cookie does not come with
    it. The first arrival without a session gets a page that reloads this same URL from
    Crucible's own site, which the cookie does come with; a second arrival without one
    is not signed in, and goes to sign-in and back."""
    if _session(request, ctx, uow) is not None:
        return None
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "hop"]
    query = "&".join(f"{quote(k)}={quote(v)}" for k, v in params)
    here = request.url.path + (f"?{query}" if query else "")
    if request.query_params.get("hop") == "1":
        return RedirectResponse(f"/ui/sign-in?next={quote(here)}", status_code=303)
    target = here + ("&" if query else "?") + "hop=1"
    response = templates.TemplateResponse(
        request=request, name="github_return.html", context={"target": target}
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _to_github_page(
    message: str, kind: str = "ok", *, page: str = "/ui/github"
) -> RedirectResponse:
    return RedirectResponse(f"{page}?kind={quote(kind)}&message={quote(message)}", status_code=303)


@router.get("/github/callback", response_class=HTMLResponse)
def github_callback(request: Request, ctx: Ctx, uow: UoW) -> Response:
    """Where GitHub sends the browser once it has created the App (crucible#168)."""
    bounced = _github_return(request, ctx, uow)
    if bounced is not None:
        return bounced
    found = _session(request, ctx, uow)
    assert found is not None
    principal, _ = found
    try:
        _admin(principal)
        if ctx.admin is None:
            raise ConflictError("the administrative surface is not configured")
        state = request.query_params.get("state", "")
        done = github_manifest.complete(
            ctx.admin,
            uow,
            principal=principal.name,
            code=request.query_params.get("code", ""),
            state=state,
            browser_nonce=request.cookies.get(github_manifest.binding_cookie(state)),
        )
        uow.commit()
    except ApplicationError as exc:
        return _to_github_page(exc.detail, "bad")
    app = done.get("app") or {}
    response = _to_github_page(
        f"Created the GitHub App {app.get('slug') or app.get('name')} (App {app.get('id')}) "
        "and connected it. Next: install it."
    )
    response.delete_cookie(
        github_manifest.binding_cookie(state),
        path=github_manifest.binding_cookie_path(done["external_url"]),
    )
    return response


@router.get("/github/installed", response_class=HTMLResponse)
def github_installed(request: Request, ctx: Ctx, uow: UoW) -> Response:
    """Where GitHub sends the browser after an install (the manifest's `setup_url`)."""
    bounced = _github_return(request, ctx, uow)
    if bounced is not None:
        return bounced
    found = _session(request, ctx, uow)
    assert found is not None
    principal, _ = found
    try:
        _admin(principal)
        if ctx.admin is None:
            raise ConflictError("the administrative surface is not configured")
        result = github.rebind_repositories(ctx.admin, uow, principal=principal.name)
        uow.commit()
    except ApplicationError as exc:
        return _to_github_page(exc.detail, "bad")
    parts = ["Installed on GitHub."]
    if result["rebound"]:
        parts.append("Rebound repositories: " + ", ".join(result["rebound"]) + ".")
    if result["unavailable"]:
        parts.append(
            "Not visible to the new App and left unchanged: "
            + ", ".join(result["unavailable"])
            + "."
        )
    if not result["rebound"] and not result["unavailable"]:
        parts.append("No registered repositories needed rebinding.")
    parts.append("Pick the repositories to deliver to below.")
    # crucible#265: the apps is on Repositories now, so an install lands there.
    return _to_github_page(" ".join(parts), page="/ui/repositories")


async def _action_github_create_app(
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
    started = github_manifest.start(
        ctx.admin,
        uow,
        principal=principal.name,
        app_name=form.get("app_name"),
        organization=form.get("organization"),
        browser_url=_browser_url(request),
        reason=reason,
    )
    uow.commit()
    context = _base(
        request,
        principal,
        csrf,
        title="Continue on GitHub",
        active="/ui/github",
        data_page="github-continue",
    )
    context.update(
        target_url=started["target_url"],
        manifest=json.dumps(started["manifest"], separators=(",", ":")),
        manifest_pretty=json.dumps(started["manifest"], indent=2),
        app_name=started["manifest"]["name"],
        account=started["account"],
        permissions=", ".join(
            f"{name.replace('_', ' ')} {level}"
            for name, level in started["manifest"]["default_permissions"].items()
        ),
    )
    response = templates.TemplateResponse(
        request=request, name="github_continue.html", context=context
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Ties the start to this browser (crucible#168). Lax, because GitHub's
    # redirect back is a cross-site navigation, which a Lax cookie comes with.
    response.set_cookie(
        github_manifest.binding_cookie(started["state"]),
        started["browser_nonce"],
        max_age=int(github_manifest.STATE_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_browser_url(request).startswith("https://"),
        path=github_manifest.binding_cookie_path(started["manifest"]["url"]),
    )
    return response


register("github-create-app", _action_github_create_app)


async def _action_github_external_url(
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
    saved = github_manifest.save_external_url(
        ctx.admin,
        uow,
        principal=principal.name,
        url=form.get("external_url"),
        reason=reason,
    )
    uow.commit()
    return _redirect(
        form,
        f"Saved: GitHub sends the browser back to {saved['url']}."
        if saved["url"]
        else "Cleared: GitHub sends the browser back to the address you use.",
    )


register("github-external-url", _action_github_external_url)


async def _action_github_check(
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
    github.check(ctx.admin, uow, principal=principal.name, reason=reason)
    return None


register("github-check", _action_github_check)
