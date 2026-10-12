# ADR 0030: UI sessions are server-side

## Status

Accepted.

## Context

The administrative UI previously placed the operator's bearer token and CSRF value in
an itsdangerous-signed cookie. A signature prevents modification, but it does not hide
the cookie's contents. Any system or person that obtained the cookie could therefore
recover and use the bearer token outside the UI.

## Decision

Administrative UI sessions are records in the `ui_sessions` table. The browser cookie
contains only a signed, opaque session identifier. The corresponding record identifies
the principal and holds the CSRF value, creation time, expiry, and last-seen time.

The bearer token is used only to authenticate the sign-in request. It never leaves that
request in a response cookie or other session state. A missing, expired, or orphaned
server-side record is not an authenticated session. Sign-out deletes the record, and
sign-in removes expired records.

The preauthentication cookie used to protect the sign-in form is unchanged, as is
bearer authentication for `/v1`.

## Consequences

Possession of a UI cookie no longer discloses an API bearer token. Sessions can be
revoked immediately by deleting their rows, at the cost of one database lookup for
authenticated UI requests. Deployments must apply migration `0033_ui_sessions` before
serving the updated UI.

## Addendum: device sign-in (hades #576, U9)

A device token is exchanged once at `POST /ui/device-sign-in` for a session of this
kind; it is the same door, not a second session mechanism. The token authenticates
only that request, as a bearer token does at the sign-in form, and never reaches the
cookie. Revoking the device disables its principal, so its sessions stop being
sessions at once.
