"""The two places the first-run administrator token is delivered (ADR 0016).

On Kubernetes, a Secret in the service namespace that only the migrate Job's account
may create and only the service's account may delete. On Docker, a mode 0600 file in the
credential root, which only the service container mounts. Neither ever puts the value
in a log line, an exception message or a repr.
"""

from __future__ import annotations

import base64
import contextlib
import os
from pathlib import Path

from crucible.adapters.execution.k8sapi import KubernetesApiError, KubernetesClient

SECRET_NAME = "hades-first-run-admin"
SECRET_KEY = "token"
FILE_NAME = "first-run-admin-token"


class SecretDelivery:
    def __init__(self, client: KubernetesClient, name: str = SECRET_NAME) -> None:
        self.client = client
        self.name = name

    def where(self) -> str:
        namespace = self.client.namespace
        return (
            f"the Secret {namespace}/{self.name}, key {SECRET_KEY}: "
            f"kubectl -n {namespace} get secret {self.name} "
            f"-o jsonpath='{{.data.{SECRET_KEY}}}' | base64 -d"
        )

    def deliver(self, token: str) -> None:
        data = {SECRET_KEY: base64.b64encode(token.encode("utf-8")).decode("ascii")}
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": self.name,
                "labels": {
                    "app.kubernetes.io/name": "crucible",
                    "app.kubernetes.io/component": "first-run-admin",
                    "app.kubernetes.io/managed-by": "crucible",
                },
            },
            "data": data,
        }
        try:
            self.client.create("secrets", body)
        except KubernetesApiError as exc:
            if exc.status != 409:
                raise
            # A previous run's token whose principal the database no longer has (a
            # reset database): replace it whole.
            self.client.patch("secrets", self.name, {"data": data})

    def discard(self) -> None:
        self.client.delete("secrets", self.name)


class FileDelivery:
    def __init__(self, path: Path) -> None:
        self.path = path

    def where(self) -> str:
        return (
            f"the file {self.path} (mode 0600); under compose, read it with "
            f"docker compose exec crucible cat {self.path}"
        )

    def deliver(self, token: str) -> None:
        directory = self.path.parent
        temporary = directory / f".{self.path.name}.{os.getpid()}"
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(handle, f"{token}\n".encode())
            os.fsync(handle)
        finally:
            os.close(handle)
        os.replace(temporary, self.path)

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)
