"""Main went red at 44b8a9f (#585): on Postgres `/ui/tasks/{id}` became the card page,
and the card page dropped the hades #425 Egress section, so the kind tier's
`test_hades_425_a_worker_reaches_an_allowlisted_host_and_the_probe_records_it` found no
"Egress" on the task page. The card page now carries the probe rows the task page did."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import app_context, unit_of_work
from crucible.adapters.ui.pages import board as board_page
from crucible.adapters.ui.pages import tasks as tasks_page
from tests.fixtures import FakeClock
from tests.unit.test_issue_360_ready_for_merge_correction import NOW
from tests.unit.test_issue_489_card_actions import (
    OPERATOR,
    TASK_ID,
    CardStore,
    stuck_fixture,
)

PROBE = {
    "hosts": [
        {"host": "github.com", "reachable": True, "curl_exit": 0, "ms": 120},
        {"host": "pypi.org", "reachable": False, "curl_exit": 28, "detail": "Timeout"},
    ],
    "recorded_at": NOW.isoformat(),
}


def _probed_store() -> CardStore:
    store = stuck_fixture()
    attempt = store.attempts.get("01ATTEMPT48900000000000002")
    assert attempt is not None
    attempt.egress_probe = PROBE
    return store


def _task_page_on_postgres(store: CardStore, monkeypatch: pytest.MonkeyPatch) -> str:
    """The kind tier's route: `/ui/tasks/{id}` on a Postgres-backed context."""
    app = FastAPI()
    app.include_router(tasks_page.router)
    ctx = SimpleNamespace(
        clock=FakeClock(NOW),
        providers=[SimpleNamespace(name="fake")],
        database_url="postgresql+psycopg://kind/crucible",
    )
    app.state.ctx = ctx
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: store
    monkeypatch.setattr(board_page, "_require", lambda request, ctx, uow: (OPERATOR, "tok"))
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(f"/ui/tasks/{TASK_ID}")
    assert response.status_code == 200, response.text
    return str(response.text)


def test_the_task_page_on_postgres_shows_the_egress_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _task_page_on_postgres(_probed_store(), monkeypatch)
    assert 'aria-label="Egress"' in html
    assert "before the harness started" in html
    assert "github.com" in html and "reachable" in html
    assert "pypi.org" in html and "unreachable (curl 28: Timeout)" in html


def test_the_card_page_has_no_egress_section_before_a_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _task_page_on_postgres(stuck_fixture(), monkeypatch)
    assert 'aria-label="Egress"' not in html


def test_the_egress_rows_name_each_probed_host() -> None:
    view = SimpleNamespace(
        executions=[SimpleNamespace(attempts=[SimpleNamespace(id="A1", egress_probe=PROBE)])]
    )
    rows = board_page.egress_rows(view)
    assert len(rows) == 2
    assert rows[0]["attempt_id"] == "A1"
    assert rows[0]["host"] == "github.com"
    assert rows[0]["result"] == "reachable"
    assert rows[0]["ms"] == 120
    assert rows[0]["detail"] == ""
    assert rows[0]["recorded_at"] is not None
    assert rows[1]["host"] == "pypi.org"
    assert rows[1]["result"] == "unreachable (curl 28: Timeout)"
    assert rows[1]["ms"] is None
    assert rows[1]["detail"] == "Timeout"
