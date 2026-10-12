"""The GitHub App credential the service owns (ADR 0017, crucible#120, #79).

Two stores behind one port. On Kubernetes the credential is the `hades-github-app`
Secret in the service's own namespace: the service creates it the first time the
Connect GitHub flow writes it, labels it as its own, and replaces its data whole on each
later write. It is read through the API server on each signature rather than from the
mounted file, so a save is in force at once instead of after the kubelet's next sync;
the mount stays for the webhook secret, which the webhook route reads as a file. With
the Docker provider the credential is the files beside `github.app.private_key_path`,
written mode 0600 by the same flow into a versioned directory and switched as one pair.

Keys and file names are the same in both: `app-id`, `app.pem`, `webhook.secret`. The App
id is a public identifier; the other two are never returned, logged or audited.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.ports.github import AppCredential, GitHubAppStoreError

APP_ID = "app-id"
PRIVATE_KEY = "app.pem"
WEBHOOK_SECRET = "webhook.secret"
CREDENTIAL_LABEL = "github-app"
VERSIONS = ".versions"
CURRENT = ".current"
LOCK = ".lock"


def _app_id(raw: bytes | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    return value if value > 0 else None


def _resolve(
    files: dict[str, bytes] | None, *, settings_app_id: int, settings_enabled: bool
) -> AppCredential | None:
    """The configured rule (ADR 0017): a key, and an App id that the service wrote beside
    it, or that the settings name with `github.enabled` on. A key alone is not enough."""
    if not files or not files.get(PRIVATE_KEY):
        return None
    stored = _app_id(files.get(APP_ID))
    if stored is not None:
        return AppCredential(stored, files[PRIVATE_KEY])
    if settings_enabled and settings_app_id > 0:
        return AppCredential(settings_app_id, files[PRIVATE_KEY])
    return None


class SecretAppCredentials:
    """The App credential as a Secret the service owns, in its own namespace."""

    def __init__(
        self,
        client: Any,
        *,
        name: str = "hades-github-app",
        settings_app_id: int = 0,
        settings_enabled: bool = False,
    ) -> None:
        self._client = client
        self.name = name
        self._settings_app_id = settings_app_id
        self._settings_enabled = settings_enabled

    @property
    def namespace(self) -> str:
        return str(getattr(self._client, "namespace", ""))

    def _body(self) -> dict[str, Any] | None:
        try:
            body: dict[str, Any] = self._client.get("secrets", self.name)
        except KubernetesApiError as exc:
            if exc.status == 404:
                return None
            raise GitHubAppStoreError(
                f"the Secret {self.name!r} in {self.namespace} is not readable ({exc.status})"
            ) from None
        self._require_opaque(body)
        return body

    def _require_opaque(self, body: dict[str, Any]) -> None:
        """Only an Opaque Secret is the App credential. A Secret of another type under
        this name (a service-account token, say) is refused, never read or adopted."""
        kind = body.get("type") or "Opaque"
        if kind != "Opaque":
            raise GitHubAppStoreError(
                f"the Secret {self.name!r} in {self.namespace} is of type {kind}, not Opaque; "
                "it is not the App credential and the service will not use or adopt it"
            )

    def _files(self, body: dict[str, Any] | None) -> dict[str, bytes] | None:
        if body is None:
            return None
        out: dict[str, bytes] = {}
        for key, raw in (body.get("data") or {}).items():
            with contextlib.suppress(ValueError):
                out[str(key)] = base64.b64decode(str(raw))
        return out

    def describe(self) -> dict[str, Any]:
        try:
            body = self._body()
        except GitHubAppStoreError as exc:
            return {
                "kind": "secret",
                "name": self.name,
                "namespace": self.namespace,
                "exists": None,
                "detail": str(exc),
            }
        files = self._files(body) or {}
        labels = ((body or {}).get("metadata") or {}).get("labels") or {}
        return {
            "kind": "secret",
            "name": self.name,
            "namespace": self.namespace,
            "exists": body is not None,
            "service_owned": labels.get(k8sspec.LABEL_MANAGED_BY) == k8sspec.MANAGED_BY_CRUCIBLE,
            "app_id_stored": _app_id(files.get(APP_ID)),
            "key_present": bool(files.get(PRIVATE_KEY)),
            "webhook_secret_present": bool(files.get(WEBHOOK_SECRET)),
        }

    def read(self) -> AppCredential | None:
        return _resolve(
            self._files(self._body()),
            settings_app_id=self._settings_app_id,
            settings_enabled=self._settings_enabled,
        )

    def write(
        self, *, app_id: int, private_key: bytes, webhook_secret: bytes | None
    ) -> dict[str, Any]:
        """Created and labelled as the service's own when absent; otherwise one merge
        patch that sets the labels and replaces the id and the key, and the webhook
        secret when one is given. The values travel in the request body only."""
        data = {APP_ID: str(app_id).encode("ascii"), PRIVATE_KEY: private_key}
        if webhook_secret:
            data[WEBHOOK_SECRET] = webhook_secret
        labels = {
            k8sspec.LABEL_MANAGED_BY: k8sspec.MANAGED_BY_CRUCIBLE,
            k8sspec.LABEL_CREDENTIAL: CREDENTIAL_LABEL,
        }
        body = k8sspec.secret(
            name=self.name, namespace=self.namespace, object_labels=labels, data=data
        )
        try:
            self._client.create("secrets", body)
            return {"store": "secret", "name": self.name, "created": True}
        except KubernetesApiError as exc:
            if exc.status != 409:
                raise GitHubAppStoreError(
                    f"the Secret {self.name!r} could not be created in {self.namespace} "
                    f"({exc.status})"
                ) from None
        try:
            current = self._client.get("secrets", self.name)
            self._require_opaque(current)
            keep = {WEBHOOK_SECRET} if not webhook_secret else set()
            stale = {
                k: None for k in (current.get("data") or {}) if k not in data and k not in keep
            }
            self._client.patch(
                "secrets",
                self.name,
                {"metadata": {"labels": labels}, "data": {**body["data"], **stale}},
            )
        except KubernetesApiError as exc:
            raise GitHubAppStoreError(
                f"the Secret {self.name!r} could not be updated in {self.namespace} ({exc.status})"
            ) from None
        return {"store": "secret", "name": self.name, "created": False}


class DirectoryAppCredentials:
    """The App credential as files beside `github.app.private_key_path` (Docker).

    The id and the key are one credential, so they change as one. Each write stages
    both in a fresh `.versions/<v>` directory and then renames one `.current` symlink
    onto it; `read` resolves that link once and reads both files from the version it
    names, so a signer never pairs one App's id with another's key. A write that fails
    before the rename leaves the previous pair in force and its stage removed. Writes
    take a lock on `.lock`, so two saves never prune each other's version. The id
    file and the key path become links through `.current`, so anything that reads the
    configured key path sees the version in force. Files placed there by hand, before
    the service ever wrote, are read as they are until the first write."""

    def __init__(
        self,
        private_key_path: str,
        *,
        webhook_secret_path: str | None = None,
        settings_app_id: int = 0,
        settings_enabled: bool = False,
    ) -> None:
        self.key_path = Path(private_key_path)
        self.directory = self.key_path.parent
        self.webhook_path = Path(webhook_secret_path) if webhook_secret_path else None
        self.app_id_path = self.directory / APP_ID
        self.versions = self.directory / VERSIONS
        self.current = self.directory / CURRENT
        self._settings_app_id = settings_app_id
        self._settings_enabled = settings_enabled

    def _version(self) -> Path | None:
        """The version directory in force, resolved once, or None before the first write."""
        try:
            return self.directory / os.readlink(self.current)
        except OSError:
            return None

    def _files(self) -> dict[str, bytes]:
        version = self._version()
        pair = (
            ((APP_ID, version / APP_ID), (PRIVATE_KEY, version / PRIVATE_KEY))
            if version is not None
            else ((APP_ID, self.app_id_path), (PRIVATE_KEY, self.key_path))
        )
        out: dict[str, bytes] = {}
        for name, path in (*pair, (WEBHOOK_SECRET, self.webhook_path)):
            if path is None:
                continue
            with contextlib.suppress(OSError):
                out[name] = path.read_bytes()
        return out

    def describe(self) -> dict[str, Any]:
        files = self._files()
        return {
            "kind": "directory",
            "path": str(self.directory),
            "exists": bool(files.get(PRIVATE_KEY)),
            "service_owned": self.current.is_symlink() or self.app_id_path.is_file(),
            "writable": self.directory.is_dir() and os.access(self.directory, os.W_OK),
            "app_id_stored": _app_id(files.get(APP_ID)),
            "key_present": bool(files.get(PRIVATE_KEY)),
            "webhook_secret_present": bool(files.get(WEBHOOK_SECRET)),
        }

    def read(self) -> AppCredential | None:
        return _resolve(
            self._files(),
            settings_app_id=self._settings_app_id,
            settings_enabled=self._settings_enabled,
        )

    def write(
        self, *, app_id: int, private_key: bytes, webhook_secret: bytes | None
    ) -> dict[str, Any]:
        if not self.directory.is_dir() or not os.access(self.directory, os.W_OK):
            raise GitHubAppStoreError(
                f"the GitHub App directory {self.directory} is missing or read-only here; "
                "the service needs it writable to own the credential (ADR 0017)"
            )
        try:
            lock = os.open(self.directory / LOCK, os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError as exc:
            raise GitHubAppStoreError(
                f"{self.directory} could not be locked for the write ({type(exc).__name__})"
            ) from None
        try:
            # One save at a time: two saves racing would each prune the other's version.
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._write_locked(app_id, private_key, webhook_secret)
        finally:
            os.close(lock)

    def _write_locked(
        self, app_id: int, private_key: bytes, webhook_secret: bytes | None
    ) -> dict[str, Any]:
        # The webhook secret stands apart from the pair and goes first, so a failure
        # after the switch below can only be the tidying that follows it.
        if webhook_secret and self.webhook_path is not None:
            _write_private(self.webhook_path, webhook_secret)
        previous = self._version()
        try:
            self.versions.mkdir(mode=0o700, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix="v-", dir=self.versions))
        except OSError as exc:
            raise GitHubAppStoreError(
                f"{self.versions} could not be prepared ({type(exc).__name__})"
            ) from None
        try:
            _write_private(stage / APP_ID, str(app_id).encode("ascii"))
            _write_private(stage / PRIVATE_KEY, private_key)
            _link(self.current, stage.relative_to(self.directory))
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        # The new pair is in force from here, so nothing after this point is a refusal:
        # the caller has recorded the change and must not roll that record back.
        done: dict[str, Any] = {"store": "directory", "path": str(self.directory), "created": False}
        try:
            _link(self.app_id_path, Path(CURRENT) / APP_ID)
            _link(self.key_path, Path(CURRENT) / PRIVATE_KEY)
        except GitHubAppStoreError as exc:
            done["warning"] = (
                f"the new App credential is in force, but {exc}; anything that reads "
                f"{self.key_path} directly still sees the previous key"
            )
        keep = {stage.name, previous.name if previous is not None else ""}
        with contextlib.suppress(OSError):
            for old in self.versions.iterdir():
                if old.name not in keep:
                    shutil.rmtree(old, ignore_errors=True)
        return done


def _link(target: Path, points_to: Path) -> None:
    """`target` made a symlink to `points_to` by one rename, so a reader finds the old
    link (or file) or the new link and never neither. Left alone when already so."""
    with contextlib.suppress(OSError):
        if os.readlink(target) == str(points_to):
            return
    temporary = target.with_name(f".{target.name}.incoming")
    try:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        os.symlink(points_to, temporary)
        os.replace(temporary, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise GitHubAppStoreError(
            f"{target} could not be switched ({type(exc).__name__})"
        ) from None


def _write_private(target: Path, value: bytes) -> None:
    """Mode 0600, written beside the target and renamed over it, so a reader sees the
    old file or the new one and never half of either."""
    temporary = target.with_name(f".{target.name}.incoming")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError as exc:
        raise GitHubAppStoreError(f"{target} could not be written ({type(exc).__name__})") from None
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
