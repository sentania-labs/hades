"""Controlled local comparison: run with PYTHONPATH pointing at the checkout."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event, update
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.persistence import models as m
from crucible.adapters.ui.session import COOKIE, _serializer
from crucible.application.admin import status
from crucible.application.admin.harnesses import _concurrency
from crucible.domain.lifecycle import TaskState
from crucible.ports.execution import ImageInfo, ProviderHealth
from tests.unit.issue_485_fixture import workload


class Provider(FakeProvider):
    name = "kubernetes"

    async def list_images(self):
        await asyncio.sleep(0.02)
        return [
            ImageInfo(
                reference="worker:test", digest="sha256:test", harnesses={"script-harness": "1"}
            )
        ]

    async def health(self):
        await asyncio.sleep(0.02)
        return ProviderHealth(state="ok")


def run(count=500):
    with tempfile.TemporaryDirectory() as directory:
        app, ctx, engine = workload(Path(directory) / "bench.db", count)
        provider = Provider()
        ctx.providers = [provider]
        ctx.admin.providers = {"kubernetes": provider}
        if hasattr(ctx.admin, "status_cache_enabled"):
            ctx.admin.status_cache_enabled = True
            ctx.admin.status_cache.images = [
                ("kubernetes", image) for image in asyncio.run(provider.list_images())
            ]
            from crucible.application.admin.providers import (  # noqa: PLC0415
                refresh_providers_status,
            )

            ctx.admin.status_cache.providers = asyncio.run(refresh_providers_status(ctx.admin))
            ctx.admin.status_cache.refreshed_at = time.monotonic()
            if hasattr(ctx.admin, "status_cache_shared"):
                from crucible.application.admin import status_cache  # noqa: PLC0415

                ctx.admin.status_cache_shared = True
                with ctx.uow_factory() as uow:
                    status_cache.write(
                        ctx.admin,
                        uow,
                        ctx.admin.status_cache.images,
                        ctx.admin.status_cache.providers,
                    )
                    uow.commit()

        now = datetime.now(UTC)
        with Session(engine) as db:
            db.add(
                m.UiSessionRow(
                    id="bench",
                    principal_id="operator",
                    csrf="bench",
                    created_at=now,
                    last_seen_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            db.commit()
        statements = []
        lock = threading.Lock()

        def query(_conn, _cursor, statement, _params, _context, _many):
            with lock:
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", query)
        rows = []
        with TestClient(app) as client:
            client.cookies.set(COOKIE, _serializer(ctx).dumps("bench"))

            def get(path):
                started = time.perf_counter()
                response = client.get(path)
                assert response.status_code == 200, (
                    path,
                    response.status_code,
                    response.text[:300],
                )
                return round(time.perf_counter() - started, 4)

            # Warm templates and SQL compilation equally on both revisions.
            for path in ("/ui", "/ui/images", "/ui/harnesses", "/ui/tasks"):
                get(path)
            for path in ("/ui", "/ui/images", "/ui/harnesses", "/ui/tasks"):
                statements.clear()
                alone = get(path)
                alone_sql = len(statements)
                if count != 500 and path == "/ui":
                    assert alone < 5, alone
                    assert alone_sql < 100, alone_sql
                    assert sum("GROUP BY tasks.state" in sql for sql in statements) == 1
                    assert not any("WHERE tasks.id =" in sql for sql in statements)
                statements.clear()
                barrier = threading.Barrier(4)

                def concurrent(barrier=barrier, path=path):
                    barrier.wait()
                    return get(path)

                with ThreadPoolExecutor(max_workers=4) as pool:
                    times = list(pool.map(lambda _: concurrent(), range(4)))
                rows.append(
                    dict(
                        page=path,
                        alone=alone,
                        four=times,
                        queries_alone=alone_sql,
                        queries_four_total=len(statements),
                    )
                )
            if count != 500:
                settings_page = client.get("/ui/settings")
                assert settings_page.status_code == 200
                assert 'name="seconds"' in settings_page.text
                assert 'action="/ui/actions/status-cache"' in settings_page.text

                with ctx.uow_factory() as uow:
                    for query_service, expected in (
                        (status.workers, 1),
                        (_concurrency, 1),
                        (status.wakes, 1),
                        (status.tasks, 4),
                    ):
                        statements.clear()
                        query_service(uow)
                        assert len(statements) == expected, (query_service, statements)
                now = datetime.now(UTC)

                archived_ids = [f"task-{n:05}" for n in range(len(TaskState))]
                with Session(engine) as db:
                    db.add(
                        m.PrincipalRow(
                            id="archived",
                            name="discarded-import-bench",
                            role="observer",
                            token_salt=b"",
                            token_hash=b"",
                            created_at=now,
                            disabled_at=now,
                        )
                    )
                    db.execute(
                        update(m.TaskRow)
                        .where(m.TaskRow.id.in_(archived_ids))
                        .values(principal_id="archived")
                    )
                    db.add(
                        m.EventRow(
                            seq=1,
                            ts=now,
                            kind="task_publish_pending",
                            task_id=archived_ids[list(TaskState).index(TaskState.PUBLISHING)],
                            principal="supervisor",
                            verified=True,
                            payload={"reason": "fixture waiting", "waiting_since": now.isoformat()},
                        )
                    )
                    db.commit()
                hidden = client.get("/ui/tasks").text
                shown = client.get("/ui/tasks?archived=1").text
                assert f"{len(TaskState)} archived import tasks hidden" in hidden
                assert all(task_id not in hidden for task_id in archived_ids), [
                    task_id for task_id in archived_ids if task_id in hidden
                ]
                for state in ("awaiting_external_review", "awaiting_internal_review", "publishing"):
                    task_id = archived_ids[list(TaskState).index(TaskState(state))]
                    assert task_id in shown
        engine.dispose()
        return rows


if __name__ == "__main__":
    print(json.dumps(run(int(sys.argv[1]) if len(sys.argv) > 1 else 500), indent=2))
