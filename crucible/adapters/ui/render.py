from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles

from crucible.application.queries import supervisor_health
from crucible.contracts.task_contract import HarnessName
from crucible.domain.entities import Principal
from crucible.domain.secrets import redact

ROOT = Path(__file__).parent


templates = Jinja2Templates(directory=str(ROOT / "templates"))


static = StaticFiles(directory=str(ROOT / "static"))


# Grouped so the operator's path reads in order (crucible#115): what to set up, the work
# running, then administration. An entry with no link is a group's label.
NAV = (
    ("/ui", "Status"),
    ("", "Set up"),
    ("/ui/harnesses", "Harnesses"),
    ("/ui/credentials", "Credentials"),
    ("/ui/gateway", "Local gateway"),
    ("/ui/images", "Images"),
    ("/ui/routing", "Routing"),
    ("/ui/repositories", "Repositories"),
    ("/ui/github", "GitHub"),
    ("", "Work"),
    ("/ui/tasks", "Tasks"),
    ("/ui/board", "Board"),
    ("/ui/workers", "Workers"),
    ("/ui/wakes", "Wakes"),
    ("", "Admin"),
    ("/ui/tokens", "Tokens"),
    ("/ui/audit", "Audit"),
    ("/ui/settings", "Settings"),
    ("/ui/retention", "Retention"),
    ("/ui/bootstrap", "Bootstrap"),
)


# Shown only once they have something in them, or while one is open: a new deployment
# has run no cleanup and imported no ledger (crucible#115).
HIDDEN_WHEN_EMPTY = ("/ui/retention", "/ui/bootstrap")


LABELS = {
    "active": "Currently active",
    "api_base": "API base",
    "app_id": "App ID",
    "attempt_id": "Attempt ID",
    "authoritative": "Authoritative import",
    "checked_at": "Last checked",
    "clear_reason": "Clear reason",
    "cleared_at": "Cleared at",
    "cleared_by": "Cleared by",
    "committed_at": "Committed at",
    "configured": "App configured",
    "content_sha256": "Content fingerprint",
    "counts": "Tasks by state",
    "decided_by_administrator": "Decided by an administrator",
    "enabled_by_administrator": "Administrator's setting",
    "enabled_by_configuration": "Configuration default",
    "exhausted_at": "Exhausted at",
    "external_id": "External ID",
    "health_detail": "Health detail",
    "healthy": "Supervisor health",
    "holder": "Lease holder",
    "image_digest": "Image digest",
    "installation_covers": "Installation coverage",
    "installation_id": "Installation ID",
    "key_fingerprint": "Public key fingerprint",
    "key_present": "Private key",
    "last_check": "Last connectivity check",
    "last_error": "Last error",
    "last_error_at": "Last error time",
    "last_heartbeat": "Last heartbeat",
    "last_run": "Last cleanup action",
    "last_success_at": "Last successful tick",
    "last_tick_at": "Last tick",
    "lease": "Supervisor lease",
    "lists": "Tasks needing attention",
    "next_cursor": "Next cursor",
    "oldest_pending": "Oldest pending wake",
    "pending": "Deliveries pending by principal",
    "recent_actions": "Recent actions",
    "repositories": "Repositories",
    "reset_at": "Automatic reset at",
    "task_id": "Task ID",
    "tick_ms": "Tick duration (ms)",
    "unacked": "Deliveries pending",
    "updated_at": "Last updated",
    "verified_at": "Verified at",
    "webhook_enabled": "Webhook",
    "webhook_secret_present": "Webhook secret",
}


# A field is hidden when its own name says it holds a credential value (crucible#126).
# The name is compared whole, or by a credential prefix or suffix, and the names that
# only look like one are listed as what they are: a harness called `claude_code` is a
# harness, not a login code, and its version is not a secret.
SECRET_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "auth_code",
    "authorization",
    "authorization_code",
    "bearer",
    "client_secret",
    "code",
    "cookie",
    "credential_value",
    "device_code",
    "id_token",
    "oauth_token",
    "passwd",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session_token",
    "token",
    "user_code",
}


SECRET_SUFFIXES = ("_api_key", "_code", "_password", "_private_key", "_secret", "_token")


SECRET_PREFIXES = (
    "api_key_",
    "authorization_",
    "password_",
    "private_key_",
    "secret_",
    "token_",
)


# Names that describe a credential without holding one: whether it is there, what it
# fingerprints to, where it is kept, when it changed.
DESCRIBES_SECRET_SUFFIXES = ("_at", "_fingerprint", "_path", "_present", "_set", "_source")


NON_SECRET_FIELDS = {
    "error_code",
    "exit_code",
    "fenced_token",
    "http_code",
    "status_code",
    "tokens_in",
    "tokens_out",
    # Harness names key the version maps an image promotion records (crucible#126).
    *(harness.value.replace("-", "_") for harness in HarnessName),
}


# A reason is an audit note the operator may leave out (the operator's decision of
# 2026-09-25, crucible#117). These forms' services require one, because what they do is
# destructive or hard to reverse; a read-only check never asks for one.
REASON_REQUIRED_ACTIONS = frozenset(
    {
        "/ui/actions/bootstrap-commit",
        "/ui/actions/bootstrap-discard",
        "/ui/actions/repository-remove",
        "/ui/actions/token-revoke",
        "/ui/actions/token-rename",
    }
)


NO_REASON_ACTIONS = frozenset(
    {"/ui/actions/github-check", "/ui/actions/harness-test", "/ui/actions/gateway-test"}
)


# A row action names its reason mode itself, since one action path can serve both a
# check and a removal (credential validate and remove): `True` is required (destructive),
# "optional" is an audit note the operator may leave out, absent asks for none.


def _reason_fields(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every form's reason field, set by one rule rather than form by form: required
    where the service requires one, optional elsewhere, absent on a read-only check."""
    for section in sections:
        form = section.get("form")
        if not isinstance(form, dict):
            continue
        action = str(form.get("action", ""))
        fields = []
        for field in form.get("fields", []):
            if field.get("name") != "reason":
                fields.append(field)
                continue
            if action in NO_REASON_ACTIONS:
                continue
            required = action in REASON_REQUIRED_ACTIONS
            label = field.get("reason_label") or ("Reason" if required else "Reason (optional)")
            fields.append({**field, "label": label, "required": required})
        form["fields"] = fields
    return sections


def _operator_label(key: str) -> str:
    """Turn an API key into an operator label while retaining the key separately."""
    if key in LABELS:
        return LABELS[key]
    words = key.replace(".", " ").replace("_", " ").split()
    expanded = [
        word.upper() if word.lower() in {"api", "id", "sha256", "url"} else word for word in words
    ]
    label = " ".join(expanded)
    return label[:1].upper() + label[1:]


def _secret_field(key: str) -> bool:
    """Whether a field's value is a credential, decided by what its name means."""
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    name = separated.rsplit(".", 1)[-1].replace("-", "_")
    if name in NON_SECRET_FIELDS or name.endswith(DESCRIBES_SECRET_SUFFIXES):
        return False
    return (
        name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES) or name.startswith(SECRET_PREFIXES)
    )


def _safe_value(key: str, value: Any) -> Any:
    """Return readable scalar content without exposing secret-shaped values."""
    lowered = key.lower()
    if _secret_field(lowered):
        return "not displayed"
    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlsplit(value)
            url_parameters = unquote(f"{parsed.query}&{parsed.fragment}")
            sensitive_parameters = any(
                _secret_field(partition.partition("=")[0])
                for partition in re.split(r"[&?;]", url_parameters)
                if partition
            )
            if parsed.username is not None or parsed.password is not None or sensitive_parameters:
                hostname = parsed.hostname or ""
                if parsed.port is not None:
                    hostname += f":{parsed.port}"
                return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
        except ValueError:
            return "invalid URL"
    if isinstance(value, bool):
        if lowered.endswith("healthy") or lowered == "healthy":
            return "healthy" if value else "not healthy"
        if lowered.endswith("present"):
            return "present" if value else "absent"
        if lowered.endswith("enabled") or lowered.startswith("enabled_"):
            return "enabled" if value else "disabled"
        if lowered in {"active", "configured", "installation_covers"}:
            words = {
                "active": ("active", "inactive"),
                "configured": ("configured", "not configured"),
                "installation_covers": ("covered", "not covered"),
            }[lowered]
            return words[0] if value else words[1]
        if lowered == "ok":
            return "successful" if value else "failed"
        return "yes" if value else "no"
    if value is None:
        return "none"
    if isinstance(value, str):
        return redact(value)
    return value


def _flatten_table_row(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    if not value:
        return {prefix or "value": _panel(value, key=prefix)}
    row: dict[str, Any] = {}
    for child_key, item in value.items():
        path = f"{prefix}.{child_key}" if prefix else str(child_key)
        if isinstance(item, dict):
            row.update(_flatten_table_row(item, path))
        elif isinstance(item, list):
            row[path] = _panel(item, key=path)
        else:
            row[path] = _safe_value(path, item)
    return row


def _panel(value: Any, *, key: str = "") -> dict[str, Any]:
    """Build the three readable panel kinds used by the administration template."""
    if isinstance(value, dict):
        items = []
        for child_key, child in value.items():
            item: dict[str, Any] = {
                "label": _operator_label(str(child_key)),
                "source": str(child_key),
            }
            if isinstance(child, (dict, list)):
                item["panel"] = _panel(child, key=str(child_key))
            else:
                item["value"] = _safe_value(str(child_key), child)
            items.append(item)
        return {"kind": "fields", "items": items}
    if isinstance(value, list):
        if not value:
            return {"kind": "empty"}
        if all(isinstance(item, dict) for item in value):
            flattened = [_flatten_table_row(item) for item in value]
            column_keys = list(dict.fromkeys(path for row in flattened for path in row))
            return {
                "kind": "table",
                "columns": [
                    {"label": _operator_label(path), "source": path} for path in column_keys
                ],
                "rows": [[row.get(path, "none") for path in column_keys] for row in flattened],
            }
        if not any(isinstance(item, (dict, list)) for item in value):
            # A plain list reads as a list, not a one-column table headed "Value"
            # (crucible#115).
            return {"kind": "values", "items": [_safe_value(key, item) for item in value]}
        rows = [
            [_panel(item, key=key)] if isinstance(item, (dict, list)) else [_safe_value(key, item)]
            for item in value
        ]
        return {
            "kind": "table",
            "columns": [{"label": "Value", "source": key}],
            "rows": rows,
        }
    return {"kind": "value", "value": _safe_value(key, value)}


def _without_migration(note: Any) -> str:
    """A model note without the migration that wrote it (crucible#115)."""
    return re.sub(r" \(\d{4}_[a-z0-9_]+\)", "", str(note))


def _check_words(check: Any) -> str:
    """A repository's last connectivity check in one phrase."""
    if not isinstance(check, dict) or not check:
        return "not checked yet"
    when = check.get("checked_at") or check.get("at") or ""
    outcome = "passed" if check.get("ok") else f"failed: {check.get('error') or 'no detail'}"
    return f"last check {outcome} {when}".strip()


def _duration_words(milliseconds: Any) -> str:
    """A millisecond bound in the unit an operator reads it in."""
    try:
        value = int(milliseconds)
    except (TypeError, ValueError):
        return str(milliseconds)
    for unit, size in (("hour", 3_600_000), ("minute", 60_000), ("second", 1000)):
        if value >= size and value % size == 0:
            count = value // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{value} ms"


def _document_section(title: str, document: Any) -> dict[str, Any]:
    return {"title": title, "panel": _panel(document)}


templates.env.globals["panel_from_cell"] = _panel


templates.env.globals["safe_value"] = _safe_value


def _base(
    request: Request,
    principal: Principal | None,
    csrf: str = "",
    *,
    title: str,
    active: str,
    hidden: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return {
        "request": request,
        "title": title,
        "active": active,
        "nav": tuple(item for item in NAV if item[0] not in hidden or item[0] == active),
        "principal": principal,
        "csrf": csrf,
        "message": request.query_params.get("message"),
        "message_kind": request.query_params.get("kind", "info"),
        "supervisor_warning": _supervisor_warning(request) if principal is not None else None,
    }


def _supervisor_warning(request: Request) -> str | None:
    """hades #190: readiness no longer reflects the supervisor, so every signed-in page
    says when it is not healthy. None when it is, or when there is nothing to ask."""
    try:
        ctx = request.app.state.ctx
        with ctx.uow_factory() as uow:
            healthy, detail = supervisor_health(uow, ctx.clock.now(), ctx.lease_ttl_seconds)
    except (AttributeError, KeyError):
        return None
    if healthy:
        return None
    settings = getattr(ctx, "settings", None)
    timezone = settings.service.render_timezone if settings is not None else "America/Chicago"
    return str(_localize(str(detail), timezone))


def _page(
    request: Request,
    principal: Principal,
    csrf: str,
    *,
    active: str,
    heading: str,
    intro: str,
    sections: list[dict[str, Any]],
    badge: str | None = None,
    badge_kind: str = "accent",
) -> HTMLResponse:
    timezone = "America/Chicago"
    settings = getattr(request.app.state.ctx, "settings", None)
    if settings is not None:
        timezone = settings.service.render_timezone
    sections = _reason_fields(_localize(sections, timezone))
    intro = str(_localize(intro, timezone))
    context = _base(
        request, principal, csrf, title=heading, active=active, hidden=_empty_sections(request)
    )
    context.update(
        heading=heading,
        intro=intro,
        sections=sections,
        badge=badge,
        badge_kind=badge_kind,
    )
    return templates.TemplateResponse(request=request, name="page.html", context=context)


def _empty_sections(request: Request) -> frozenset[str]:
    """The navigation entries with nothing behind them yet (HIDDEN_WHEN_EMPTY)."""
    try:
        factory = request.app.state.ctx.uow_factory
    except (AttributeError, KeyError):
        return frozenset()
    empty: set[str] = set()
    with factory() as uow:
        if not list(uow.retention.list_recent(1)):
            empty.add("/ui/retention")
        if not list(uow.bootstrap_imports.list_all()):
            empty.add("/ui/bootstrap")
    return frozenset(empty)


def _localize(value: Any, timezone: str) -> Any:
    """Render stored UTC instants in the operator's configured local zone."""
    if isinstance(value, dict):
        return {key: _localize(item, timezone) for key, item in value.items()}
    if isinstance(value, list):
        return [_localize(item, timezone) for item in value]
    if isinstance(value, tuple):
        return tuple(_localize(item, timezone) for item in value)
    moment: datetime | None = value if isinstance(value, datetime) else None
    if isinstance(value, str) and "T" in value:
        pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")

        def replace(match: re.Match[str]) -> str:
            try:
                parsed = datetime.fromisoformat(match.group(0).replace("Z", "+00:00"))
            except ValueError:
                return match.group(0)
            try:
                local = parsed.astimezone(ZoneInfo(timezone))
            except ZoneInfoNotFoundError:
                local = parsed.astimezone(ZoneInfo("America/Chicago"))
            return local.strftime("%Y-%m-%d %I:%M:%S %p %Z")

        replaced = pattern.sub(replace, value)
        if replaced != value:
            return replaced
    if isinstance(value, str) and "T" in value and (value.endswith("Z") or "+" in value[10:]):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            moment = None
    if moment is None or moment.tzinfo is None:
        return value
    try:
        local = moment.astimezone(ZoneInfo(timezone))
    except ZoneInfoNotFoundError:
        local = moment.astimezone(ZoneInfo("America/Chicago"))
    return local.strftime("%Y-%m-%d %I:%M:%S %p %Z")


def _redirect(form: dict[str, str], message: str, *, kind: str = "ok") -> RedirectResponse:
    target = form.get("return_to", "/ui")
    if not target.startswith("/ui") or target.startswith("//"):
        target = "/ui"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{separator}kind={quote(kind)}&message={quote(message)}", status_code=303
    )


# Task states in the words an operator uses (crucible#115). A state not named here is
# shown as its own name with the underscores taken out.
STATE_WORDS = {
    "proposed": "Proposed: waiting for the operator",
    "sent_back": "Sent back to the orchestrator",
    "blocked": "Blocked: needs a decision",
    "pre_pr_gates_failed": "Checks failed before the pull request",
    "publish_failed": "Publishing failed",
    "ci_certification_failed": "CI did not certify",
    "head_diverged": "Branch changed outside Crucible",
    "awaiting_internal_review": "Awaiting internal review",
    "awaiting_acceptance": "Awaiting acceptance",
    "awaiting_external_review": "Awaiting external review",
    "awaiting_ci_certification": "Awaiting CI",
    "ready_for_merge": "Ready to merge",
}


CREDENTIAL_TONES = {
    "validated": "ok",
    "valid": "ok",
    "not_required": "accent",
    "configured": "warn",
    "absent": "warn",
    "invalid": "bad",
    "unreadable": "bad",
}


def _state_words(state: str) -> str:
    return STATE_WORDS.get(state, state.replace("_", " ").capitalize())
