"""Regression coverage for issue 485's admin request hot paths."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import time
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any, cast

from fastapi.routing import APIRoute

from crucible.adapters.api import routers as api_routers
from crucible.adapters.ui import actions, session
from crucible.adapters.ui import pages as ui_pages
from crucible.application.admin import status
from crucible.application.admin.context import ProviderStatusCache
from crucible.application.admin.harnesses import list_images
from crucible.application.admin.providers import providers_status
from crucible.domain.lifecycle import TaskState
from crucible.ports.execution import ImageInfo


def test_every_ui_and_api_handler_is_a_threadpool_entry_point() -> None:
    modules: list[ModuleType] = []
    for package in (ui_pages, api_routers):
        modules.extend(
            importlib.import_module(info.name)
            for info in pkgutil.iter_modules(package.__path__, f"{package.__name__}.")
        )
    modules.extend([actions, session])
    routes = [
        route
        for module in modules
        for route in getattr(getattr(module, "router", None), "routes", [])
        if isinstance(route, APIRoute)
    ]
    assert routes
    assert not [route.path for route in routes if inspect.iscoroutinefunction(route.endpoint)]


def test_fifty_tasks_per_state_use_grouped_and_bulk_queries() -> None:
    now = datetime(2026, 10, 6, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            id=f"{state.value}-{number}",
            external_id=f"FDY-{number}",
            principal_id="operator",
            updated_at=now,
            state=state,
            head_sha="head",
        )
        for state in TaskState
        for number in range(50)
    ]
    calls = {"counts": 0, "details": 0, "gates": 0, "events": 0}

    def counts(**_kwargs: Any) -> dict[TaskState, int]:
        calls["counts"] += 1
        return dict.fromkeys(TaskState, 50)

    def details(states: tuple[TaskState, ...], **_kwargs: Any) -> list[Any]:
        calls["details"] += 1
        wanted = set(states)
        return [row for row in rows if row.state in wanted]

    def gates(_task_ids: list[str]) -> list[Any]:
        calls["gates"] += 1
        return []

    def events(_task_ids: list[str], _kinds: tuple[str, ...]) -> dict[Any, Any]:
        calls["events"] += 1
        return {}

    uow = SimpleNamespace(
        tasks=SimpleNamespace(count_by_state=counts, list_in_states=details),
        gate_results=SimpleNamespace(list_for_tasks=gates),
        events=SimpleNamespace(latest_for_tasks_kinds=events),
    )
    started = time.perf_counter()
    document = status.tasks(uow)
    elapsed = time.perf_counter() - started

    assert document["counts"] == {state.value: 50 for state in TaskState}
    assert calls == {"counts": 1, "details": 1, "gates": 1, "events": 1}
    assert elapsed < 0.5


def test_page_reads_use_the_supervisor_snapshot() -> None:
    class Provider:
        async def list_images(self) -> list[Any]:
            raise AssertionError("page contacted registry")

        async def health(self) -> Any:
            raise AssertionError("page contacted provider")

    image = ImageInfo(reference="worker:test", digest="sha256:test", harnesses={})
    cached_provider = {"name": "fake", "health": "ok", "checks": {}}
    ctx = SimpleNamespace(
        providers={"fake": Provider()},
        status_cache_enabled=True,
        status_cache=ProviderStatusCache(
            images=[("fake", image)], providers=[cached_provider], refreshed_at=1.0
        ),
    )

    discovered = asyncio.run(list_images(cast(Any, ctx)))
    assert discovered[0][0] == "fake"
    assert discovered[0][1] is image
    assert asyncio.run(providers_status(cast(Any, ctx))) == [cached_provider]
