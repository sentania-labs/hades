"""A small Kubernetes API client for the one namespace the provider owns (26).

The Docker provider talks to a socket proxy that narrows the API surface for it. There
is no proxy on the cluster: the narrowing is the supervisor's ServiceAccount, bound to a
Role in the workers namespace and nothing else (26). This client is the second half of
that discipline. It knows the eight resource kinds 26 names, `pods/log`, and
`pods/exec`, and it has no call for anything cluster-scoped, no `create` on a
Deployment, and no way to name a namespace other than the one it was built with.

Blocking, like the Docker client: the provider calls it from a thread. One connection
per request.

`exec` is here because bytes have to leave a Pod without passing through its log. The
kubelet writes pod logs to the node's disk, so a rotated credential read back through a
log would be a credential on a node (12). The exec stream is the same channel
`kubectl cp` uses and it is what the reader Pod hands the collected outputs and the
rotated auth files back on. It is also the one way a value goes into a Pod: the code an
operator pastes into a harness login (25) travels on the exec's stdin channel, never in
the exec's argv, which the API server can record in its audit log.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException, HTTPResponse, HTTPSConnection
from typing import IO, Any
from urllib.parse import quote, urlencode, urlsplit

import yaml

from crucible.ports.execution import ProviderError, ProviderUnavailableError

DEFAULT_TIMEOUT = 30.0
SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# The API groups each kind lives in. Nothing cluster-scoped is reachable from here.
_KINDS: dict[str, tuple[str, str]] = {
    "pods": ("", "v1"),
    "secrets": ("", "v1"),
    "configmaps": ("", "v1"),
    "persistentvolumeclaims": ("", "v1"),
    "resourcequotas": ("", "v1"),
    "events": ("", "v1"),
    "jobs": ("batch", "v1"),
    "networkpolicies": ("networking.k8s.io", "v1"),
}


# The answers that mean "the API server could not answer right now", not "the answer
# is no": throttling and the server-side errors a restart, an overloaded etcd or a
# load balancer in front of the API server produce.
UNAVAILABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class KubernetesApiError(ProviderError):
    """An API call failed. Carries the status so a caller can tell 404 from 409.

    It is a `ProviderError`, so one that escapes a provider method is the environment
    failure the port promises and never an exception the supervisor did not expect."""

    def __init__(self, status: int, message: str, *, path: str = "") -> None:
        super().__init__(f"{status} on {path}: {message}" if path else f"{status}: {message}")
        self.status = status
        self.message = message
        self.path = path


class KubernetesUnavailableError(KubernetesApiError, ProviderUnavailableError):
    """The API server could not be reached or could not answer: a refused, reset or
    timed-out connection (status 0), or one of `UNAVAILABLE_STATUSES`. The same call
    may succeed a moment later, so nothing is decided from it."""


def _api_error(status: int, message: str, *, path: str) -> KubernetesApiError:
    if status in UNAVAILABLE_STATUSES:
        return KubernetesUnavailableError(status, message, path=path)
    return KubernetesApiError(status, message, path=path)


# What a request that never got an answer raises: a refused or reset connection, a
# timeout (both are OSError), a TLS failure mid-stream, and a response http.client
# could not parse because the connection dropped partway.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (OSError, HTTPException, ssl.SSLError)


@dataclass(frozen=True, slots=True)
class ExecResult:
    """What one `pods/exec` produced: the two streams and the command's exit status.

    `exit_code` is None when the API server reported no status at all, which is a
    failed exec rather than a command that failed."""

    stdout: bytes
    stderr: bytes
    exit_code: int | None
    # How many stdout bytes the stream carried, counted past `limit` as well. When the
    # exec wrote stdout to a file (`pod_exec_to`) `stdout` is empty and this is the
    # only measure of it.
    stdout_size: int = 0


@dataclass(frozen=True, slots=True)
class LogFrame:
    """One pod log payload, shaped like the Docker client's frame so the shared resume
    logic (10) reads both without knowing which provider produced them."""

    stream: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class ClusterAccess:
    """Where the API server is and how this process authenticates to it.

    `token_path` rather than a token: an in-cluster ServiceAccount token is rotated by
    the kubelet, so it is read per request and never held (12's file-not-value rule
    applied to Crucible's own credential).

    A kubeconfig's inline `-data` material is held here as bytes and never written
    to a file that outlives the TLS handshake that needs it (crucible#65)."""

    server: str
    token_path: str | None = None
    token: str | None = field(default=None, repr=False)
    ca_cert_path: str | None = None
    client_cert_path: str | None = None
    client_key_path: str | None = None
    verify: bool = True
    ca_cert_data: bytes | None = field(default=None, repr=False)
    client_cert_data: bytes | None = field(default=None, repr=False)
    client_key_data: bytes | None = field(default=None, repr=False)

    def bearer(self) -> str | None:
        if self.token_path:
            try:
                return open(self.token_path, encoding="utf-8").read().strip()
            except OSError:
                return None
        return self.token


def in_cluster_access() -> ClusterAccess:
    """The ServiceAccount arrangement a Deployment in the `crucible` namespace gets."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise KubernetesApiError(0, "not running in a cluster: KUBERNETES_SERVICE_HOST is unset")
    return ClusterAccess(
        server=f"https://{host}:{port}",
        token_path=f"{SERVICE_ACCOUNT_DIR}/token",
        ca_cert_path=f"{SERVICE_ACCOUNT_DIR}/ca.crt",
    )


def kubeconfig_access(path: str, context: str | None = None) -> ClusterAccess:
    """The developer arrangement: a kubeconfig file, one context, no interactive auth.

    Only the static forms are read. An `exec` credential plugin would run a program this
    process chose from a file, which is not something a supervisor does unattended."""
    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise KubernetesApiError(0, f"kubeconfig {path!r} is not a mapping")
    wanted = context or str(document.get("current-context") or "")
    entry = _named(document.get("contexts"), wanted, "context")
    cluster = _named(document.get("clusters"), str(entry.get("cluster", "")), "cluster")
    user = _named(document.get("users"), str(entry.get("user", "")), "user")
    server = str(cluster.get("server", ""))
    if not server:
        raise KubernetesApiError(0, f"kubeconfig context {wanted!r} names no server")
    if "exec" in user or "auth-provider" in user:
        raise KubernetesApiError(
            0,
            f"kubeconfig user for context {wanted!r} needs a credential plugin; "
            "Crucible authenticates with a token or a client certificate only",
        )
    # A kubeconfig holds its certificates either as a path or inline as base64 under the
    # `-data` twin. kind writes the inline form, so a parser that reads only paths gets
    # the system CA and no client certificate, and cannot connect at all.
    return ClusterAccess(
        server=server,
        token=str(user["token"]) if user.get("token") else None,
        ca_cert_path=_path(cluster, "certificate-authority"),
        client_cert_path=_path(user, "client-certificate"),
        client_key_path=_path(user, "client-key"),
        verify=not bool(cluster.get("insecure-skip-tls-verify")),
        ca_cert_data=_inline(cluster, "certificate-authority"),
        client_cert_data=_inline(user, "client-certificate"),
        client_key_data=_inline(user, "client-key"),
    )


def _path(entry: Mapping[str, Any], key: str) -> str | None:
    path = entry.get(key)
    return str(path) if path else None


def _inline(entry: Mapping[str, Any], key: str) -> bytes | None:
    """The inline `<key>-data` form, decoded and kept in memory. A path, when the entry
    also names one, wins, as it does for kubectl."""
    if entry.get(key):
        return None
    raw = entry.get(f"{key}-data")
    if not raw:
        return None
    try:
        return base64.b64decode(str(raw))
    except ValueError as exc:
        raise KubernetesApiError(0, f"kubeconfig {key}-data is not base64") from exc


@contextmanager
def _transient_file(data: bytes) -> Iterator[str]:
    """A path `ssl` can read `data` from, gone when the block ends.

    `ssl` loads a client certificate and key from a path and nothing else. On Linux the
    path is an anonymous memory file (`memfd`), so the key never reaches a disk at all;
    elsewhere it is a mode 0600 file in a private directory, unlinked as soon as the
    context has loaded it. Nothing is kept for the life of the process (crucible#65)."""
    memfd_create = getattr(os, "memfd_create", None)
    if memfd_create is not None:
        handle = memfd_create("crucible-tls", 0)
        try:
            os.write(handle, data)
            yield f"/proc/self/fd/{handle}"
        finally:
            os.close(handle)
        return
    with tempfile.TemporaryDirectory(prefix="crucible-tls-") as directory:
        name = os.path.join(directory, "material.pem")
        handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(handle, data)
        finally:
            os.close(handle)
        try:
            yield name
        finally:
            os.unlink(name)


def _named(entries: Any, name: str, kind: str) -> dict[str, Any]:
    for entry in entries or []:
        if isinstance(entry, dict) and str(entry.get("name")) == name:
            body = entry.get(kind)
            return body if isinstance(body, dict) else {}
    raise KubernetesApiError(0, f"kubeconfig has no {kind} named {name!r}")


class KubernetesClient:
    """Namespaced calls only. The namespace is fixed at construction on purpose: 26
    gives the supervisor a Role in `hades-workers` and nothing anywhere else, and a
    client that cannot spell another namespace cannot drift past that."""

    def __init__(
        self, access: ClusterAccess, namespace: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.access = access
        self.namespace = namespace
        self.timeout = timeout

    # ----- transport ---------------------------------------------------

    def _context(self) -> ssl.SSLContext | None:
        parsed = urlsplit(self.access.server)
        if parsed.scheme != "https":
            return None
        access = self.access
        cadata = access.ca_cert_data.decode("ascii", "replace") if access.ca_cert_data else None
        context = ssl.create_default_context(cafile=access.ca_cert_path, cadata=cadata)
        if not access.verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if access.client_cert_path or access.client_cert_data:
            with ExitStack() as stack:
                cert = access.client_cert_path or stack.enter_context(
                    _transient_file(access.client_cert_data or b"")
                )
                key = access.client_key_path
                if key is None and access.client_key_data:
                    key = stack.enter_context(_transient_file(access.client_key_data))
                context.load_cert_chain(cert, key)
        return context

    def _connect(self, timeout: float | None = None) -> HTTPConnection:
        parsed = urlsplit(self.access.server)
        wait = self.timeout if timeout is None else timeout
        host = parsed.hostname or ""
        context = self._context()
        if context is None:
            return HTTPConnection(host, parsed.port or 80, timeout=wait)
        return HTTPSConnection(host, parsed.port or 443, timeout=wait, context=context)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        bearer = self.access.bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    @contextmanager
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        timeout: float | None = None,
    ) -> Iterator[HTTPResponse]:
        url = path
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = self._headers()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn = self._connect(timeout)
        try:
            try:
                conn.request(method, url, body=payload, headers=headers)
                response = conn.getresponse()
                if response.status >= 400:
                    raw = response.read().decode("utf-8", "replace")
                    raise _api_error(response.status, _message(raw), path=url)
            except _TRANSPORT_ERRORS as exc:
                raise _unreachable(exc, url) from exc
            yield response
        finally:
            conn.close()

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        with self._request(method, path, params=params, body=body, timeout=timeout) as response:
            try:
                raw = response.read()
            except _TRANSPORT_ERRORS as exc:
                raise _unreachable(exc, path) from exc
        return json.loads(raw.decode("utf-8")) if raw else None

    # ----- paths -------------------------------------------------------

    def _base(self, kind: str) -> str:
        try:
            group, version = _KINDS[kind]
        except KeyError:
            raise KubernetesApiError(0, f"the client has no call for {kind!r}") from None
        root = f"/api/{version}" if not group else f"/apis/{group}/{version}"
        return f"{root}/namespaces/{quote(self.namespace, safe='')}/{kind}"

    # ----- calls -------------------------------------------------------

    def version(self) -> str:
        data = self._json("GET", "/version")
        return str(data.get("gitVersion", "")) if isinstance(data, dict) else ""

    def create(self, kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
        data = self._json("POST", self._base(kind), body=body)
        assert isinstance(data, dict)
        return data

    def get(self, kind: str, name: str) -> dict[str, Any]:
        data = self._json("GET", f"{self._base(kind)}/{quote(name, safe='')}")
        assert isinstance(data, dict)
        return data

    def list_objects(
        self, kind: str, *, label_selector: str | None = None, field_selector: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if label_selector:
            params["labelSelector"] = label_selector
        if field_selector:
            params["fieldSelector"] = field_selector
        data = self._json("GET", self._base(kind), params=params)
        items = data.get("items") if isinstance(data, dict) else None
        return [row for row in (items or []) if isinstance(row, dict)]

    def delete(
        self,
        kind: str,
        name: str,
        *,
        grace_period_seconds: int | None = None,
        propagation: str = "Background",
        uid: str | None = None,
    ) -> None:
        """Delete one object. A 404 is the state delete was asked to produce.

        `propagation` is Background by default so deleting a Job takes its Pod with it;
        Orphan would leave a worker Pod running with nothing tracking it (26). `uid`
        deletes only that incarnation of the name: the API server answers 409 when the
        object there now is a different one (the login lock, 25)."""
        body: dict[str, Any] = {
            "apiVersion": "meta.k8s.io/v1",
            "kind": "DeleteOptions",
            "propagationPolicy": propagation,
        }
        if grace_period_seconds is not None:
            body["gracePeriodSeconds"] = grace_period_seconds
        if uid is not None:
            body["preconditions"] = {"uid": uid}
        try:
            self._json("DELETE", f"{self._base(kind)}/{quote(name, safe='')}", body=body)
        except KubernetesApiError as exc:
            if exc.status != 404:
                raise

    def patch(
        self,
        kind: str,
        name: str,
        body: Mapping[str, Any],
        *,
        resource_version: str | None = None,
    ) -> dict[str, Any]:
        """A JSON merge patch on one object. The only mutation this client makes to an
        object it did not create, and the only thing it patches is the harness
        credential Secret's data on a validated sync-back (12).

        ``resource_version`` is embedded in the patch body as
        ``metadata.resourceVersion`` so the API server returns 409 Conflict when
        the stored object has changed since the read (339).
        """
        url = f"{self._base(kind)}/{quote(name, safe='')}"
        headers = {**self._headers(), "Content-Type": "application/merge-patch+json"}
        patch_body: dict[str, Any] = dict(body)
        if resource_version is not None:
            patch_body.setdefault("metadata", {})["resourceVersion"] = resource_version
        conn = self._connect()
        try:
            conn.request("PATCH", url, body=json.dumps(patch_body).encode("utf-8"), headers=headers)
            response = conn.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise _api_error(
                    response.status, _message(raw.decode("utf-8", "replace")), path=url
                )
        except _TRANSPORT_ERRORS as exc:
            raise _unreachable(exc, url) from exc
        finally:
            conn.close()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        return data if isinstance(data, dict) else {}

    def pod_log(
        self,
        name: str,
        *,
        container: str | None = None,
        since_time: str | None = None,
        timestamps: bool = True,
        timeout: float | None = None,
        limit_bytes: int | None = None,
        tail_lines: int | None = None,
    ) -> list[LogFrame]:
        """`pods/log` with timestamps and an RFC 3339 `sinceTime` bound (26).

        `limit_bytes` is the API's `limitBytes` (issue 63): the kubelet stops after that
        many bytes of output, which may be in the middle of a line.

        Kubernetes merges stdout and stderr into one stream and does not say which a
        line came from, so every line is reported on stdout. The resume position (10)
        is a timestamp and a line hash, neither of which depends on the stream name.
        `sinceTime` has one-second granularity and is inclusive, which is exactly the
        overlap the strict-after resume already exists to drop."""
        params: dict[str, Any] = {"timestamps": "true" if timestamps else "false"}
        if container:
            params["container"] = container
        if since_time:
            params["sinceTime"] = since_time
        if limit_bytes is not None:
            params["limitBytes"] = str(limit_bytes)
        if tail_lines is not None:
            params["tailLines"] = str(tail_lines)
        path = f"{self._base('pods')}/{quote(name, safe='')}/log"
        try:
            with self._request("GET", path, params=params, timeout=timeout) as response:
                try:
                    raw = response.read()
                except _TRANSPORT_ERRORS as exc:
                    raise _unreachable(exc, path) from exc
        except KubernetesApiError as exc:
            if exc.status in (400, 404):
                # 400 is what a Pod that has not started a container answers.
                return []
            raise
        if not raw:
            return []
        # Hades #425: kubelet and containerd may return non-UTF-8 bytes in a Pod log.
        # Normalize at the one pods/log ingress so every text consumer is safe.
        safe = raw.decode("utf-8", "replace").encode("utf-8")
        return [LogFrame("stdout", safe)]

    def pod_exec(
        self,
        name: str,
        command: Sequence[str],
        *,
        container: str | None = None,
        timeout: float | None = None,
        limit: int = 64 * 1024 * 1024,
        stdin: bytes | None = None,
        stdout_to: IO[bytes] | None = None,
    ) -> ExecResult:
        """Run one command in a Pod and return both streams and its exit status.

        The WebSocket form of `pods/exec` (`v4.channel.k8s.io`), which is the transport
        `kubectl cp` uses. Stdin is opened only when `stdin` is given, and then the bytes
        are sent once on channel 0. The v4 protocol has no way to close stdin, so the
        command must stop reading on its own (a `read` of one line does)."""
        params: dict[str, Any] = {
            "stdout": "true",
            "stderr": "true",
            "stdin": "true" if stdin is not None else "false",
        }
        if container:
            params["container"] = container
        query = urlencode([*params.items(), *[("command", c) for c in command]])
        path = f"{self._base('pods')}/{quote(name, safe='')}/exec?{query}"
        return _exec_over_websocket(
            self._connect(timeout),
            self._headers(),
            self.access.server,
            path,
            limit=limit,
            stdin=stdin,
            stdout=stdout_to,
        )

    def pod_exec_to(
        self,
        name: str,
        command: Sequence[str],
        into: IO[bytes],
        *,
        container: str | None = None,
        timeout: float | None = None,
        limit: int = 64 * 1024 * 1024,
    ) -> ExecResult:
        """`pod_exec` with stdout written to `into` as it arrives rather than held in
        memory: the collected archive can be hundreds of megabytes, and the supervisor
        should not hold it once, let alone twice. At most `limit` bytes are written;
        `stdout_size` says how many the Pod sent."""
        return self.pod_exec(
            name, command, container=container, timeout=timeout, limit=limit, stdout_to=into
        )


# ----- the exec stream ---------------------------------------------------

_CHANNEL_STDIN = 0
_CHANNEL_STDOUT = 1
_CHANNEL_STDERR = 2
_CHANNEL_ERROR = 3
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _exec_over_websocket(
    conn: HTTPConnection,
    headers: Mapping[str, str],
    server: str,
    path: str,
    *,
    limit: int,
    stdin: bytes | None = None,
    stdout: IO[bytes] | None = None,
) -> ExecResult:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    parsed = urlsplit(server)
    request_headers = {
        **headers,
        # The ordinary API calls ask for JSON. An exec upgrade has no JSON
        # representation, and a real API server answers 406 when that Accept header
        # leaks into the WebSocket handshake.
        "Accept": "*/*",
        "Host": parsed.netloc,
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": key,
        "Sec-WebSocket-Protocol": "v4.channel.k8s.io",
    }
    try:
        try:
            conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            for header, value in request_headers.items():
                conn.putheader(header, value)
            conn.endheaders()
            response = conn.getresponse()
            if response.status != 101:
                raw = response.read().decode("utf-8", "replace")
                raise _api_error(response.status, _message(raw), path=path)
            sock = conn.sock
            if sock is None:
                raise KubernetesUnavailableError(0, "the exec upgrade carried no socket", path=path)
            if stdin is not None:
                sock.sendall(_client_frame(bytes([_CHANNEL_STDIN]) + stdin))
            # The socket can also reset or time out after the upgrade, while the frames
            # are read: that is the same unavailable API server, not a failed command.
            return _read_exec_channels(sock, limit=limit, stdout=stdout)
        except _TRANSPORT_ERRORS as exc:
            raise _unreachable(exc, path) from exc
    finally:
        conn.close()


def _client_frame(payload: bytes) -> bytes:
    """One final binary frame from a client, masked as RFC 6455 requires of a client."""
    length = len(payload)
    if length < 126:
        header = bytes([0x82, 0x80 | length])
    elif length < 1 << 16:
        header = bytes([0x82, 0x80 | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([0x82, 0x80 | 127]) + length.to_bytes(8, "big")
    mask = secrets.token_bytes(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return header + mask + masked


def _read_exec_channels(sock: Any, *, limit: int, stdout: IO[bytes] | None = None) -> ExecResult:
    streams: dict[int, bytearray] = {
        _CHANNEL_STDOUT: bytearray(),
        _CHANNEL_STDERR: bytearray(),
        _CHANNEL_ERROR: bytearray(),
    }
    total = 0
    stdout_size = 0
    for payload in _websocket_frames(sock):
        if not payload:
            continue
        channel, body = payload[0], payload[1:]
        buffer = streams.get(channel)
        if buffer is None or not body:
            continue
        # Bounded before anything is parsed: a Pod owns what it writes and the reader
        # asks for a file a worker could have replaced with anything (12).
        room = max(0, limit - total)
        if channel == _CHANNEL_STDOUT:
            stdout_size += len(body)
            if stdout is not None:
                stdout.write(body[:room])
            else:
                buffer.extend(body[:room])
        else:
            buffer.extend(body[:room])
        total += len(body)
        if total > limit:
            break
    return ExecResult(
        stdout=bytes(streams[_CHANNEL_STDOUT]),
        stderr=bytes(streams[_CHANNEL_STDERR]),
        exit_code=_exit_status(bytes(streams[_CHANNEL_ERROR])),
        stdout_size=stdout_size,
    )


def _exit_status(raw: bytes) -> int | None:
    """The error channel carries a metav1.Status: `Success`, or a non-zero exit code in
    its `details.causes`. No status at all is an exec that never ran the command."""
    if not raw:
        return None
    try:
        document = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    if str(document.get("status", "")) == "Success":
        return 0
    for cause in (document.get("details") or {}).get("causes") or []:
        if isinstance(cause, dict) and str(cause.get("reason")) == "ExitCode":
            try:
                return int(str(cause.get("message")))
            except ValueError:
                return None
    return None


def _websocket_frames(sock: Any) -> Iterator[bytes]:
    """RFC 6455 frames from a server, which are never masked. Continuations are joined;
    a close or an empty read ends the stream. A socket error is raised: a reset or a
    timeout mid-stream is an unavailable API server, not a finished command."""
    buffer = bytearray()
    pending = bytearray()

    def need(count: int) -> bool:
        while len(buffer) < count:
            chunk = sock.recv(65536)
            if not chunk:
                return False
            buffer.extend(chunk)
        return True

    while True:
        if not need(2):
            return
        first, second = buffer[0], buffer[1]
        opcode = first & 0x0F
        final = bool(first & 0x80)
        length = second & 0x7F
        offset = 2
        if length == 126:
            if not need(4):
                return
            length = int.from_bytes(buffer[2:4], "big")
            offset = 4
        elif length == 127:
            if not need(10):
                return
            length = int.from_bytes(buffer[2:10], "big")
            offset = 10
        if second & 0x80:
            # A masked frame from a server is a protocol violation; nothing is parsed.
            return
        if not need(offset + length):
            return
        payload = bytes(buffer[offset : offset + length])
        del buffer[: offset + length]
        if opcode == 0x8:
            return
        if opcode in (0x9, 0xA):
            continue
        pending.extend(payload)
        if final:
            yield bytes(pending)
            pending.clear()


def _unreachable(exc: BaseException, path: str) -> KubernetesUnavailableError:
    """A request that got no answer, as the one error type the provider handles."""
    what = "timed out" if isinstance(exc, TimeoutError) else "failed"
    return KubernetesUnavailableError(
        0, f"the API server connection {what}: {type(exc).__name__}: {exc}", path=path
    )


def _message(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw.strip()
    if isinstance(parsed, dict) and "message" in parsed:
        return str(parsed["message"])
    return raw.strip()
