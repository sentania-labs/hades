from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from crucible.adapters.execution.k8sapi import (
    ClusterAccess,
    KubernetesClient,
    KubernetesUnavailableError,
    _exec_over_websocket,
)


class _Response:
    status = 101


class _Socket:
    def recv(self, _count: int) -> bytes:
        return b"\x88\x00"


class _Connection:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.sock = _Socket()

    def putrequest(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def putheader(self, key: str, value: str) -> None:
        self.headers[key] = value

    def endheaders(self) -> None:
        pass

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        pass


def test_exec_websocket_does_not_ask_for_a_json_representation() -> None:
    connection = _Connection()

    result = _exec_over_websocket(
        connection,  # type: ignore[arg-type]
        {"Accept": "application/json"},
        "https://127.0.0.1:6443",
        "/api/v1/namespaces/workers/pods/reader/exec",
        limit=1024,
    )

    assert connection.headers["Accept"] == "*/*"
    assert connection.headers["Sec-WebSocket-Protocol"] == "v4.channel.k8s.io"
    assert result.exit_code is None


class _ResetSocket:
    def recv(self, _count: int) -> bytes:
        raise ConnectionResetError(104, "Connection reset by peer")


def test_a_socket_reset_while_reading_exec_frames_is_an_unavailable_api_server() -> None:
    """PR 237 review: a reset after the upgrade, while frames are read, is the same
    unavailable API server as one before it, so collection retries it."""
    connection = _Connection()
    connection.sock = _ResetSocket()  # type: ignore[assignment]

    with pytest.raises(KubernetesUnavailableError):
        _exec_over_websocket(
            connection,  # type: ignore[arg-type]
            {},
            "https://127.0.0.1:6443",
            "/api/v1/namespaces/workers/pods/reader/exec",
            limit=1024,
        )


class _Body:
    def read(self) -> bytes:
        return b"2026-09-25T17:00:00.000000000Z hello\n"


def test_pod_log_sends_limit_bytes_and_since_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue 63: the bound is the API's own `limitBytes`, next to `sinceTime`."""
    client = KubernetesClient(ClusterAccess(server="https://127.0.0.1:6443"), "workers")
    seen: list[dict[str, Any]] = []

    @contextmanager
    def request(method: str, path: str, **kwargs: Any) -> Iterator[_Body]:
        seen.append({"method": method, "path": path, **kwargs})
        yield _Body()

    monkeypatch.setattr(client, "_request", request)
    frames = client.pod_log(
        "worker-1", container="crucible", since_time="2026-09-25T17:00:00Z", limit_bytes=4096
    )
    assert frames and frames[0].payload.startswith(b"2026-09-25T17:00:00")
    [call] = seen
    assert call["path"].endswith("/namespaces/workers/pods/worker-1/log")
    assert call["params"]["limitBytes"] == "4096"
    assert call["params"]["sinceTime"] == "2026-09-25T17:00:00Z"
    client.pod_log("worker-1")
    assert "limitBytes" not in seen[1]["params"]


def test_pod_log_replaces_non_utf8_bytes_at_the_kubernetes_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidBody:
        def read(self) -> bytes:
            return b"publisher byte: \xfc\n"

    @contextmanager
    def request(method: str, path: str, **kwargs: Any) -> Iterator[InvalidBody]:
        yield InvalidBody()

    client = KubernetesClient(ClusterAccess(server="https://127.0.0.1:6443"), "workers")
    monkeypatch.setattr(client, "_request", request)
    [frame] = client.pod_log("publisher-1")
    assert frame.payload.decode("utf-8") == "publisher byte: \ufffd\n"
