"""Regression tests for the collection-path fixes in hades #392."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution import kubernetes as kubernetes_module
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from tests.unit.kubernetes_fixtures import build, spec

NOW = datetime(2026, 10, 2, tzinfo=UTC)


@dataclass
class DelayedReaderPods:
    """Keep deleted reader Pods around so their cleanup cannot complete."""

    api: FakeKubernetesApi
    role: str
    pending: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_delete = self.api.delete
        real_list = self.api.list_objects
        real_get = self.api.get

        def delayed_delete(kind: str, name: str, **kwargs: Any) -> None:
            kept = {
                key: obj
                for key, obj in self.api.objects.items()
                if key[0] == "pods"
                and obj.body.get("metadata", {}).get("labels", {}).get(k8sspec.LABEL_ROLE)
                == self.role
            }
            real_delete(kind, name, **kwargs)
            for key, obj in kept.items():
                obj.body.setdefault("spec", {})["terminationGracePeriodSeconds"] = 30
                obj.body["metadata"]["deletionTimestamp"] = NOW.isoformat()
                self.pending.setdefault(key[1], 1000)
                self.api.objects[key] = obj

        def delayed_list(kind: str, **kwargs: Any) -> list[dict[str, Any]]:
            rows = real_list(kind, **kwargs)
            if kind == "pods" and any(row["metadata"]["name"] in self.pending for row in rows):
                self.elapsed += 1
            return rows

        def delayed_get(kind: str, name: str) -> dict[str, Any]:
            if kind == "pods" and name in self.pending:
                self.elapsed += 1
            return real_get(kind, name)

        monkeypatch.setattr(self.api, "delete", delayed_delete)
        monkeypatch.setattr(self.api, "list_objects", delayed_list)
        monkeypatch.setattr(self.api, "get", delayed_get)
        monkeypatch.setattr(
            kubernetes_module, "time", SimpleNamespace(monotonic=lambda: self.elapsed)
        )


async def test_in_flight_role_error_is_not_replaced_by_lingering_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``failed`` guard preserves an in-flight error during Job cleanup.

    Without ``or failed`` in ``_run_role_job``, the finally block raises the
    lingering-Pod error instead of this OSError.
    """
    api, _registry, provider = build()
    launch = spec()
    delayed = DelayedReaderPods(api, k8sspec.ROLE_PREPARER)
    delayed.install(monkeypatch)
    monkeypatch.setattr(provider, "_await_job", AsyncMock(side_effect=OSError("read failed")))

    with pytest.raises(OSError, match="read failed"):
        await provider._run_role_job(
            launch,
            role=k8sspec.ROLE_PREPARER,
            image=launch.image,
            script="exit 0",
            mounts=(),
            volumes=(),
            limits=k8sspec.limits_from_policy({}),
            timeout=1,
            plan=provider._egress_plan(launch, k8sspec.ROLE_PREPARER),
        )

    assert delayed.pending


async def test_preparer_read_back_uses_non_collection_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preparer's read-back avoids the 35-second collection deletion wait.

    ``_read_files`` defaults ``collection`` to True. Removing ``collection=False``
    from ``prepare`` therefore makes this regression test fail.
    """
    api, _registry, provider = build()
    api.script_all("succeed", after=1)
    launch = spec()
    collections: list[bool] = []
    read_files = provider._read_files

    async def capture_collection(*args: Any, **kwargs: Any) -> dict[str, bytes]:
        collections.append(kwargs.get("collection", True))
        return await read_files(*args, **kwargs)

    monkeypatch.setattr(provider, "_read_files", capture_collection)

    workspace = await provider.prepare(launch)

    assert collections == [False]
    assert workspace.work_branch == "crucible/EX-0001"
