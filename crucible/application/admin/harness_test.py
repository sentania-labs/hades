"""The harness test (crucible#118): does this harness work, end to end, right now?

It runs the path a real task takes, in order, and stops at the first step that fails:
the harness is enabled, it has a worker image of its own at a supported version (ADR
0018), its credential is stored, and a worker Pod (or container) runs that image with
the credential under the worker's egress and makes one minimal model call. Each step
is reported in plain words, pass or fail, with the failing step's cause. The last result
is kept on the harness's row for the Harnesses page.

A test is a check, not a change, so it asks for no reason (25, crucible#117). The worker
run is the bounded probe (25) with every harness in a worker, Hermes included, so it is
recorded as the probe is: a `credential_probed` event and the last launch outcome.

The run takes up to a couple of minutes, so the API and the Harnesses page start it as a
background job (issue 147): `start_test` stores a running marker as the harness's
`last_test`, returns it at once, and a thread of this process runs the six steps and
replaces the marker with the result. A second start while the marker says running returns
that marker and starts nothing. `test_harness` is the run itself, in the foreground, which
is what the thread and the CLI's local mode call."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, TypeGuard

from crucible.application.admin import credentials
from crucible.application.admin.context import AdminContext, guard_mutation
from crucible.application.errors import ApplicationError, NotFoundError
from crucible.application.harnesses import HarnessUnavailableError, harness_state
from crucible.domain.exit_class import ExitClass
from crucible.ports.repository import UnitOfWork

log = logging.getLogger(__name__)

HERMES = "hermes"
QWEN_CODE = "qwen_code"

# The `status` of a stored test (issue 147): the marker a start leaves, and the result.
RUNNING = "running"
FINISHED = "finished"
NOT_TESTED = "not tested"
# A running marker older than this, with no run in this process, was left by a process
# that died mid-test (an api restart); a start replaces it rather than waiting on it.
STALE_AFTER_SECONDS = 900
# How long a start waits for its thread to store the running marker, so the Harnesses
# page loaded right after the redirect already reads running.
MARKER_WAIT_SECONDS = 5.0

ENABLED = "Harness enabled"
IMAGE = "Worker image"
CREDENTIAL = "Credential"
ROUTE = "Model"
WORKER = "Worker starts"
MODEL = "Model call"
STEPS = (ENABLED, IMAGE, CREDENTIAL, ROUTE, WORKER, MODEL)


def _is_hermes_like(harness: str) -> bool:
    """True for the Hermes-like harnesses that use Local gateway."""
    return harness in {HERMES, QWEN_CODE}


def _route_title(harness: str, detail: str) -> str:
    """A plain, actionable sentence for a ROUTE step failure."""
    hermes = _is_hermes_like(harness)
    if hermes:
        return f"{harness} has no enabled model. Pick one on Local gateway."
    return f"{harness} has no enabled model in the routing policy in force. Enable one on Routing."


def _credential_title(harness: str, detail: str) -> str:
    """A plain, actionable sentence for a CREDENTIAL step failure."""
    hermes = _is_hermes_like(harness)
    if "no API key is stored" in detail or "no credential is stored" in detail:
        if hermes:
            return f"No key is stored for {harness}. Set it on Local gateway."
        return f"No credential is stored for {harness}. Log in on Credentials."
    if "cannot be read" in detail:
        if hermes:
            return f"The credential of {harness} cannot be read. Check Local gateway."
        return f"The credential of {harness} cannot be read. Check Credentials."
    return detail


# What each way a run can end means to the operator, for the model call step.
EXIT_WORDS = {
    ExitClass.AUTH_FAILURE.value: "the model provider refused the credential",
    ExitClass.TIMEOUT.value: "no answer before the time limit",
    ExitClass.STALLED.value: "no activity before the stall limit",
    ExitClass.QUOTA_EXHAUSTED.value: "the model provider says the quota is used up",
    ExitClass.PROVIDER_ERROR.value: "the model provider or the endpoint returned an error",
    ExitClass.ENVIRONMENT.value: "the worker could not run the harness (environment)",
    ExitClass.CRASHED.value: "the harness exited with an error",
    ExitClass.KILLED.value: "the run was killed",
    ExitClass.LOST.value: "the worker was lost",
}


@dataclass(slots=True)
class _Steps:
    items: list[dict[str, Any]]

    def passed(self, name: str, detail: str) -> None:
        self.items.append({"name": name, "ok": True, "result": "pass", "detail": detail})

    def failed(self, name: str, detail: str, *, title: str | None = None) -> dict[str, Any]:
        """Record a failed step.

        `detail` contains policy internals (policy names, versions).
        `title` is a plain, actionable sentence the operator can act on.
        """
        entry: dict[str, Any] = {
            "name": name,
            "ok": False,
            "result": "fail",
            "detail": detail,
        }
        if title is not None:
            entry["title"] = title
        self.items.append(entry)
        for later in STEPS[STEPS.index(name) + 1 :]:
            self.items.append({"name": later, "ok": None, "result": "not run", "detail": ""})
        return self.items[-1]

    def fail_in_progress(self, harness: str, exc: BaseException) -> None:
        """The step the run was on when it raised fails with the cause, and the rest are
        not run: the test never becomes a server error or a marker that stays running."""
        if len(self.items) >= len(STEPS):
            return
        current = STEPS[len(self.items)]
        self.failed(
            current,
            f"the test itself failed: {type(exc).__name__}: {exc}",
            title=f"Check the service log for the {harness} test.",
        )


def is_running(last: Any) -> TypeGuard[dict[str, Any]]:
    """Whether a stored `last_test` is the marker of a run in progress."""
    return isinstance(last, dict) and last.get("status") == RUNNING


def is_result_of(latest: Any, started: Any) -> bool:
    """Whether `latest` (a stored `last_test`) is the result of the run `started` is the
    marker of, or of a later one: finished, and started no earlier."""
    if not isinstance(latest, dict) or not isinstance(started, dict):
        return False
    if latest.get("status") != FINISHED:
        return False
    since, landed = started.get("started_at"), latest.get("started_at")
    return isinstance(since, str) and isinstance(landed, str) and landed >= since


def _stale(marker: dict[str, Any], now: datetime) -> bool:
    """A running marker nothing in this process is running, old enough to be a run that
    died with its process. An unreadable start time is stale too."""
    raw = marker.get("started_at")
    try:
        started = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    if started.tzinfo is None:
        return True
    return (now - started).total_seconds() > STALE_AFTER_SECONDS


def _not_tested(harness: str) -> dict[str, Any]:
    return {
        "harness": harness,
        "status": NOT_TESTED,
        "ok": None,
        "failed_step": None,
        "steps": [],
    }


def last_result(ctx: AdminContext, uow: UnitOfWork, *, harness: str) -> dict[str, Any]:
    """The harness's stored test: the running marker, the last result, or not tested."""
    if ctx.harnesses.get(harness) is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    state = uow.harnesses.get(harness)
    if state is None or not isinstance(state.last_test, dict):
        return _not_tested(harness)
    return dict(state.last_test)


def start_test(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Start the test as a background job and return its running marker at once (issue
    147). The marker is the harness's `last_test` until the result replaces it, so every
    api replica and the Harnesses page see the run; a second start while one runs returns
    the running marker and starts nothing. A marker older than `STALE_AFTER_SECONDS` with
    no run in this process is a run that died with its process, and a start replaces it.

    `uow` is read, never written: the thread stores the marker and the result through
    units of work of its own, so a result that lands quickly is never overwritten by the
    caller's later commit of the marker."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"harnesses test {harness}"
    )
    if ctx.harnesses.get(harness) is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    runs = ctx.harness_tests
    with runs.guard:
        marker = runs.running(harness)
        if marker is not None:
            return dict(marker)
        state = uow.harnesses.get(harness)
        stored = state.last_test if state is not None else None
        if is_running(stored) and not _stale(stored, ctx.clock.now()):
            return dict(stored)
        marker = {
            "harness": harness,
            "status": RUNNING,
            "ok": None,
            "failed_step": None,
            "steps": [],
            "started_at": ctx.clock.now().isoformat(),
            "started_by": principal,
        }
        landed = threading.Event()
        runs.start(
            harness,
            marker,
            partial(
                _in_background,
                ctx,
                marker,
                landed,
                principal=principal,
                harness=harness,
                reason=reason,
            ),
        )
    landed.wait(MARKER_WAIT_SECONDS)
    return dict(marker)


def _in_background(
    ctx: AdminContext,
    marker: dict[str, Any],
    landed: threading.Event,
    *,
    principal: str,
    harness: str,
    reason: str,
) -> None:
    """The thread's whole life: the loop is its own, as the request handlers' are."""
    try:
        asyncio.run(
            _background(ctx, marker, landed, principal=principal, harness=harness, reason=reason)
        )
    except Exception:  # the thread has nowhere to raise
        log.exception("the %s test could not run in the background", harness)
    finally:
        landed.set()


async def _background(
    ctx: AdminContext,
    marker: dict[str, Any],
    landed: threading.Event,
    *,
    principal: str,
    harness: str,
    reason: str,
) -> None:
    with ctx.uow_factory() as uow:
        state = harness_state(uow, ctx.clock, harness)
        state.last_test = dict(marker)
        state.updated_at = ctx.clock.now()
        uow.harnesses.put(state)
        uow.commit()
    landed.set()
    with ctx.uow_factory() as uow:
        await test_harness(
            ctx,
            uow,
            principal=principal,
            harness=harness,
            reason=reason,
            started_at=str(marker["started_at"]),
        )
        uow.commit()


async def test_harness(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    reason: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Run the steps in the foreground, store the result as the harness's `last_test`
    and return it: {harness, status, ok, failed_step, steps, started_at, tested_at,
    tested_by}. `started_at` is the running marker's when a background run calls."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"harnesses test {harness}"
    )
    adapter = ctx.harnesses.get(harness)
    if adapter is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    started = started_at or ctx.clock.now().isoformat()
    steps = _Steps([])
    try:
        await _run(ctx, uow, steps, principal=principal, harness=harness, reason=reason)
    except Exception as exc:
        log.exception("the %s test raised at step %d", harness, len(steps.items) + 1)
        steps.fail_in_progress(harness, exc)
    failed = next((s["name"] for s in steps.items if s["ok"] is False), None)
    result = {
        "harness": harness,
        "status": FINISHED,
        "ok": failed is None,
        "failed_step": failed,
        "steps": steps.items,
        "started_at": started,
        "tested_at": ctx.clock.now().isoformat(),
        "tested_by": principal,
    }
    state = harness_state(uow, ctx.clock, harness)
    state.last_test = result
    state.updated_at = ctx.clock.now()
    uow.harnesses.put(state)
    return result


async def _run(
    ctx: AdminContext,
    uow: UnitOfWork,
    steps: _Steps,
    *,
    principal: str,
    harness: str,
    reason: str,
) -> None:
    adapter = ctx.harnesses.require(harness)
    try:
        ctx.harnesses.resolve(harness, gates=ctx.harness_gates, state=uow.harnesses.get(harness))
    except HarnessUnavailableError as exc:
        steps.failed(
            ENABLED,
            f"{exc.reason}; enable it on Harnesses",
            title="Enable the harness on Harnesses.",
        )
        return
    steps.passed(ENABLED, "enabled in configuration and on Harnesses")

    default = uow.harness_images.get(harness)
    if default is None:
        steps.failed(
            IMAGE,
            f"no worker image is promoted for {harness}; choose one on Images",
            title=f"Promote a worker image for {harness} on Images.",
        )
        return
    if not adapter.supported_versions.supports(default.version):
        steps.failed(
            IMAGE,
            f"{default.reference} carries {harness} {default.version}, outside the tested "
            f"range {adapter.supported_versions.text}; promote a supported image on Images",
            title=f"Promote a supported image for {harness} on Images.",
        )
        return
    steps.passed(IMAGE, f"{default.reference} ({harness} {default.version})")

    if adapter.credential_spec() is None:
        steps.passed(CREDENTIAL, "this harness needs none")
    else:
        # Read on a worker thread with a bounded wait, as every async page does, so a
        # slow API server never holds the event loop.
        secrets = await credentials.read_secrets(ctx, [harness])
        view = credentials.state_view(ctx, uow, harness, secrets.get(harness))
        state = str(view.get("state"))
        if state == "unreadable":
            detail = (
                f"the credential of {harness} cannot be read: {view.get('detail') or 'no detail'}"
            )
            steps.failed(CREDENTIAL, detail, title=_credential_title(harness, detail))
            return
        # A refused credential ("invalid") goes on to the model call, which is the check
        # that can clear it once the operator has fixed it.
        if state == "absent":
            if harness == credentials.HERMES:
                detail = (
                    "no API key is stored for "
                    f"{harness}; set it with the gateway URL on Local gateway"
                )
            else:
                detail = f"no credential is stored for {harness}; log in on Credentials"
            steps.failed(CREDENTIAL, detail, title=_credential_title(harness, detail))
            return
        steps.passed(CREDENTIAL, f"stored ({state})")

    try:
        model, endpoint, endpoint_url = credentials.probe_route(uow, adapter, harness)
    except ApplicationError as exc:
        detail = exc.detail or exc.title
        steps.failed(ROUTE, detail, title=_route_title(harness, detail))
        return
    if model == "none":
        steps.passed(ROUTE, "this harness calls no model")
    elif endpoint == "local":
        steps.passed(ROUTE, f"{model} at {endpoint_url}")
    else:
        steps.passed(ROUTE, f"{model} on the {harness} subscription")

    try:
        record = await credentials.worker_probe(
            ctx, uow, harness=harness, principal=principal, reason=reason
        )
    except ApplicationError as exc:
        detail = exc.detail or exc.title
        steps.failed(WORKER, detail, title=f"Check the worker's egress for {harness}.")
        return
    except ValueError as exc:
        # The adapter refused to build a launch for this route (Hermes without a local
        # endpoint): a routing problem, reported, never a server error.
        detail = f"the harness cannot be launched on this route: {exc}"
        steps.failed(WORKER, detail, title=f"Fix the route for {harness} on Routing.")
        return
    if record.cause == "provider_unavailable":
        detail = f"the worker did not start: {record.detail}"
        steps.failed(WORKER, detail, title=f"Check the worker's egress for {harness}.")
        return
    seconds = f"{record.duration_seconds:g} s"
    steps.passed(WORKER, f"ran {record.image_digest or record.image} under the worker's egress")
    if record.exit_class == ExitClass.COMPLETED.value:
        steps.passed(
            MODEL,
            f"the harness ran and exited cleanly in {seconds}"
            if model == "none"
            else f"{model} answered one prompt in {seconds}",
        )
        return
    words = EXIT_WORDS.get(record.exit_class, f"the run ended as {record.exit_class}")
    code = f" (exit code {record.exit_code})" if record.exit_code is not None else ""
    detail = f"{words}{code}, after {seconds}"
    steps.failed(MODEL, detail, title=f"Fix the credential or provider for {harness}.")
