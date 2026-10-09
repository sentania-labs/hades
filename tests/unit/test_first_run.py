"""The first-run administrator token's two deliveries (crucible#122, ADR 0016)."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import pytest

from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.first_run import SECRET_NAME, FileDelivery, SecretDelivery
from crucible.application.first_run import discard_after_use, is_first_run
from crucible.cli import wiring
from crucible.settings import Settings

TOKEN = "cru_" + "0" * 26 + "." + "s" * 40


def test_the_secret_holds_the_token_and_names_only_where_it_is() -> None:
    api = FakeKubernetesApi(namespace="crucible")
    delivery = SecretDelivery(api)  # type: ignore[arg-type]

    delivery.deliver(TOKEN)

    secret = api.get("secrets", SECRET_NAME)
    assert base64.b64decode(secret["data"]["token"]).decode() == TOKEN
    assert secret["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "crucible"
    where = delivery.where()
    assert "crucible/hades-first-run-admin" in where and TOKEN not in where


def test_a_second_delivery_replaces_a_stale_secret_whole() -> None:
    api = FakeKubernetesApi(namespace="crucible")
    delivery = SecretDelivery(api)  # type: ignore[arg-type]
    delivery.deliver("cru_stale.value")
    delivery.deliver(TOKEN)
    secret = api.get("secrets", SECRET_NAME)
    assert base64.b64decode(secret["data"]["token"]).decode() == TOKEN


def test_discard_deletes_the_secret_and_tolerates_it_being_gone() -> None:
    api = FakeKubernetesApi(namespace="crucible")
    delivery = SecretDelivery(api)  # type: ignore[arg-type]
    delivery.deliver(TOKEN)
    delivery.discard()
    with pytest.raises(KubernetesApiError):
        api.get("secrets", SECRET_NAME)
    delivery.discard()


def test_the_file_is_private_and_replaced_atomically(tmp_path: Path) -> None:
    delivery = FileDelivery(tmp_path / "first-run-admin-token")
    delivery.deliver("cru_stale.value")
    delivery.deliver(TOKEN)
    assert delivery.path.read_text(encoding="utf-8") == TOKEN + "\n"
    assert os.stat(delivery.path).st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["first-run-admin-token"]
    assert "docker compose exec crucible cat" in delivery.where()
    assert str(delivery.path) in delivery.where()
    delivery.discard()
    delivery.discard()
    assert not delivery.path.exists()


def test_only_the_first_run_principal_discards(tmp_path: Path) -> None:
    delivery = FileDelivery(tmp_path / "first-run-admin-token")
    delivery.deliver(TOKEN)
    discard_after_use(delivery, "operator-admin")
    assert delivery.path.exists()
    discard_after_use(None, "first-run-admin")
    discard_after_use(delivery, "first-run-admin-1a2b3c4d")
    assert not delivery.path.exists()
    assert is_first_run("first-run-admin") and not is_first_run("admin-first-run-admin")


def test_a_failed_discard_is_logged_without_the_token(caplog: pytest.LogCaptureFixture) -> None:
    class Failing:
        def where(self) -> str:
            return "the Secret crucible/hades-first-run-admin"

        def deliver(self, token: str) -> None:
            raise AssertionError("never called")

        def discard(self) -> None:
            raise KubernetesApiError(403, "forbidden")

    with caplog.at_level(logging.WARNING):
        discard_after_use(Failing(), "first-run-admin")
    assert "could not be removed" in caplog.text and "cru_" not in caplog.text


def test_wiring_uses_configured_first_run_secret_for_create_reset_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUCIBLE_KUBERNETES__ENABLED", "true")
    monkeypatch.setenv("CRUCIBLE_KUBERNETES__FIRST_RUN_SECRET_NAME", "lab-bootstrap")
    settings = Settings()
    api = FakeKubernetesApi(namespace=settings.kubernetes.namespace)
    monkeypatch.setattr(wiring, "in_cluster_access", lambda: None)
    monkeypatch.setattr(wiring, "KubernetesClient", lambda *args, **kwargs: api)
    delivery = wiring.first_run_delivery(settings)
    assert isinstance(delivery, SecretDelivery)
    assert delivery.name == "lab-bootstrap"
    delivery.deliver("cru_stale.value")
    delivery.deliver(TOKEN)
    secret = api.get("secrets", "lab-bootstrap")
    assert base64.b64decode(secret["data"]["token"]).decode() == TOKEN
    assert "hades/lab-bootstrap" in delivery.where()
    discard_after_use(delivery, "first-run-admin")
    with pytest.raises(KubernetesApiError):
        api.get("secrets", "lab-bootstrap")
    with pytest.raises(KubernetesApiError):
        api.get("secrets", SECRET_NAME)
