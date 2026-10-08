"""The per-harness Test runs as a background job (issue 147).

The POST starts the run and answers at once with a running marker, the result lands on
the harness row's `last_test` as before, a second Test while one runs starts no
duplicate, the Harnesses row reads running and the page refreshes until the result
lands, and the CLI waits for the result. No database: a fake unit of work holds the
harness rows, and the six steps are stood in for by a run that waits on a gate.

On main, `harness_test` has no `start_test`, so this file fails at import.
"""

from __future__ import annotations

import argparse
import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from crucible.adapters.api.routers import admin as admin_router
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.ui.pages import harnesses as harnesses_page
from crucible.adapters.ui.render import templates
from crucible.application.admin import harness_test
from crucible.application.admin import harnesses as harnesses_admin
from crucible.application.admin import status as status_admin
from crucible.application.admin.context import AdminContext, BackgroundRuns
from crucible.cli import admin as cli_admin
from crucible.client.envelope import ClientError
from crucible.domain.entities import HarnessState, Principal, Role
from tests.wait import wait_until

NOW = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
HARNESS = "codex"


class Harnesses:
    """The harness rows, shared by every unit of work the factory hands out."""

    def __init__(self) -> None:
        self.rows: dict[str, HarnessState] = {}
        self.locked: list[str] = []

    def get(self, name: str, *, for_update: bool = False) -> HarnessState | None:
        if for_update:
            self.locked.append(name)
        return self.rows.get(name)

    def list_all(self) -> list[HarnessState]:
        return list(self.rows.values())

    def put(self, state: HarnessState) -> HarnessState:
        self.rows[state.name] = state
        return state


class Uow:
    def __init__(self, harnesses: Harnesses) -> None:
        self.harnesses = harnesses
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> Uow:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _context(store: Harnesses, clock: Clock | None = None) -> AdminContext:
    return AdminContext(
        uow_factory=lambda: Uow(store),  # type: ignore[arg-type]
        clock=clock or Clock(),
        providers={},
        harnesses=default_registry(),
    )


def _gated_run(gate: threading.Event, calls: list[str]) -> Any:
    """A stand-in for the six steps: it waits on the gate, then passes every step."""

    async def _run(
        ctx: Any,
        uow: Any,
        steps: Any,
        *,
        principal: str,
        harness: str,
        reason: str,
        credential_mode: Any = None,
    ) -> None:
        calls.append(harness)
        wait_until(gate.is_set, timeout=2.0, describe="gate to open")
        for step in harness_test.STEPS:
            steps.passed(step, "stood in for")

    return _run


def _ran(calls: list[str], timeout: float = 2.0) -> list[str]:
    """The runs started so far, once the thread has had time to enter the first one: the
    start no longer waits for its thread, which may not have reached the steps yet."""
    wait_until(lambda: bool(calls), timeout=timeout, describe="harness run to start")
    return calls


def _stored(store: Harnesses) -> dict[str, Any]:
    state = store.get(HARNESS)
    assert state is not None and isinstance(state.last_test, dict)
    return state.last_test


def _finished(store: Harnesses, ctx: AdminContext) -> dict[str, Any]:
    assert ctx.harness_tests.wait(HARNESS, timeout=5.0), "the background run did not end"
    return _stored(store)


# ----- the service ---------------------------------------------------------------


def test_a_start_answers_running_at_once_and_the_result_lands_on_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 and AC2: the start returns within a second with a running marker that is
    already the harness row's `last_test`; when the run ends, the result replaces it,
    with the six steps in order and the marker's start time."""
    store, gate = Harnesses(), threading.Event()
    calls: list[str] = []
    ctx = _context(store)
    monkeypatch.setattr(harness_test, "_run", _gated_run(gate, calls))

    before = time.monotonic()
    marker = harness_test.start_test(ctx, Uow(store), principal="admin", harness=HARNESS)  # type: ignore[arg-type]
    assert time.monotonic() - before < 1.0
    assert marker["status"] == harness_test.RUNNING
    assert marker["harness"] == HARNESS
    assert marker["ok"] is None and marker["failed_step"] is None and marker["steps"] == []
    assert marker["started_at"] == NOW.isoformat()
    assert marker["started_by"] == "admin"
    assert harness_test.is_running(marker)
    # The marker is on the row before the start answers, for every replica and the page.
    assert _stored(store)["status"] == harness_test.RUNNING
    assert harness_test.last_result(ctx, Uow(store), harness=HARNESS)["status"] == "running"  # type: ignore[arg-type]
    assert _ran(calls) == [HARNESS]

    gate.set()
    result = _finished(store, ctx)
    assert result["status"] == harness_test.FINISHED
    assert result["ok"] is True and result["failed_step"] is None
    assert [step["name"] for step in result["steps"]] == list(harness_test.STEPS)
    assert all(step["result"] == "pass" for step in result["steps"])
    assert result["started_at"] == marker["started_at"]
    assert result["tested_at"] == NOW.isoformat() and result["tested_by"] == "admin"
    assert harness_test.is_result_of(result, marker)
    assert not harness_test.is_running(result)
    assert harness_test.last_result(ctx, Uow(store), harness=HARNESS) == result  # type: ignore[arg-type]


def test_a_second_start_while_one_runs_returns_the_marker_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3, in this process: the second start hands back the first run's marker."""
    store, gate = Harnesses(), threading.Event()
    calls: list[str] = []
    ctx = _context(store)
    monkeypatch.setattr(harness_test, "_run", _gated_run(gate, calls))

    first = harness_test.start_test(ctx, Uow(store), principal="admin", harness=HARNESS)  # type: ignore[arg-type]
    ctx.clock.at = NOW + timedelta(seconds=30)  # type: ignore[attr-defined]
    second = harness_test.start_test(ctx, Uow(store), principal="other", harness=HARNESS)  # type: ignore[arg-type]
    assert second == first
    assert _ran(calls) == [HARNESS]

    gate.set()
    result = _finished(store, ctx)
    assert result["ok"] is True and result["tested_by"] == "admin"
    assert _ran(calls) == [HARNESS]


def test_a_fresh_running_marker_from_another_replica_is_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3, across replicas: a start that finds a fresh running marker on the row, with
    no run of its own, returns that marker and starts nothing. A marker old enough to be
    a run that died with its process is replaced by a new run."""
    store, gate = Harnesses(), threading.Event()
    calls: list[str] = []
    ctx = _context(store)
    monkeypatch.setattr(harness_test, "_run", _gated_run(gate, calls))
    elsewhere: dict[str, Any] = {
        "harness": HARNESS,
        "status": harness_test.RUNNING,
        "ok": None,
        "failed_step": None,
        "steps": [],
        "started_at": (NOW - timedelta(seconds=60)).isoformat(),
        "started_by": "replica-b",
    }
    store.put(
        HarnessState(
            name=HARNESS,
            enabled=True,
            reason="",
            session_compatibility="unverified",
            updated_at=NOW,
            updated_by="crucible",
            last_test=dict(elsewhere),
        )
    )

    joined = harness_test.start_test(ctx, Uow(store), principal="admin", harness=HARNESS)  # type: ignore[arg-type]
    assert joined == elsewhere
    assert calls == [] and ctx.harness_tests.running(HARNESS) is None

    ctx.clock.at = NOW + timedelta(seconds=harness_test.STALE_AFTER_SECONDS + 1)  # type: ignore[attr-defined]
    gate.set()
    replaced = harness_test.start_test(ctx, Uow(store), principal="admin", harness=HARNESS)  # type: ignore[arg-type]
    assert replaced["started_by"] == "admin" and replaced != elsewhere
    assert _ran(calls) == [HARNESS]
    assert _finished(store, ctx)["ok"] is True


def test_a_run_that_raises_fails_the_step_it_was_on_and_never_stays_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background run has nowhere to raise: the step it was on fails with the cause,
    the rest are not run, and the marker never stays running."""
    store = Harnesses()
    ctx = _context(store)

    async def exploding(
        ctx: Any,
        uow: Any,
        steps: Any,
        *,
        principal: str,
        harness: str,
        reason: str,
        credential_mode: Any = None,
    ) -> None:
        steps.passed(harness_test.ENABLED, "yes")
        steps.passed(harness_test.IMAGE, "yes")
        raise RuntimeError("the registry went away")

    monkeypatch.setattr(harness_test, "_run", exploding)
    harness_test.start_test(ctx, Uow(store), principal="admin", harness=HARNESS)  # type: ignore[arg-type]
    result = _finished(store, ctx)
    assert result["status"] == harness_test.FINISHED and result["ok"] is False
    assert result["failed_step"] == harness_test.CREDENTIAL
    assert [step["result"] for step in result["steps"]] == [
        "pass",
        "pass",
        "fail",
        "not run",
        "not run",
        "not run",
    ]
    assert "the registry went away" in result["steps"][2]["detail"]
    assert result["steps"][2]["title"] == "Check the service log for the codex test."


def test_the_foreground_run_the_cli_uses_locally_stores_the_same_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`test_harness` is still the run itself: local mode calls it and gets the result."""
    store, gate = Harnesses(), threading.Event()
    calls: list[str] = []
    gate.set()
    ctx = _context(store)
    monkeypatch.setattr(harness_test, "_run", _gated_run(gate, calls))
    uow = Uow(store)
    result = asyncio.run(
        harness_test.test_harness(ctx, uow, principal="cli", harness=HARNESS)  # type: ignore[arg-type]
    )
    assert result["status"] == harness_test.FINISHED and result["ok"] is True
    assert result["started_at"] == NOW.isoformat()
    assert _stored(store) == result


def test_not_tested_and_unknown_harnesses_are_reported_as_such() -> None:
    from crucible.application.errors import NotFoundError  # noqa: PLC0415

    store = Harnesses()
    ctx = _context(store)
    untested = harness_test.last_result(ctx, Uow(store), harness=HARNESS)  # type: ignore[arg-type]
    assert untested["status"] == harness_test.NOT_TESTED and untested["ok"] is None
    with pytest.raises(NotFoundError):
        harness_test.last_result(ctx, Uow(store), harness="no-such-harness")  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        harness_test.start_test(ctx, Uow(store), principal="admin", harness="no-such-harness")  # type: ignore[arg-type]


def test_background_runs_know_what_is_alive() -> None:
    runs = BackgroundRuns()
    gate = threading.Event()
    assert runs.running("x") is None and runs.wait("x") is True

    def hold() -> None:
        gate.wait()

    runs.start("x", {"status": "running"}, hold)
    assert runs.running("x") == {"status": "running"}
    assert runs.wait("x", timeout=0.05) is False
    gate.set()
    assert runs.wait("x", timeout=2.0) is True
    assert runs.running("x") is None


# ----- the API -------------------------------------------------------------------


def test_the_post_route_answers_accepted_with_the_marker_and_the_get_reads_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 over the route: `POST /admin/harnesses/{name}/test` is a 202 with the running
    marker, within a second; `GET /admin/harnesses/{name}/test` is the stored test."""
    store, gate = Harnesses(), threading.Event()
    calls: list[str] = []
    ctx = _context(store)
    monkeypatch.setattr(harness_test, "_run", _gated_run(gate, calls))
    app_ctx = SimpleNamespace(admin=ctx)
    principal = Principal("01K6H9ZH2J7F0X7M6C1Y8D3P4Q", "admin", Role.ADMIN, NOW)
    post = next(
        route
        for route in admin_router.router.routes
        if getattr(route, "path", "") == "/admin/harnesses/{name}/test"
        and "POST" in getattr(route, "methods", set())
    )
    assert post.status_code == 202  # type: ignore[attr-defined]

    before = time.monotonic()
    answer = admin_router.admin_test_harness(HARNESS, app_ctx, Uow(store), principal, None)  # type: ignore[arg-type]
    assert time.monotonic() - before < 1.0
    assert answer["status"] == harness_test.RUNNING
    again = admin_router.admin_test_harness(HARNESS, app_ctx, Uow(store), principal, {})  # type: ignore[arg-type]
    assert again == answer and _ran(calls) == [HARNESS]
    read = admin_router.admin_harness_test_result(HARNESS, app_ctx, Uow(store), principal)  # type: ignore[arg-type]
    assert read == answer

    gate.set()
    result = _finished(store, ctx)
    read = admin_router.admin_harness_test_result(HARNESS, app_ctx, Uow(store), principal)  # type: ignore[arg-type]
    assert read == result and read["ok"] is True


# ----- the CLI -------------------------------------------------------------------


class Remote:
    """A service whose POST answers the marker and whose GET lands the result later."""

    def __init__(self, answers: list[dict[str, Any]], post: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.post = post
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, body: Any = None, **_kwargs: Any) -> Any:
        self.calls.append((method, path))
        if method == "POST":
            return dict(self.post)
        return dict(self.answers.pop(0)) if len(self.answers) > 1 else dict(self.answers[0])


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        command="harnesses", harness_command="test", name=HARNESS, reason=None
    )


def test_the_cli_waits_for_the_result_and_prints_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: remote mode starts the test, polls the stored test until the result of that
    run lands, and returns the result, so the envelope prints pass or fail as before."""
    marker: dict[str, Any] = {
        "harness": HARNESS,
        "status": harness_test.RUNNING,
        "ok": None,
        "failed_step": None,
        "steps": [],
        "started_at": NOW.isoformat(),
        "started_by": "admin",
    }
    stale = {
        **marker,
        "status": harness_test.FINISHED,
        "ok": False,
        "started_at": "2026-10-07T13:00:00+00:00",
    }
    result = {**marker, "status": harness_test.FINISHED, "ok": True, "tested_at": NOW.isoformat()}
    remote = Remote([stale, marker, result], marker)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    printed = cli_admin._remote(_args(), remote)  # type: ignore[arg-type]
    assert printed == result
    assert remote.calls[0] == ("POST", f"/v1/admin/harnesses/{HARNESS}/test")
    assert remote.calls[1:] == [("GET", f"/v1/admin/harnesses/{HARNESS}/test")] * 3


def test_the_cli_prints_a_result_a_service_answers_at_once_and_gives_up_on_a_lost_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = {"harness": HARNESS, "ok": True, "failed_step": None, "steps": []}
    direct = Remote([], finished)
    assert cli_admin._remote(_args(), direct) == finished  # type: ignore[arg-type]
    assert direct.calls == [("POST", f"/v1/admin/harnesses/{HARNESS}/test")]

    marker = {"harness": HARNESS, "status": harness_test.RUNNING, "started_at": NOW.isoformat()}
    lost = Remote([marker], marker)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cli_admin, "HARNESS_TEST_WAIT_SECONDS", 0.2)
    with pytest.raises(ClientError, match="did not finish"):
        cli_admin._remote(_args(), lost)  # type: ignore[arg-type]


# ----- the Harnesses page --------------------------------------------------------


def test_the_row_reads_running_and_the_page_refreshes_until_the_result_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The running harness's Last test cell is a running badge with no Test button, the
    page asks the browser to reload itself, and a finished row is shown as before."""
    marker: dict[str, Any] = {
        "harness": HARNESS,
        "status": harness_test.RUNNING,
        "ok": None,
        "failed_step": None,
        "steps": [],
        "started_at": NOW.isoformat(),
        "started_by": "admin",
    }
    cell = harnesses_page._test_cell(marker)
    assert cell["kind"] == "status" and cell["value"] == "running"
    assert "refreshes" in cell["hint"]

    items = [
        {
            "name": HARNESS,
            "enabled": True,
            "enabled_by_configuration": True,
            "enabled_by_administrator": True,
            "reason": "",
            "supported_versions": "0.156",
            "credential": {"state": "validated"},
            "default_image": None,
            "last_test": marker,
        },
        {
            "name": "hermes",
            "enabled": True,
            "enabled_by_configuration": True,
            "enabled_by_administrator": True,
            "reason": "",
            "supported_versions": "0.19",
            "credential": {"state": "absent"},
            "default_image": None,
            "last_test": None,
        },
    ]

    async def no_images(_admin: Any) -> list[Any]:
        return []

    async def read(
        _admin: Any, _uow: Any, _found: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return items, {}

    async def no_providers(_admin: Any) -> list[Any]:
        return []

    principal = Principal("01K6H9ZH2J7F0X7M6C1Y8D3P4Q", "admin", Role.ADMIN, NOW)
    monkeypatch.setattr(harnesses_admin, "list_images", no_images)
    monkeypatch.setattr(harnesses_admin, "read_harnesses", read)
    monkeypatch.setattr(harnesses_page, "providers_status", no_providers)
    monkeypatch.setattr(status_admin, "harness_readiness", lambda *_a, **_k: [])
    monkeypatch.setattr(harnesses_page, "_require", lambda *_a, **_k: (principal, "csrf"))
    captured: dict[str, Any] = {}

    def fake_page(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(harnesses_page, "_page", fake_page)
    request = Request({"type": "http", "method": "GET", "path": "/ui/harnesses", "headers": []})
    ctx = SimpleNamespace(admin=_context(Harnesses()))
    asyncio.run(harnesses_page.harness_page(request, ctx, Uow(Harnesses())))  # type: ignore[arg-type]

    assert captured["refresh_seconds"] == harnesses_page.RUNNING_REFRESH_SECONDS
    rows = captured["sections"][0]["rows"]
    running_row, idle_row = rows
    assert running_row[4]["value"] == "running"
    assert running_row[5] == "" or all(
        action["label"] != "Test" for action in running_row[5]["items"]
    )
    assert any(action["label"] == "Test" for action in idle_row[5]["items"])

    rendered = templates.get_template("page.html").render(
        request=request,
        title="Harnesses",
        active="/ui/harnesses",
        nav=(),
        heading="Harnesses",
        intro="",
        sections=captured["sections"],
        csrf="csrf",
        hidden=frozenset(),
        principal=principal,
        message=None,
        refresh_seconds=captured["refresh_seconds"],
    )
    assert '<meta http-equiv="refresh" content="5;url=/ui/harnesses">' in rendered
    assert "running" in rendered

    idle = templates.get_template("page.html").render(
        request=request,
        title="Harnesses",
        active="/ui/harnesses",
        nav=(),
        heading="Harnesses",
        intro="",
        sections=[],
        csrf="csrf",
        hidden=frozenset(),
        principal=principal,
        message=None,
        refresh_seconds=None,
    )
    assert "http-equiv" not in idle


def test_the_test_action_starts_the_run_and_redirects_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, gate = Harnesses(), threading.Event()
    calls: list[str] = []
    ctx: Any = SimpleNamespace(admin=_context(store))
    uow: Any = Uow(store)
    monkeypatch.setattr(harness_test, "_run", _gated_run(gate, calls))
    principal = Principal("01K6H9ZH2J7F0X7M6C1Y8D3P4Q", "admin", Role.ADMIN, NOW)
    request = Request(
        {"type": "http", "method": "POST", "path": "/ui/actions/harness-test", "headers": []}
    )
    form = {"harness": HARNESS, "return_to": "/ui/harnesses"}

    before = time.monotonic()
    response = asyncio.run(
        harnesses_page._action_harness_test(
            request,
            "harness-test",
            ctx,
            uow,
            principal,
            "csrf",
            form,
            None,
        )
    )
    assert time.monotonic() - before < 1.0
    assert response is not None and response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/ui/harnesses?kind=info&message=")
    assert "running" in location
    assert _ran(calls) == [HARNESS] and _stored(store)["status"] == harness_test.RUNNING
    gate.set()
    assert _finished(store, ctx.admin)["ok"] is True


# ----- the review corrections ----------------------------------------------------


def test_the_claim_locks_the_row_and_another_replica_joins_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 across replicas: the marker is claimed under the harness row's lock and
    committed before the thread starts, so a second replica (its own AdminContext and
    thread registry) that starts right after finds it and launches nothing."""
    store, gate = Harnesses(), threading.Event()
    calls: list[str] = []
    replica_a, replica_b = _context(store), _context(store)
    monkeypatch.setattr(harness_test, "_run", _gated_run(gate, calls))

    first = harness_test.start_test(replica_a, Uow(store), principal="admin", harness=HARNESS)  # type: ignore[arg-type]
    assert store.locked == [HARNESS]
    second = harness_test.start_test(replica_b, Uow(store), principal="other", harness=HARNESS)  # type: ignore[arg-type]
    assert second == first
    assert replica_b.harness_tests.running(HARNESS) is None
    gate.set()
    assert _finished(store, replica_a)["ok"] is True
    assert calls == [HARNESS]


def test_the_start_never_waits_for_its_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: a thread that is slow to do anything does not hold the start; the marker is
    already stored by the start itself."""
    store, gate = Harnesses(), threading.Event()
    ctx = _context(store)

    async def slow(ctx: Any, marker: Any, **_kwargs: Any) -> None:
        wait_until(gate.is_set, timeout=5.0, describe="gate to open")

    monkeypatch.setattr(harness_test, "_background", slow)
    before = time.monotonic()
    marker = harness_test.start_test(ctx, Uow(store), principal="admin", harness=HARNESS)  # type: ignore[arg-type]
    assert time.monotonic() - before < 0.5
    assert _stored(store) == marker
    gate.set()
    assert ctx.harness_tests.wait(HARNESS, timeout=5.0)


def test_a_result_that_cannot_be_stored_leaves_a_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background run that raises outside the steps (the result's own transaction)
    replaces its marker with a failed result, never a marker that stays running."""
    store = Harnesses()
    ctx = _context(store)

    async def broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(harness_test, "test_harness", broken)
    marker = harness_test.start_test(ctx, Uow(store), principal="admin", harness=HARNESS)  # type: ignore[arg-type]
    result = _finished(store, ctx)
    assert result["status"] == harness_test.FINISHED and result["ok"] is False
    assert result["started_at"] == marker["started_at"]
    assert "the database went away" in result["steps"][0]["detail"]
    assert not harness_test.is_running(result)


def test_a_stale_marker_offers_test_again_and_stops_refreshing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker past STALE_AFTER_SECONDS is a lost run: the row says no result landed,
    offers Test, and the page stops reloading; the Test it offers replaces the marker."""
    lost: dict[str, Any] = {
        "harness": HARNESS,
        "status": harness_test.RUNNING,
        "ok": None,
        "failed_step": None,
        "steps": [],
        "started_at": (NOW - timedelta(seconds=harness_test.STALE_AFTER_SECONDS + 1)).isoformat(),
        "started_by": "admin",
    }
    assert harness_test.is_stale(lost, NOW)
    assert not harness_test.is_stale({**lost, "started_at": NOW.isoformat()}, NOW)
    assert not harness_test.is_stale(None, NOW)
    items = [
        {
            "name": HARNESS,
            "enabled": True,
            "enabled_by_configuration": True,
            "enabled_by_administrator": True,
            "reason": "",
            "supported_versions": "0.156",
            "credential": {"state": "validated"},
            "default_image": None,
            "last_test": lost,
        }
    ]

    async def no_images(_admin: Any) -> list[Any]:
        return []

    async def read(
        _admin: Any, _uow: Any, _found: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return items, {}

    async def no_providers(_admin: Any) -> list[Any]:
        return []

    principal = Principal("01K6H9ZH2J7F0X7M6C1Y8D3P4Q", "admin", Role.ADMIN, NOW)
    monkeypatch.setattr(harnesses_admin, "list_images", no_images)
    monkeypatch.setattr(harnesses_admin, "read_harnesses", read)
    monkeypatch.setattr(harnesses_page, "providers_status", no_providers)
    monkeypatch.setattr(status_admin, "harness_readiness", lambda *_a, **_k: [])
    monkeypatch.setattr(harnesses_page, "_require", lambda *_a, **_k: (principal, "csrf"))
    captured: dict[str, Any] = {}

    def fake_page(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(harnesses_page, "_page", fake_page)
    request = Request({"type": "http", "method": "GET", "path": "/ui/harnesses", "headers": []})
    ctx = SimpleNamespace(admin=_context(Harnesses()))
    asyncio.run(harnesses_page.harness_page(request, ctx, Uow(Harnesses())))  # type: ignore[arg-type]

    assert captured["refresh_seconds"] is None
    (row,) = captured["sections"][0]["rows"]
    assert row[4]["value"] == "no result" and "Test again" in row[4]["hint"]
    assert any(action["label"] == "Test" for action in row[5]["items"])
