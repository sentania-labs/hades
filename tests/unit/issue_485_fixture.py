"""SQLite workload for the controlled local issue 485 benchmark, not a lab replica."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session
from sqlalchemy.types import TypeDecorator

from crucible.adapters.api.app import create_app
from crucible.adapters.persistence import models as m
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory
from crucible.cli.wiring import wire
from crucible.domain.lifecycle import TaskState
from crucible.settings import Settings


class AwareTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def workload(path: Path, count: int = 500) -> tuple[Any, Any, Any]:
    # SQLite has neither JSONB nor arrays; keep production queries and row mappings.
    for table in m.Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSONB | ARRAY):
                column.type = JSON()
            elif isinstance(column.type, DateTime):
                column.type = AwareTime()
            if column.server_default is not None and "::" in str(
                getattr(column.server_default, "arg", "")
            ):
                column.server_default = None
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    m.Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add(
            m.PrincipalRow(
                id="operator",
                name="operator",
                role="admin",
                token_salt=b"",
                token_hash=b"",
                created_at=now,
            )
        )
        db.add(
            m.RepositoryRow(
                id="repo",
                name="repo",
                url="https://example.invalid/repo",
                default_branch="main",
                policy_name="default",
                registered_by="operator",
                created_at=now,
            )
        )
        for number in range(count):
            task_id = f"task-{number:05}"
            state = list(TaskState)[number % len(TaskState)]
            db.add(
                m.TaskRow(
                    id=task_id,
                    external_id=task_id,
                    principal_id="operator",
                    repository_id="repo",
                    project="bench",
                    title="Benchmark",
                    state=state.value,
                    contract_version=1,
                    policy_name="default",
                    policy_version=1,
                    created_at=now,
                    updated_at=now,
                    head_sha="head",
                )
            )
            db.add(
                m.ExecutionRow(
                    id=f"exec-{number}",
                    task_id=task_id,
                    role="implement",
                    contract_version=1,
                    harness="script-harness",
                    model="fake",
                    provider="fake",
                    image="worker:test",
                    policy_snapshot={},
                    state="active",
                    max_attempts=1,
                    retry_on=[],
                    timeout_seconds=60,
                    created_at=now,
                )
            )
            db.add(
                m.AttemptRow(
                    id=f"attempt-{number}",
                    execution_id=f"exec-{number}",
                    task_id=task_id,
                    number=1,
                    state="running",
                    created_at=now,
                )
            )
            db.add(
                m.GateResultRow(
                    id=f"gate-{number}",
                    task_id=task_id,
                    attempt_id=f"attempt-{number}",
                    head_sha="head",
                    gate="test",
                    phase="pre_pr",
                    result="passed",
                    detail="",
                    evidence_ids=[],
                    evaluated_at=now,
                    findings=[],
                )
            )
            db.add(
                m.WakeRow(
                    id=f"wake-{number}",
                    principal_id="operator",
                    task_id=task_id,
                    reason="state_changed",
                    payload={},
                    created_at=now,
                )
            )
        db.commit()
    settings = Settings(
        database={"url": f"sqlite:///{path}"},
        service={"artifact_root": str(path.parent / "artifacts")},
        test_fixtures=True,
    )
    composed = wire(settings, role="admin")
    composed.ctx.engine.dispose()
    composed.ctx.engine = engine
    composed.ctx.uow_factory = SqlUnitOfWorkFactory(engine)
    assert composed.ctx.admin is not None
    composed.ctx.admin.uow_factory = composed.ctx.uow_factory
    return create_app(composed.ctx), composed.ctx, engine
