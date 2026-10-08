"""The one-click GitHub App (crucible#168): GitHub's App manifest flow, as Chronicle does it.

The operator presses Create GitHub App. The service records a start (a single-use
`state`, good for 15 minutes, kept only as its sha256) and hands the browser a manifest
to post to GitHub's own "create a GitHub App" page, for the operator's personal account
or an organization they name. GitHub creates the App and redirects the operator's
browser back to `/ui/github/callback` with a one-time code; the service checks the state,
exchanges the code once (`POST /app-manifests/{code}/conversions`), and keeps the App ID,
private key and webhook secret in the credential store it owns (ADR 0017). None of them
is shown, logged or audited; the key's public fingerprint is. The browser then installs
the App from its own page, and GitHub sends it back to the repository picker.

The App asks for spec 23's permission set and nothing else: Metadata read, Contents
and Pull requests write, Checks read, Actions write (so a `ci-decision rerun` can re-run
the failed jobs itself, hades #435) and Issues read. No Issues write. An App created
before #435 keeps Actions read until the operator grants Actions write; Hades reads the
installation's grant on every rerun decision rather than assuming it.

No public DNS is involved: every redirect is of the operator's own browser, and the one
call the service makes is outbound. The manifest turns the webhook off, so GitHub never
needs to reach Hades (it polls).

The external URL the redirects use is the one the operator's browser used (the form
post's `Origin`), unless the `github.external_url` setting overrides it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlsplit

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
    record_refusal,
)
from crucible.application.admin.github import GitHubConnectError, is_rsa, keep, web_base
from crucible.application.errors import (
    ApplicationError,
    ConflictError,
    ContractValidationError,
)
from crucible.domain.entities import GitHubManifestState, ProviderSetting
from crucible.domain.events import EventKind
from crucible.ports.github import GitHubError
from crucible.ports.repository import UnitOfWork

SETTING_NAME = "github.external_url"
STATE_TTL = timedelta(minutes=15)
DEFAULT_NAME_PREFIX = "Hades"
# Spec 23's permission set, exactly: nothing else, and no Issues write. Actions write is
# for `POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs` (hades #435).
PERMISSIONS = {
    "metadata": "read",
    "contents": "write",
    "pull_requests": "write",
    "checks": "read",
    "actions": "write",
    "issues": "read",
}
CALLBACK_PATH = "/ui/github/callback"
SETUP_PATH = "/ui/github/installed"
WEBHOOK_PATH = "/v1/github/webhook"
# GitHub's own limits: an App name is at most 34 characters, a login at most 39.
MAX_NAME = 34
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
CODE = re.compile(r"[A-Za-z0-9_-]{1,128}")
OPERATION = "github create-app"
# The browser that pressed Create keeps a nonce in a cookie named for its start; the
# return must carry it, so a state read from anywhere else finishes nothing.
BINDING_COOKIE_PREFIX = "crucible_github_start_"


class GitHubManifestStateError(ConflictError):
    slug = "github-manifest-state"
    title = "GitHub App creation could not be finished"


def default_app_name() -> str:
    """ "Hades" and a short random suffix, because an App name is unique across GitHub;
    the operator may change it before pressing Create."""
    return f"{DEFAULT_NAME_PREFIX}-{secrets.token_hex(3)}"


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def binding_cookie(state: str) -> str:
    """The name of the cookie that ties a start to the browser that made it."""
    return BINDING_COOKIE_PREFIX + _hash(state)[:16]


def binding_cookie_path(external_url: str) -> str:
    """Where the binding cookie is scoped: `/ui/github`, under whatever path prefix
    `external_url` carries. GitHub returns the browser to `external_url` plus
    `CALLBACK_PATH`, and a deployment behind a reverse proxy path (for example
    `https://host/crucible`) serves that at `/crucible/ui/github/callback` in the
    browser's own address bar, so the cookie must be scoped there too, or the browser
    never sends it back."""
    prefix = urlsplit(external_url).path.rstrip("/")
    return f"{prefix}/ui/github"


def normalize_external_url(value: str, *, path: str = "url") -> str:
    """An http(s) origin, and an optional path prefix, with no trailing slash. No
    credentials, query or fragment: it is where GitHub sends the operator's browser."""
    text = value.strip().rstrip("/")
    parts = urlsplit(text)
    problem = None
    if parts.scheme not in ("http", "https"):
        problem = "must start with https:// or http://"
    elif not parts.hostname:
        problem = "must name a host"
    elif parts.username or parts.password:
        problem = "must not carry a user name or password"
    elif parts.query or parts.fragment:
        problem = "must not carry a query or a fragment"
    if problem:
        raise ContractValidationError(
            f"the external URL {problem}", errors=[{"path": path, "message": problem}]
        )
    return text


# ----- the external URL setting -----------------------------------------------------


def external_url_view(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    """The `github.external_url` setting: the saved override, or none, in which case the
    manifest flow uses the URL the operator's browser used for the page."""
    row = uow.provider_settings.get(SETTING_NAME)
    url = (row.document.get("url") or None) if row is not None else None
    return {
        "setting": SETTING_NAME,
        "url": url,
        "source": "database" if url else "browser",
        "updated_at": row.updated_at.isoformat() if row is not None else None,
        "updated_by": row.updated_by if row is not None else None,
        "reason": row.reason if row is not None else None,
    }


def save_external_url(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, url: str | None, reason: str | None
) -> dict[str, Any]:
    """Save the override, or clear it with an empty URL so the browser's URL is used."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="github set-external-url"
    )
    cleaned = normalize_external_url(url) if url and url.strip() else ""
    before = external_url_view(ctx, uow)
    uow.provider_settings.put(
        ProviderSetting(
            name=SETTING_NAME,
            document={"url": cleaned},
            updated_at=ctx.clock.now(),
            updated_by=principal,
            reason=reason,
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.GITHUB_EXTERNAL_URL_UPDATED,
        principal=principal,
        reason=reason,
        before={"url": before["url"]},
        after={"url": cleaned or None},
    )
    return external_url_view(ctx, uow)


# ----- the manifest ------------------------------------------------------------------


def build_manifest(app_name: str, external_url: str) -> dict[str, Any]:
    """What GitHub pre-fills: the name, where Hades is, where to send the browser after
    the App is created (`redirect_url`) and after it is installed (`setup_url`), spec
    23's permissions and nothing more, no events, and the webhook off."""
    return {
        "name": app_name,
        "url": external_url,
        "description": "Crucible delivery: opens and follows pull requests.",
        "public": False,
        "redirect_url": f"{external_url}{CALLBACK_PATH}",
        "setup_url": f"{external_url}{SETUP_PATH}",
        "setup_on_update": True,
        "hook_attributes": {"url": f"{external_url}{WEBHOOK_PATH}", "active": False},
        "default_permissions": dict(PERMISSIONS),
        "default_events": [],
    }


def manifest_target_url(web: str, state: str, organization: str | None = None) -> str:
    """GitHub's "create a GitHub App" page, for the operator's account or an org's."""
    if organization:
        return f"{web}/organizations/{quote(organization)}/settings/apps/new?state={state}"
    return f"{web}/settings/apps/new?state={state}"


def _app_name(value: str | None) -> str:
    name = (value or "").strip() or default_app_name()
    if len(name) > MAX_NAME or any(ord(c) < 32 for c in name):
        raise ContractValidationError(
            f"the App name must be 1 to {MAX_NAME} printable characters",
            errors=[{"path": "app_name", "message": f"at most {MAX_NAME} characters"}],
        )
    return name


def _organization(value: str | None) -> str | None:
    login = (value or "").strip()
    if not login:
        return None
    if not LOGIN.fullmatch(login):
        raise ContractValidationError(
            f"{login!r} is not a GitHub organization login",
            errors=[{"path": "organization", "message": "letters, digits and single hyphens"}],
        )
    return login


def _require_store(ctx: AdminContext) -> None:
    if ctx.github_credentials is None or ctx.github_apps is None:
        raise ConflictError(
            "this deployment has nowhere to keep a GitHub App credential: run on the "
            "Kubernetes provider, or set github.app.private_key_path (ADR 0017)"
        )


def start(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    app_name: str | None,
    organization: str | None,
    browser_url: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """Record a start and return what the browser posts to GitHub: the target page (with
    the state in it) and the manifest. The state is returned here once and stored only
    as its hash."""
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation=OPERATION)
    _require_store(ctx)
    name = _app_name(app_name)
    org = _organization(organization)
    saved = external_url_view(ctx, uow)["url"]
    if saved:
        external = str(saved)
    elif browser_url:
        external = normalize_external_url(browser_url, path="external_url")
    else:
        raise ContractValidationError(
            "the external URL is not known: open the GitHub page in a browser, or save "
            "the github.external_url setting",
            errors=[{"path": "external_url", "message": "required"}],
        )
    now = ctx.clock.now()
    uow.github_manifest_states.prune(now - timedelta(days=1))
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    expires = now + STATE_TTL
    uow.github_manifest_states.add(
        GitHubManifestState(
            state_hash=_hash(state),
            browser_hash=_hash(nonce),
            principal=principal,
            app_name=name,
            organization=org,
            external_url=external,
            created_at=now,
            expires_at=expires,
        )
    )
    manifest = build_manifest(name, external)
    admin_event(
        uow,
        ctx,
        EventKind.GITHUB_APP_MANIFEST_STARTED,
        principal=principal,
        reason=reason,
        before=None,
        after={
            "app_name": name,
            "organization": org,
            "external_url": external,
            "expires_at": expires.isoformat(),
        },
    )
    return {
        "target_url": manifest_target_url(web_base(ctx.github_app.api_base), state, org),
        "manifest": manifest,
        "state": state,
        # For the starting browser's cookie (`binding_cookie(state)`), never shown.
        "browser_nonce": nonce,
        "expires_at": expires.isoformat(),
        "account": org or "your personal account",
    }


def _refuse(ctx: AdminContext, principal: str, detail: str) -> GitHubManifestStateError:
    record_refusal(ctx, principal=principal, operation=OPERATION, detail=detail)
    return GitHubManifestStateError(detail)


def _consume(
    ctx: AdminContext, *, principal: str, state: str, browser_nonce: str | None
) -> GitHubManifestState:
    """Spend the state in a transaction of its own, committed before GitHub is asked,
    so a second return with it is refused whatever happens to the first. Only the
    starter's own return spends it: another administrator's, or one from a browser that
    does not hold the start's cookie, is refused and leaves it unspent."""
    now = ctx.clock.now()
    found = None
    browser_hash = _hash(browser_nonce or "")
    if state and len(state) <= 128:
        with ctx.uow_factory() as own:
            found = own.github_manifest_states.consume(
                _hash(state), now, principal=principal, browser_hash=browser_hash
            )
            own.commit()
    if found is None:
        raise _refuse(
            ctx, principal, "the state GitHub returned matches no start of Create GitHub App"
        )
    if found.consumed_at is not None:
        raise _refuse(ctx, principal, "this Create GitHub App return was used already; start again")
    if found.expires_at <= now:
        raise _refuse(
            ctx,
            principal,
            f"this Create GitHub App start expired after {int(STATE_TTL.total_seconds() // 60)}"
            " minutes; start again",
        )
    if found.principal != principal:
        raise _refuse(
            ctx, principal, "this Create GitHub App start belongs to another administrator"
        )
    if not browser_nonce or found.browser_hash != browser_hash:
        raise _refuse(
            ctx,
            principal,
            "this Create GitHub App start was made in another browser; finish it there, or "
            "start again here",
        )
    return found


def complete(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    code: str,
    state: str,
    browser_nonce: str | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Check the state and the browser, exchange the code once, keep what GitHub hands
    back. The answer is the GitHub status with the new App and its install link, never a
    secret."""
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation=OPERATION)
    _require_store(ctx)
    started = _consume(ctx, principal=principal, state=state, browser_nonce=browser_nonce)
    if not CODE.fullmatch(code or ""):
        raise _refuse(ctx, principal, "GitHub returned no usable code")
    assert ctx.github_apps is not None
    try:
        made = ctx.github_apps.convert_manifest(code)
    except (GitHubError, OSError) as exc:
        cause = f"HTTP {exc.status}" if isinstance(exc, GitHubError) else type(exc).__name__
        raise GitHubConnectError(
            f"GitHub did not hand over the new App ({cause}); the code is good once and for "
            "an hour, so start Create GitHub App again"
        ) from None
    # From here GitHub has made an App whose code is spent: a refusal names it, so the
    # operator can delete it on GitHub rather than leave it behind.
    stranded = (
        f"; GitHub made the App {made.slug or made.app_id}, which Crucible could not keep: "
        "delete it in GitHub's App settings and start again"
    )
    if not is_rsa(made.private_key):
        raise GitHubConnectError(
            "GitHub handed over a key that is not an RSA private key" + stranded
        )
    app = {
        "id": made.app_id,
        "slug": made.slug,
        "name": made.name,
        "owner": made.owner,
        "html_url": made.html_url,
    }
    try:
        done = keep(
            ctx,
            uow,
            principal=principal,
            reason=reason,
            app=app,
            private_key=made.private_key,
            webhook_secret=made.webhook_secret,
            via="manifest" + (f" for {started.organization}" if started.organization else ""),
        )
    except ApplicationError as exc:
        raise GitHubConnectError(exc.detail + stranded) from None
    done["external_url"] = started.external_url
    return done


__all__ = [
    "PERMISSIONS",
    "SETTING_NAME",
    "binding_cookie",
    "binding_cookie_path",
    "build_manifest",
    "complete",
    "default_app_name",
    "external_url_view",
    "manifest_target_url",
    "save_external_url",
    "start",
]
