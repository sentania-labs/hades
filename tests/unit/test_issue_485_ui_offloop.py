"""Regression coverage for issue 485's admin request hot paths."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import pkgutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

from crucible.adapters.api import routers as api_routers
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.threaded_router import BufferedRequestRoute, ThreadedAPIRouter
from crucible.adapters.ui import actions, session
from crucible.adapters.ui import pages as ui_pages
from crucible.application.admin import status, status_cache
from crucible.application.admin.context import ProviderStatusCache
from crucible.application.admin.harnesses import list_images
from crucible.application.admin.providers import providers_status
from crucible.application.errors import ContractValidationError
from crucible.application.supervisor import Supervisor
from crucible.cli.wiring import wire
from crucible.domain.lifecycle import TaskState
from crucible.ports.execution import ImageInfo
from crucible.settings import Settings
from tests.wait import async_wait_until


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
    assert all(isinstance(route, BufferedRequestRoute) for route in routes)
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


def test_streamed_body_is_received_on_server_loop() -> None:

    async def exercise() -> None:
        server_loop = asyncio.get_running_loop()
        router = ThreadedAPIRouter()

        @router.post("/upload")
        async def upload(request: Request) -> dict[str, int]:
            assert asyncio.get_running_loop() is not server_loop
            chunks = [chunk async for chunk in request.stream()]
            return {"length": len(b"".join(chunks))}

        app = FastAPI()
        app.include_router(router)
        remaining = 16
        messages: list[Any] = []

        async def receive() -> dict[str, Any]:
            nonlocal remaining
            assert asyncio.get_running_loop() is server_loop
            await async_wait_until(
                lambda: remaining > 0, timeout=5.0, describe="to still have chunks"
            )
            remaining -= 1
            return {
                "type": "http.request",
                "body": b"x" * 65536,
                "more_body": remaining > 0,
            }

        async def send(message: Any) -> None:
            messages.append(message)

        await asyncio.wait_for(
            app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/upload",
                    "query_string": b"",
                    "headers": [],
                    "http_version": "1.1",
                },
                receive,
                send,
            ),
            timeout=5,
        )
        assert remaining == 0
        assert messages[0]["status"] == 200
        assert messages[1]["body"] == b'{"length":1048576}'

    asyncio.run(exercise())


def test_local_admin_discovers_images_and_provider_health(tmp_path: Any) -> None:

    composed = wire(
        Settings(
            database={"url": "sqlite://"},
            service={"artifact_root": str(tmp_path)},
            test_fixtures=True,
        ),
        role="admin",
    )
    try:
        ctx = composed.ctx.admin
        assert ctx is not None
        assert not ctx.status_cache_enabled

        provider = ctx.providers["fake"]
        assert isinstance(provider, FakeProvider)
        provider.images = [ImageInfo(reference="worker:test", digest="sha256:test", harnesses={})]
        assert asyncio.run(list_images(ctx))
        assert asyncio.run(providers_status(ctx))[0]["name"] == "fake"
    finally:
        composed.ctx.engine.dispose()


def test_board_with_fifty_tasks_per_state_and_archived_sections() -> None:
    # Keep SQLite type adaptations isolated from other repository tests.

    result = subprocess.run(
        [sys.executable, "tools/benchmarks/issue_485.py", str(50 * len(TaskState))],
        env={**os.environ, "PYTHONPATH": "."},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_supervisor_refresh_uses_saved_ttl_and_shares_snapshot() -> None:
    from_context: dict[str, Any] = {}

    class Uow:
        provider_settings = SimpleNamespace(
            get=from_context.get,
            put=lambda row: from_context.__setitem__(row.name, row),
        )

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def commit(self) -> None:
            pass

    provider = FakeProvider()
    provider.images = [ImageInfo(reference="worker:test", digest="sha256:test", harnesses={})]
    ctx = SimpleNamespace(
        status_cache=ProviderStatusCache(),
        status_cache_shared=True,
        status_cache_enabled=True,
        status_cache_ttl_seconds=60,
        uow_factory=Uow,
        providers={"fake": provider},
        clock=SimpleNamespace(now=lambda: datetime.now(UTC)),
    )

    async def db(call: Any) -> Any:
        return call()

    supervisor = SimpleNamespace(_admin_context=ctx, _uow_factory=Uow, _fenced=Uow, _db=db)
    asyncio.run(Supervisor._refresh_admin_status(cast(Any, supervisor)))
    first = ctx.status_cache.refreshed_at
    assert first is not None
    # A fresh API context reads the persisted snapshot without a supervisor of its own.
    api = SimpleNamespace(**{**vars(ctx), "status_cache": ProviderStatusCache()})
    assert asyncio.run(list_images(cast(Any, api))) == [("fake", provider.images[0])]
    assert asyncio.run(providers_status(cast(Any, api)))[0]["health"] == "ok"
    asyncio.run(Supervisor._refresh_admin_status(cast(Any, supervisor)))
    assert ctx.status_cache.refreshed_at == first
    from_context[status_cache.TTL] = SimpleNamespace(document={"ttl_seconds": 1}, reason="fixture")
    ctx.status_cache.refreshed_at = time.monotonic() - 2
    provider.images = []
    asyncio.run(Supervisor._refresh_admin_status(cast(Any, supervisor)))
    assert asyncio.run(list_images(cast(Any, api))) == []
    assert ctx.status_cache.refreshed_at > first


def test_cache_ttl_save_is_validated_and_audited() -> None:
    rows: dict[str, Any] = {}
    events: list[Any] = []

    def put(row: Any) -> Any:
        rows[row.name] = row
        return row

    uow = SimpleNamespace(
        provider_settings=SimpleNamespace(get=rows.get, put=put),
        events=SimpleNamespace(append=events.append),
    )
    ctx = cast(
        Any,
        SimpleNamespace(
            status_cache_ttl_seconds=60,
            clock=SimpleNamespace(now=lambda: datetime.now(UTC)),
        ),
    )
    principal = cast(Any, SimpleNamespace(name="operator"))
    assert status_cache.ttl_value(ctx, uow).value == 60
    status_cache.save_ttl(ctx, uow, principal=principal, seconds=15, reason=None)
    saved = status_cache.ttl_value(ctx, uow)
    assert (saved.value, saved.source, saved.applies) == (15, "saved", "next tick")
    assert events[-1].kind == "status_cache_updated"
    for invalid in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ContractValidationError):
            status_cache.save_ttl(ctx, uow, principal=principal, seconds=invalid, reason=None)
    assert status_cache.ttl_value(ctx, uow).value == 15
