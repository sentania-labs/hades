"""GitHub health (25): the App's public identity, whether its key is present and its
public-key fingerprint, and per registered repository whether an installation covers it
and what the last check found. `check` mints a token per repository and discards it.

Connect GitHub (crucible#120, ADR 0017): the operator creates the App with one click
(`github_manifest`, crucible#168), the only way to connect one; the service then owns
the credential (the `crucible-github-app` Secret on Kubernetes, the files
beside `github.app.private_key_path` with Docker). The App's install link comes from its
own `html_url`. The repository picker lists what each installation covers, grouped by
account, and registers a pick with the installation id and the default branch GitHub
reports. A private repository is registered as private (ADR 0019): its preparation step
clones with a read-only installation token, and registration first proves the App can
mint one for it. An archived repository is listed but not registered, because it cannot
take a pull request."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
)
from crucible.application.admin.repositories import register as register_repository
from crucible.application.errors import ConflictError
from crucible.application.repositories import PrivateCheckoutRefusedError
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.events import EventKind
from crucible.ports.github import AppCredential, GitHubAppStoreError, GitHubError
from crucible.ports.repository import UnitOfWork


class GitHubConnectError(ConflictError):
    slug = "github-connect"
    title = "GitHub App refused"


ARCHIVED_NOT_SUPPORTED = "archived: cannot take a pull request"


def unsupported(repository: dict[str, Any]) -> str | None:
    """Why the picker cannot register this repository, in the words the page, the API
    and the CLI all show, or None when it can. A private repository can be registered
    (ADR 0019); an archived one cannot."""
    if repository.get("archived"):
        return ARCHIVED_NOT_SUPPORTED
    return None


def key_fingerprint(path: str | None) -> str | None:
    """sha256 of the public key's DER form, derived from the private key in memory.
    The private key never leaves the process and is never part of the answer."""
    if not path or not Path(path).is_file():
        return None
    try:
        return fingerprint_of(Path(path).read_bytes())
    except OSError:
        return None


def fingerprint_of(pem: bytes) -> str | None:
    """The public-key fingerprint of a PEM private key already in memory, or None when
    it is not one."""
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

        key = serialization.load_pem_private_key(pem, password=None)
        der = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    except Exception:
        return None
    return "sha256:" + hashlib.sha256(der).hexdigest()


Stored = tuple[dict[str, Any] | None, AppCredential | None]

# How long Status waits for the App credential store before it says it did not answer.
STORE_READ_TIMEOUT_SECONDS = 5.0


async def read_stored(ctx: AdminContext, *, timeout: float = STORE_READ_TIMEOUT_SECONDS) -> Stored:
    """`_stored` on a worker thread with a bounded wait, for an async handler: on
    Kubernetes it is a read of the App's Secret, and a slow API server must not hold the
    event loop."""
    store = getattr(ctx, "github_credentials", None)
    if store is None:
        return None, None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_stored, ctx), timeout)
    except (TimeoutError, OSError) as exc:
        detail = (
            f"the App credential store did not answer within {timeout:g} seconds"
            if isinstance(exc, TimeoutError)
            else f"the App credential store could not be reached ({type(exc).__name__})"
        )
        described = {
            "kind": "secret" if hasattr(store, "namespace") else "directory",
            "name": getattr(store, "name", None),
            "namespace": getattr(store, "namespace", None),
            "path": str(getattr(store, "directory", "")) or None,
            "exists": None,
            "detail": detail,
        }
        return described, None


def _stored(ctx: AdminContext) -> Stored:
    store = getattr(ctx, "github_credentials", None)
    if store is None:
        return None, None
    described = store.describe()
    try:
        credential = store.read()
    except GitHubAppStoreError:
        credential = None
    return described, credential


def _last_checks(uow: UnitOfWork) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for event in uow.events.list_global(
        after_seq=0, kind=EventKind.GITHUB_CHECKED.value, since=None, limit=200
    ):
        for entry in event.payload.get("repositories", []):
            out[str(entry.get("repository"))] = {
                "checked_at": event.ts.isoformat(),
                "ok": entry.get("ok"),
                "error": entry.get("error"),
            }
    return out


def status(ctx: AdminContext, uow: UnitOfWork, *, stored: Stored | None = None) -> dict[str, Any]:
    app = ctx.github_app
    last = _last_checks(uow)
    repositories: list[dict[str, Any]] = []
    for name, repo in sorted((r.name, r) for r in _registered(uow)):
        entry: dict[str, Any] = {
            "repository": name,
            "installation_covers": repo.installation_id is not None,
            "installation_id": repo.installation_id,
            "webhook_enabled": app.webhook_enabled,
        }
        entry.update({"last_check": last.get(name)})
        repositories.append(entry)
    described, credential = stored if stored is not None else _stored(ctx)
    if described is None:
        return {
            "configured": ctx.github is not None,
            "app_id": app.app_id or None,
            "api_base": app.api_base,
            "key_present": bool(app.private_key_path and Path(app.private_key_path).is_file()),
            "key_fingerprint": key_fingerprint(app.private_key_path),
            "webhook_secret_present": bool(
                app.webhook_secret_path and Path(app.webhook_secret_path).is_file()
            ),
            "webhook_enabled": app.webhook_enabled,
            "stored_in": None,
            "repositories": repositories,
        }
    return {
        "configured": credential is not None,
        "app_id": credential.app_id if credential else (app.app_id or None),
        "api_base": app.api_base,
        "key_present": bool(described.get("key_present")),
        "key_fingerprint": fingerprint_of(credential.private_key) if credential else None,
        "webhook_secret_present": bool(described.get("webhook_secret_present")),
        "webhook_enabled": app.webhook_enabled,
        "stored_in": {
            key: described.get(key)
            for key in ("kind", "name", "namespace", "path", "exists", "service_owned", "detail")
            if key in described
        },
        "repositories": repositories,
    }


def _registered(uow: UnitOfWork) -> list[Any]:
    return list(uow.repositories.list_all())


def check(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, reason: str | None
) -> dict[str, Any]:
    """25: mint an installation token per registered repository and discard it. The
    result per repository is a boolean and an error class; never a token."""
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation="github check")
    configured = getattr(ctx.github, "configured", None)
    if ctx.github is None or (callable(configured) and not configured()):
        raise ConflictError("no GitHub App is connected; connect one on the GitHub page")
    results: list[dict[str, Any]] = []
    for repo in _registered(uow):
        entry: dict[str, Any] = {"repository": repo.name, "ok": False, "error": None}
        if repo.installation_id is None:
            entry["error"] = "no installation id registered"
            results.append(entry)
            continue
        try:
            token = ctx.github.installation_token(
                installation_id=repo.installation_id, repository=repo.name
            )
            entry["ok"] = True
            entry["expires_at"] = token.expires_at.isoformat()
            token.discard()
        except Exception as exc:
            entry["error"] = type(exc).__name__
        results.append(entry)
    admin_event(
        uow,
        ctx,
        EventKind.GITHUB_CHECKED,
        principal=principal,
        reason=reason,
        before=None,
        after=None,
        repositories=results,
    )
    return {"repositories": results, "checked": len(results)}


# ----- Connect GitHub and the repository picker (crucible#120, ADR 0017) -------------


def web_base(api_base: str) -> str:
    """GitHub's web origin for its API base: `https://github.com` for api.github.com, the
    host without `/api/v3` for GitHub Enterprise Server, and the API's own origin
    otherwise (the stand-in serves both halves). The manifest form and the install page
    are web pages, not API calls (crucible#168)."""
    parts = urlsplit(api_base.rstrip("/"))
    host = parts.netloc
    path = parts.path
    # GitHub Enterprise Server first: its API is `/api/v3` on the web host itself, even
    # when that host's name happens to start with `api.`.
    if path.endswith("/api/v3"):
        return f"{parts.scheme}://{host}{path[: -len('/api/v3')]}".rstrip("/")
    if host.startswith("api."):
        return f"{parts.scheme}://{host[len('api.') :]}"
    return f"{parts.scheme}://{host}{path}".rstrip("/")


def _install_url(app: dict[str, Any], api_base: str = "https://api.github.com") -> str | None:
    """The App's install page, from the `html_url` GitHub gave, only when that is an
    HTTPS page or on GitHub's own web origin for this API."""
    html_url = app.get("html_url")
    if not isinstance(html_url, str):
        return None
    if html_url.startswith("https://") or html_url.startswith(web_base(api_base) + "/"):
        return html_url.rstrip("/") + "/installations/new"
    return None


def _install_target_url(
    app: dict[str, Any], api_base: str = "https://api.github.com"
) -> str | None:
    """The App's installation target selection page: lets the operator pick which
    account or organization to install on. Only emitted for public Apps."""
    html_url = app.get("html_url")
    if not isinstance(html_url, str):
        return None
    if html_url.startswith("https://") or html_url.startswith(web_base(api_base) + "/"):
        return html_url.rstrip("/") + "/installations/select_target"
    return None


def is_rsa(pem: bytes) -> bool:
    """GitHub App keys are RSA and JWTs are RS256; any other key is refused plainly here
    rather than failing later inside the signature."""
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

        key = serialization.load_pem_private_key(pem, password=None)
    except Exception:
        return False
    return isinstance(key, rsa.RSAPrivateKey)


def _github_refusal(exc: Exception, app_id: int, api_base: str) -> GitHubConnectError:
    if isinstance(exc, GitHubError) and exc.status in (401, 403):
        return GitHubConnectError(
            f"GitHub refused the stored key for App {app_id} (HTTP {exc.status}); if the "
            "App or its key was deleted on GitHub, create a new App on the GitHub page"
        )
    if isinstance(exc, GitHubError) and exc.status == 404:
        return GitHubConnectError(f"GitHub has no App {app_id} for this key (HTTP 404)")
    if isinstance(exc, GitHubError):
        return GitHubConnectError(
            f"GitHub answered HTTP {exc.status} when asked about App {app_id}"
        )
    return GitHubConnectError(
        f"the GitHub API at {api_base} could not be reached ({type(exc).__name__})"
    )


def keep(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    reason: str,
    app: dict[str, Any],
    private_key: bytes,
    webhook_secret: bytes | None,
    via: str,
) -> dict[str, Any]:
    """Audit, then store, an App credential GitHub has vouched for: a new App's key it
    just made (crucible#168). The audit carries the key's public fingerprint and never
    the key."""
    store = ctx.github_credentials
    assert store is not None
    app_id = int(app["id"])
    before = status(ctx, uow)
    # The event first and the store last: the credential leaves the transaction, so a
    # refusal of the event (or anything before it) must not leave a changed credential
    # with no audit record behind it.
    admin_event(
        uow,
        ctx,
        EventKind.GITHUB_APP_CONNECTED,
        principal=principal,
        reason=reason,
        before={"app_id": before["app_id"], "key_fingerprint": before["key_fingerprint"]},
        after={
            "app_id": app_id,
            "app_slug": app.get("slug"),
            "app_owner": app.get("owner"),
            "via": via,
            "key_fingerprint": fingerprint_of(private_key),
            "webhook_secret_set": webhook_secret is not None,
            "stored_in": (before.get("stored_in") or {}).get("name")
            or (before.get("stored_in") or {}).get("path"),
        },
    )
    try:
        written = store.write(app_id=app_id, private_key=private_key, webhook_secret=webhook_secret)
    except GitHubAppStoreError as exc:
        raise ConflictError(str(exc)) from None
    done = {
        **status(ctx, uow),
        "app": app,
        "install_url": _install_url(app, ctx.github_app.api_base),
    }
    if written.get("warning"):
        done["warning"] = written["warning"]
    return done


def _is_public(ctx: AdminContext) -> bool:
    """Whether the App is public: an unauthenticated GET to the App answers 404 when
    it is private. Returns True when the App is reachable without credentials,
    False when it is private and unreachable, or when a private key is not present
    to authenticate."""
    if ctx.github is None:
        return True
    configured = getattr(ctx.github, "configured", None)
    if callable(configured) and not configured():
        return True
    try:
        import requests  # noqa: PLC0415

        api_base = ctx.github_app.api_base
        health_url = f"{api_base.rstrip('/')}/app"
        resp = requests.head(health_url, timeout=5.0)
        return resp.status_code != 404
    except Exception:
        return True


def _connected(ctx: AdminContext) -> bool:
    client = getattr(ctx, "github", None)
    configured = getattr(client, "configured", None)
    return (client is not None and (not callable(configured) or bool(configured()))) and getattr(
        ctx, "github_apps", None
    ) is not None


NOT_CONNECTED = "No GitHub App is connected. Create one on the GitHub page first."


def apps_view(ctx: AdminContext, uow: UnitOfWork, *, repositories: bool = True) -> dict[str, Any]:
    """The picker: the App, its install link, and each installation's repositories,
    grouped by the account or organization it is installed on. Each repository says
    whether it is registered already, and under which name. Reads only.

    Adds `app_public` (bool) and `install_target_url` (str | None) so the page can
    guide the operator to install the App on another account or explain why that
    is not available for a private App (crucible#266). With `repositories=False` the
    installations are listed without asking GitHub what each covers (the GitHub page,
    which links to the picker on Repositories, crucible#265)."""
    connected = _connected(ctx)
    view: dict[str, Any] = {
        "connected": connected,
        "app": None,
        "app_public": True,
        "install_url": None,
        "install_target_url": None,
        "error": None,
        "installations": [],
    }
    if not connected:
        view["error"] = NOT_CONNECTED
        return view
    assert ctx.github_apps is not None
    try:
        app = ctx.github_apps.app()
        installations = ctx.github_apps.installations()
    except Exception as exc:  # the page reports a refusal, it never raises one
        view["error"] = _github_refusal(exc, _app_id(ctx), ctx.github_app.api_base).detail
        return view
    view["app"] = app
    view["app_public"] = _is_public(ctx)
    view["install_url"] = _install_url(app, ctx.github_app.api_base)
    view["install_target_url"] = _install_target_url(app, ctx.github_app.api_base)
    view["installations"] = (
        _covered(ctx, uow, installations)
        if repositories
        else sorted(installations, key=lambda i: str(i.get("account") or "").lower())
    )
    return view


def installations_view(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    """What the Repositories page's picker shows (crucible#265): each installation's
    repositories, marked, with its counts, and nothing about the App itself. Reads
    only; a refusal is reported, never raised."""
    view: dict[str, Any] = {"connected": _connected(ctx), "error": None, "installations": []}
    if not view["connected"]:
        view["error"] = NOT_CONNECTED
        return view
    assert ctx.github_apps is not None
    try:
        installations = ctx.github_apps.installations()
    except Exception as exc:
        view["error"] = _github_refusal(exc, _app_id(ctx), ctx.github_app.api_base).detail
        return view
    view["installations"] = _covered(ctx, uow, installations)
    return view


def _covered(
    ctx: AdminContext, uow: UnitOfWork, installations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each installation with the repositories it covers, each marked with the name it
    is registered under, and the counts: covered, registered, and registered against
    this installation but no longer covered by it."""
    assert ctx.github_apps is not None
    known = _registered(uow)
    registered = {repo.url.rstrip("/").removesuffix(".git").lower(): repo.name for repo in known}
    out: list[dict[str, Any]] = []
    for installation in sorted(installations, key=lambda i: str(i.get("account") or "").lower()):
        entry: dict[str, Any] = {**installation, "error": None, "repositories": []}
        try:
            repositories = ctx.github_apps.installation_repositories(int(installation["id"]))
        except Exception as exc:  # one installation's refusal never hides the rest
            cause = getattr(exc, "status", None) or type(exc).__name__
            entry["error"] = f"its repositories could not be listed ({cause})"
            repositories = []
        for repository in sorted(repositories, key=lambda r: str(r.get("full_name")).lower()):
            url = str(repository.get("html_url") or "").rstrip("/").lower()
            entry["repositories"].append(
                {
                    **repository,
                    "registered_as": registered.get(url),
                    "unsupported": unsupported(repository),
                }
            )
        seen = {
            key
            for repository in entry["repositories"]
            if (key := _repository_identity(str(repository.get("html_url") or ""))) is not None
        }
        # Unknown while the listing is refused: nothing is called uncovered on a guess.
        gone = (
            []
            if entry["error"]
            else sorted(
                repo.name
                for repo in known
                if repo.installation_id == int(installation["id"])
                and _repository_identity(repo.url) not in seen
            )
        )
        entry["no_longer_covered"] = gone
        entry["counts"] = {
            "covered": len(entry["repositories"]),
            "registered": sum(1 for r in entry["repositories"] if r["registered_as"]),
            "no_longer_covered": len(gone),
        }
        out.append(entry)
    return out


def _app_id(ctx: AdminContext) -> int:
    _, credential = _stored(ctx)
    return credential.app_id if credential else ctx.github_app.app_id


def _repository_identity(url: str) -> tuple[str, str] | None:
    """Match web and clone URLs, retaining the host to distinguish GitHub instances."""
    if "://" not in url and "@" in url:
        # Git's scp-style SSH syntax is not a URL that urlsplit can parse directly.
        authority, separator, path = url.partition(":")
        if separator:
            url = f"ssh://{authority}/{path}"
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return None
    if not host or parsed.scheme not in {"http", "https", "ssh", "git"}:
        return None
    path = parsed.path.strip("/").removesuffix(".git").lower()
    if not path:
        return None
    return host.lower(), path


def rebind_repositories(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
) -> dict[str, list[str]]:
    """Bind registered repositories to the installations of the newly installed App.

    GitHub sends the browser to the setup URL after installation. At that point the
    replacement credential is active and its installation list is authoritative. Read
    the complete list before changing anything, then audit each changed repository.
    Repositories absent from every installation stay exactly as they were.
    """
    if ctx.github_apps is None:
        raise ConflictError("no GitHub App is connected; connect one on the GitHub page")
    try:
        installations = ctx.github_apps.installations()
        visible: dict[tuple[str, str], int] = {}
        for installation in sorted(installations, key=lambda item: int(item["id"])):
            installation_id = int(installation["id"])
            for repository in ctx.github_apps.installation_repositories(installation_id):
                key = _repository_identity(str(repository.get("html_url") or ""))
                if key is not None:
                    visible.setdefault(key, installation_id)
    except (GitHubError, OSError, KeyError, TypeError, ValueError) as exc:
        raise _github_refusal(exc, _app_id(ctx), ctx.github_app.api_base) from None

    rebound: list[str] = []
    unavailable: list[str] = []
    for repository in _registered(uow):
        key = _repository_identity(repository.url)
        new_installation_id = visible.get(key) if key is not None else None
        if new_installation_id is None:
            unavailable.append(repository.name)
            continue
        if repository.installation_id == new_installation_id:
            continue
        before = {"repository": repository.name, "installation_id": repository.installation_id}
        uow.repositories.upsert(replace(repository, installation_id=new_installation_id))
        admin_event(
            uow,
            ctx,
            EventKind.REPOSITORY_REBOUND,
            principal=principal,
            reason="new GitHub App installation",
            before=before,
            after={"repository": repository.name, "installation_id": new_installation_id},
            repository=repository.name,
        )
        rebound.append(repository.name)
    return {"rebound": sorted(rebound), "unavailable": sorted(unavailable)}


def add_repository(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    installation_id: int,
    repository: str,
    name: str | None,
    policy_name: str,
    attested_all_prs: bool,
    attested_by: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """Register a repository the picker offered: the installation id, the clone URL and
    the default branch are GitHub's, read at the moment of the pick."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"github add-repository {repository}"
    )
    if ctx.github_apps is None:
        raise ConflictError("no GitHub App is connected; connect one on the GitHub page")
    try:
        covered = ctx.github_apps.installation_repositories(installation_id)
    except (GitHubError, OSError) as exc:
        raise _github_refusal(exc, _app_id(ctx), ctx.github_app.api_base) from None
    found = next(
        (r for r in covered if str(r.get("full_name")).lower() == repository.strip().lower()),
        None,
    )
    if found is None:
        raise GitHubConnectError(
            f"installation {installation_id} does not cover {repository}; install the App on "
            "it, or pick another installation"
        )
    if found.get("archived"):
        raise GitHubConnectError(f"{found['full_name']} is archived and cannot take a pull request")
    url = str(found.get("html_url") or f"https://github.com/{found['full_name']}")
    chosen = (name or "").strip() or str(found["full_name"]).rsplit("/", 1)[-1]
    existing = uow.repositories.get_by_name(chosen)
    if existing is not None and existing.url.rstrip("/").lower() != url.rstrip("/").lower():
        raise GitHubConnectError(
            f"a repository named {chosen!r} is already registered for {existing.url}; "
            "give this one another name"
        )
    return register_repository(
        ctx,
        uow,
        principal=principal,
        name=chosen,
        registration=RepositoryRegistration(
            url=url,
            default_branch=str(found.get("default_branch") or "main"),
            policy_name=policy_name,
            installation_id=installation_id,
            external_review=ExternalReviewAttestation(
                attested_all_prs=attested_all_prs, attested_by=attested_by
            ),
            # GitHub's own answer, read at the moment of the pick (ADR 0019).
            private=found.get("private") is True,
        ),
        reason=reason,
    )


SKIP_ALREADY_REGISTERED = "already_registered"
SKIP_ARCHIVED = "archived"
SKIP_UNSUPPORTED = "unsupported"


def add_repositories(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    installation_id: int,
    repositories: list[str],
    policy_name: str,
    attested_all_prs: bool,
    attested_by: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """Register several repositories one installation covers, in one action
    (crucible#265): one policy and one attestation for all of them, and one reason
    (optional, as for one registration) on every registration's audit event. Each is
    registered under its own name with GitHub's facts, as `add_repository` does. A
    repository that cannot be registered is skipped and named with why: already
    registered, archived, or unsupported (not covered by this installation, its name
    taken by another URL, or a private checkout GitHub refused). A refusal that is the
    batch's own (the reason, the policy, the attestation) refuses the whole batch."""
    reason = guard_mutation(
        ctx,
        uow,
        reason,
        principal=principal,
        operation=f"github add-repositories installation {installation_id}",
    )
    chosen = list(dict.fromkeys(r.strip() for r in repositories if r.strip()))
    if not chosen:
        raise ConflictError("no repositories were chosen")
    if ctx.github_apps is None:
        raise ConflictError("no GitHub App is connected; connect one on the GitHub page")
    try:
        covered = ctx.github_apps.installation_repositories(installation_id)
    except (GitHubError, OSError) as exc:
        raise _github_refusal(exc, _app_id(ctx), ctx.github_app.api_base) from None
    by_name = {str(r.get("full_name")).lower(): r for r in covered}
    registered = {
        key: repo.name
        for repo in _registered(uow)
        if (key := _repository_identity(repo.url)) is not None
    }
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def skip(repository: str, cause: str, why: str) -> None:
        skipped.append({"repository": repository, "cause": cause, "reason": why})

    for requested in chosen:
        found = by_name.get(requested.lower())
        if found is None:
            skip(requested, SKIP_UNSUPPORTED, f"not covered by installation {installation_id}")
            continue
        full_name = str(found["full_name"])
        url = str(found.get("html_url") or f"https://github.com/{full_name}")
        identity = _repository_identity(url)
        if identity is not None and identity in registered:
            skip(
                full_name, SKIP_ALREADY_REGISTERED, f"already registered as {registered[identity]}"
            )
            continue
        why = unsupported(found)
        if why is not None:
            skip(full_name, SKIP_ARCHIVED if found.get("archived") else SKIP_UNSUPPORTED, why)
            continue
        name = full_name.rsplit("/", 1)[-1]
        existing = uow.repositories.get_by_name(name)
        if existing is not None:
            skip(
                full_name,
                SKIP_UNSUPPORTED,
                f"the name {name!r} is registered for {existing.url}; register it on its "
                "own under another name",
            )
            continue
        try:
            done = register_repository(
                ctx,
                uow,
                principal=principal,
                name=name,
                registration=RepositoryRegistration(
                    url=url,
                    default_branch=str(found.get("default_branch") or "main"),
                    policy_name=policy_name,
                    installation_id=installation_id,
                    external_review=ExternalReviewAttestation(
                        attested_all_prs=attested_all_prs, attested_by=attested_by
                    ),
                    private=found.get("private") is True,
                ),
                reason=reason,
            )
        except PrivateCheckoutRefusedError as exc:
            skip(full_name, SKIP_UNSUPPORTED, exc.detail)
            continue
        added.append({**done, "full_name": full_name})
        if identity is not None:
            registered[identity] = name
    return {"installation_id": installation_id, "registered": added, "skipped": skipped}
