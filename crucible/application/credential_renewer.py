"""Single-writer renewal for the Codex ChatGPT login.

Workers receive only the projection returned by :func:`access_token_document`. The
stored ``auth.json`` remains private to the supervisor and is replaced atomically.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from crucible.domain.events import EventKind
from crucible.ports.harness import AuthFile, CredentialSpec, MountMode

# Verified from the pinned Codex 0.156.0 binary. These are public OAuth application
# metadata, not credentials.
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
MINIMUM_REFRESH_INTERVAL = timedelta(minutes=1)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class InvalidGrantError(Exception):
    """The one stored refresh token has been revoked or reused elsewhere."""


Grant = Callable[[str], Mapping[str, Any]]
Recorder = Callable[[EventKind, Mapping[str, Any]], None]
Propagator = Callable[[Mapping[str, str]], None]
Wake = Callable[[str], None]


class CredentialReader(Protocol):
    def read(self) -> dict[str, Any]: ...

    def is_dead(self) -> bool: ...


class ReadOnlyCredentialStore:
    """The API status view: no grant, token read, write, or dead-marker mutation."""

    def __init__(self, reader: CredentialReader) -> None:
        self._reader = reader

    @property
    def dead(self) -> bool:
        return self._reader.is_dead()

    @property
    def last_refresh(self) -> str | None:
        value = self._reader.read().get("last_refresh")
        return value if isinstance(value, str) else None


class CredentialStore(CredentialReader, Protocol):
    def write(self, document: Mapping[str, Any]) -> None: ...

    def mark_dead(self, document: Mapping[str, Any]) -> None: ...


class FileCredentialReader:
    def __init__(self, login_path: Path) -> None:
        self.login_path = login_path
        self.dead_path = login_path.with_name(login_path.name + ".dead")

    def read(self) -> dict[str, Any]:
        document = json.loads(self.login_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Codex auth.json is not an object")
        return document

    def is_dead(self) -> bool:
        return self.dead_path.exists()


class FileCredentialStore(FileCredentialReader):
    def write(self, document: Mapping[str, Any]) -> None:
        atomic_write(self.login_path, document)

    def mark_dead(self, document: Mapping[str, Any]) -> None:
        atomic_write(self.dead_path, document)


class SecretReader(Protocol):
    def get(self, kind: str, name: str) -> dict[str, Any]: ...


class SecretWriter(SecretReader, Protocol):
    def patch(
        self,
        kind: str,
        name: str,
        body: Mapping[str, Any],
        *,
        resource_version: str | None = None,
    ) -> dict[str, Any]: ...


class KubernetesCredentialReader:
    """Read the service-held login without a Secret mutation method."""

    def __init__(self, client: SecretReader, secret_name: str = "crucible-harness-codex") -> None:
        self.client = client
        self.secret_name = secret_name

    def _body(self) -> dict[str, Any]:
        body = self.client.get("secrets", self.secret_name)
        if not isinstance(body, dict):
            raise ValueError("Codex credential Secret is not an object")
        return body

    def read(self) -> dict[str, Any]:
        return self._document(self._body())

    @staticmethod
    def _document(body: Mapping[str, Any]) -> dict[str, Any]:
        raw = (body.get("data") or {}).get("auth.json")
        if not isinstance(raw, str):
            raise ValueError("Codex credential Secret has no auth.json")
        document = json.loads(base64.b64decode(raw))
        if not isinstance(document, dict):
            raise ValueError("Codex auth.json is not an object")
        return document

    def is_dead(self) -> bool:
        return "credential-dead.json" in (self._body().get("data") or {})


class KubernetesCredentialStore(KubernetesCredentialReader):
    """Patch against the version of the login read before the grant.

    A metadata edit must not discard already rotated tokens. On conflict, retry
    the same document against the new version only if the login token is unchanged.
    """

    def __init__(self, client: SecretWriter, secret_name: str = "crucible-harness-codex") -> None:
        super().__init__(client, secret_name)
        self.client: SecretWriter = client
        self._resource_version: str | None = None
        self._refresh_token: str | None = None

    def read(self) -> dict[str, Any]:
        # Token and version must come from the same GET snapshot.
        body = self._body()
        version = (body.get("metadata") or {}).get("resourceVersion")
        if not isinstance(version, str):
            raise ValueError("Codex credential Secret has no resourceVersion")
        document = self._document(body)
        self._resource_version = version
        tokens = document.get("tokens")
        token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
        self._refresh_token = token if isinstance(token, str) else None
        return document

    def write(self, document: Mapping[str, Any]) -> None:
        if self._resource_version is None:
            raise ValueError("read the Codex credential before writing it")
        used_token = self._refresh_token
        encoded = base64.b64encode(
            json.dumps(document, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        while True:
            try:
                body = self.client.patch(
                    "secrets",
                    self.secret_name,
                    {"data": {"auth.json": encoded}},
                    resource_version=self._resource_version,
                )
            except Exception as exc:
                if getattr(exc, "status", None) != 409:
                    raise
                self.read()
                if not used_token or self._refresh_token != used_token:
                    raise  # A new login wins over the old login's grant.
            else:
                self._resource_version = body["metadata"]["resourceVersion"]
                tokens = document.get("tokens")
                token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
                self._refresh_token = token if isinstance(token, str) else None
                return

    def mark_dead(self, document: Mapping[str, Any]) -> None:
        if self._resource_version is None:
            raise ValueError("read the Codex credential before marking it dead")
        used_token = self._refresh_token
        encoded = base64.b64encode(
            json.dumps(document, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        while True:
            try:
                body = self.client.patch(
                    "secrets",
                    self.secret_name,
                    {"data": {"credential-dead.json": encoded}},
                    resource_version=self._resource_version,
                )
            except Exception as exc:
                if getattr(exc, "status", None) != 409:
                    raise
                self.read()
                if not used_token or self._refresh_token != used_token:
                    raise  # A new login wins over the old login's dead marker.
            else:
                self._resource_version = body["metadata"]["resourceVersion"]
                return


def _claim(token: str, name: str) -> Any:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        document = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document.get(name) if isinstance(document, dict) else None


def _tokens(login: Mapping[str, Any]) -> Mapping[str, Any]:
    value = login.get("tokens")
    if not isinstance(value, dict):
        raise ValueError("Codex auth.json has no tokens object")
    return value


def access_token_document(login: Mapping[str, Any]) -> dict[str, str]:
    """Return the complete and deliberately narrow worker credential document."""
    tokens = _tokens(login)
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        raise ValueError("Codex auth.json has no access token")
    account = tokens.get("account_id") or login.get("account_id")
    if not isinstance(account, str) or not account:
        auth = _claim(access, "https://api.openai.com/auth")
        if isinstance(auth, dict):
            account = auth.get("chatgpt_account_id")
    expiry = _claim(access, "exp")
    if not isinstance(account, str) or not account:
        raise ValueError("Codex auth.json has no ChatGPT account id")
    if not isinstance(expiry, (int, float)):
        raise ValueError("Codex access token has no expiry")
    return {
        "access_token": access,
        "account_id": account,
        "expires_at": datetime.fromtimestamp(expiry, UTC).isoformat(),
    }


def worker_credential_spec(spec: CredentialSpec) -> CredentialSpec:
    """The access-only shape mounted into a renewer-mode worker."""
    return CredentialSpec(
        harness=spec.harness,
        mount_target=spec.mount_target,
        auth_files=(AuthFile("access-token.json", json=True, sync_back=False),),
        minimum_mode=MountMode.RENEWER,
        config_dir_env=spec.config_dir_env,
        login_hint=spec.login_hint,
    )


def atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".crucible-renew")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def oauth_refresh(refresh_token: str) -> Mapping[str, Any]:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CODEX_CLIENT_ID,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        error = ""
        try:
            payload = json.loads(exc.read())
            error = str(payload.get("error") or "") if isinstance(payload, dict) else ""
        except (ValueError, UnicodeDecodeError):
            pass
        if error in ("invalid_grant", "refresh_token_reused"):
            raise InvalidGrantError(error) from exc
        raise
    if not isinstance(result, dict):
        raise ValueError("OAuth refresh response was not an object")
    return result


_PROCESS_LOCK = threading.Lock()


class CodexCredentialRenewer:
    def __init__(
        self,
        login_path: Path | None = None,
        *,
        store: CredentialStore | None = None,
        grant: Grant = oauth_refresh,
        clock: Clock | None = None,
        record: Recorder | None = None,
        propagate: Propagator | None = None,
        wake: Wake | None = None,
    ) -> None:
        if store is None:
            if login_path is None:
                raise ValueError("a Codex credential store is required")
            store = FileCredentialStore(login_path)
        self.store = store
        self.grant = grant
        self.clock = clock or SystemClock()
        self.record = record or (lambda _kind, _payload: None)
        self.propagate = propagate or (lambda _document: None)
        self.wake = wake or (lambda _summary: None)
        self._dead_wake_raised = False
        self._pending_request_checker: Callable[[], bool] | None = None
        self._pending_request_ack: Callable[[], None] | None = None

    def set_pending_request_checker(self, checker: Callable[[], bool] | None) -> None:
        """Attach a callback that returns True when a refresh is pending."""
        self._pending_request_checker = checker

    def set_pending_request_ack(self, ack: Callable[[], None] | None) -> None:
        """Attach a callback that durably advances the pending-request cursor.

        Called only once the request has reached a terminal outcome: the refresh
        succeeded, or the login is now dead. A transient failure leaves the cursor
        where it is so the next tick retries the same request (339).
        """
        self._pending_request_ack = ack

    @property
    def dead(self) -> bool:
        return self.store.is_dead()

    def _read(self) -> dict[str, Any]:
        return self.store.read()

    def refresh(self, reason: str, *, force: bool = False) -> bool:
        """Refresh once under the process lock. True means a grant was performed."""
        with _PROCESS_LOCK:
            if self.dead:
                raise InvalidGrantError("stored Codex credential is dead")
            login = self._read()
            last = login.get("last_refresh")
            if not force and isinstance(last, str):
                try:
                    refreshed = datetime.fromisoformat(last.replace("Z", "+00:00"))
                except ValueError:
                    refreshed = None
                recent = refreshed is not None and (
                    self.clock.now() - refreshed < MINIMUM_REFRESH_INTERVAL
                )
                if recent:
                    self.propagate(access_token_document(login))
                    return False
            refresh_token = _tokens(login).get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                self._die("invalid_grant: the stored login has no refresh token")
                raise InvalidGrantError("stored Codex login has no refresh token")
            try:
                response = self.grant(refresh_token)
            except InvalidGrantError as exc:
                self._die(f"{exc}: the login was revoked or refreshed by another session")
                raise
            tokens = dict(_tokens(login))
            for key in ("access_token", "refresh_token", "id_token", "account_id"):
                value = response.get(key)
                if isinstance(value, str) and value:
                    tokens[key] = value
            updated = dict(login)
            updated["tokens"] = tokens
            updated["last_refresh"] = self.clock.now().isoformat()
            self.store.write(updated)
            projection = access_token_document(updated)
            self.propagate(projection)
            self.record(
                EventKind.CREDENTIAL_REFRESHED,
                {"harness": "codex", "reason": reason, "result": "refreshed"},
            )
            return True

    def due_at(self) -> datetime:
        login = self._read()
        projection = access_token_document(login)
        expiry = datetime.fromisoformat(projection["expires_at"])
        last_text = login.get("last_refresh")
        last = (
            datetime.fromisoformat(last_text.replace("Z", "+00:00"))
            if isinstance(last_text, str)
            else self.clock.now()
        )
        return last + (expiry - last) * 0.75

    def refresh_if_due(self) -> bool:
        """Timer entry point. It is a no-op until 75 percent of the token life."""
        if self.dead or self.clock.now() < self.due_at():
            return False
        return self.refresh("access token reached 75 percent of its lifetime")

    def refresh_on_request(self) -> bool:
        """Check for a pending refresh request (339). Returns True if one was processed.

        The API records a CREDENTIAL_REFRESH_REQUESTED event; this method checks
        via ``_pending_request_checker`` and performs the refresh if pending. The
        cursor is acknowledged only on a terminal outcome (success or dead), so a
        transient OAuth, Secret, or projection error retries on the next tick.
        """
        if self._pending_request_checker is None:
            return False
        if not self._pending_request_checker():
            return False
        try:
            performed = self.refresh("pending administrator request", force=True)
        except Exception:
            if self._pending_request_ack is not None and self.dead:
                self._pending_request_ack()
            raise
        if self._pending_request_ack is not None:
            self._pending_request_ack()
        return performed

    def _die(self, reason: str) -> None:
        self.store.mark_dead({"dead": True, "at": self.clock.now().isoformat()})
        self.record(
            EventKind.CREDENTIAL_REFRESH_FAILED,
            {"harness": "codex", "reason": reason, "result": "credential_dead"},
        )
        if not self._dead_wake_raised:
            self.wake(
                "Codex login is dead, likely because its refresh token was revoked or reused "
                "by another session. Log in again before starting new Codex workers."
            )
            self._dead_wake_raised = True
