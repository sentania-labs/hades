"""The first real pull request does not get stuck (hades FDY-0139).

Each test drives one way the delivery half used to stall with no way out, against the
fake GitHub over real HTTP and the real supervisor tick: a `fix` disposition after its
correction, an early merge, a close, a reviewer that never reviews, a repository with no
CI, a CI re-run decision, the reviewer's summary edit, a second page of check runs, the
CI log excerpt, and the rate limit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import PullRequestState
from tests.fixtures import FakeClock
from tests.integration import test_github_delivery as delivery
from tests.integration.conftest import correction_document, event_kinds, run_to_settled
from tests.integration.fake_github import FakeGitHubServer
from tests.integration.test_github_delivery import (
    REPOSITORY,
    REVIEWER,
    gate,
    green,
    pr,
    publish,
)

# The delivery tier's fixtures, shared rather than copied: the fake GitHub, the App key,
# the client over real HTTP, the fake publisher, and the supervisor that drives them.
app_key = delivery.app_key
github = delivery.github
github_client = delivery.github_client
publisher = delivery.publisher
delivery_supervisor = delivery.delivery_supervisor

pytestmark = pytest.mark.integration

# The fake clock starts at 12:00 UTC on 2026-09-16; a run that concluded before it is
# the failure a later re-run decision is about, one after it is a fresh result.
BEFORE_DECISION = "2026-09-16T11:00:00Z"
AFTER_DECISION = "2026-09-16T12:30:00Z"


@pytest.fixture
def operator(ctx: AppContext, tokens: dict[str, str]) -> Iterator[TestClient]:
    """The operator's API client: the waivers are operator decisions (ADR 0025)."""
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['operator']}"}
    ) as c:
        yield c


def state(client: TestClient, task_id: str) -> str:
    return str(client.get(f"/v1/tasks/{task_id}").json()["state"])


def wakes(client: TestClient, reason: str) -> list[dict[str, Any]]:
    return [w for w in client.get("/v1/wakes").json()["items"] if w["reason"] == reason]


def sign_in(browser: TestClient, token: str) -> str:
    """Sign in to the UI and return the session's CSRF value, read from the task list
    (the status page needs the administrative context this tier does not wire)."""
    form = browser.get("/ui/sign-in")
    preauth = re.search(r'name="csrf" value="([a-f0-9]+)"', form.text)
    assert preauth is not None
    response = browser.post(
        "/ui/sign-in",
        data={"csrf": preauth.group(1), "token": token, "next": "/ui/tasks"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = browser.get("/ui/tasks")
    assert page.status_code == 200, page.text
    match = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)
    assert match is not None
    return match.group(1)


def waive(client: TestClient, task_id: str, kind: str, words: str) -> Any:
    return client.post(
        f"/v1/tasks/{task_id}/decisions",
        json={"kind": kind, "verbatim": words, "resolves": f"{kind} for this task"},
    )


# ----- 1: a fix disposition, its correction, green CI --------------------------------


@pytest.mark.parametrize("afterwards", ["reply", "edit"])
async def test_codex_finding_fix_correction_green_reaches_ready_for_merge(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    afterwards: str,
) -> None:
    """The whole path: a finding, `fix`, the correction on the same PR, green CI on the
    corrected head, and `ready_for_merge`. The `fix` on the replaced head is settled, but a
    reply on its thread, or an edit to it, after the correction is new feedback."""
    task_id, view = await publish(client, delivery_supervisor)
    first_head = view["head_sha"]
    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review. Reviewed commit: " + first_head,
        comments=[{"body": "P1 this is wrong", "path": "src/app.txt", "line": 1}],
    )
    # Made on the first head, before the correction exists (the fake clock's hour).
    finding = github.state.repositories[REPOSITORY].pulls[1].review_comments[0]
    finding["created_at"] = finding["updated_at"] = BEFORE_DECISION
    await delivery_supervisor.tick()
    assert state(client, task_id) == "external_feedback_received"
    comment_id = pr(client, task_id)["comments"][0]["id"]
    response = client.post(
        f"/v1/tasks/{task_id}/dispositions",
        json={
            "review_comment_id": comment_id,
            "disposition": "fix",
            "reasoning": "The reviewer is right and the work is not done.",
        },
    )
    assert response.status_code == 200, response.text
    document = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    response = client.post(f"/v1/tasks/{task_id}/corrections", json=document)
    assert response.status_code == 200, response.text
    assert await run_to_settled(delivery_supervisor, client, task_id) == "awaiting_ci_certification"
    corrected = client.get(f"/v1/tasks/{task_id}").json()
    assert corrected["state"] == "awaiting_ci_certification"
    assert corrected["head_sha"] != first_head

    github.state.set_check(REPOSITORY, corrected["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()

    assert state(client, task_id) == "ready_for_merge"
    record = pr(client, task_id)
    dispositions = [g for g in record["gates"] if g["gate"] == "feedback_dispositions_complete"]
    assert dispositions[-1]["head_sha"] == corrected["head_sha"]
    assert dispositions[-1]["result"] == "pass"
    assert wakes(client, "ready_for_merge")
    # The corrected head never stepped back to feedback for the settled comment.
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    after_correction = [
        e["kind"]
        for e in events
        if e["kind"] == "task_external_feedback_received"
        and e["payload"].get("reason") == "feedback dispositions are incomplete"
    ]
    assert after_correction == []

    pull = github.state.repositories[REPOSITORY].pulls[1]
    if afterwards == "reply":
        # A reply on the old thread after the correction is new feedback, not settled.
        github.state.add_review_comment(
            REPOSITORY,
            1,
            review_id=pull.reviews[0]["id"],
            login=REVIEWER,
            body="P1 still wrong after the correction",
            path="src/app.txt",
            line=1,
        )
        reply = pull.review_comments[-1]
        reply["commit_id"] = first_head
        reply["created_at"] = AFTER_DECISION
    else:
        # The original finding rewritten after the correction: judged by the edit, not by
        # when the comment was first made.
        pull.review_comments[0]["body"] = "P1 still wrong, and here is why"
        pull.review_comments[0]["updated_at"] = AFTER_DECISION
    await delivery_supervisor.tick()
    assert state(client, task_id) == "external_feedback_received"
    assert gate(pr(client, task_id), "feedback_dispositions_complete") == "pending"


# ----- 2: merged early, closed ---------------------------------------------------------


@pytest.mark.parametrize(
    "where",
    ["awaiting_external_review", "awaiting_ci_certification", "ci_certification_failed"],
)
async def test_an_early_merge_moves_the_task_to_merged_and_wakes_foundry(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    where: str,
) -> None:
    if where == "ci_certification_failed":
        task_id, _ = await failed_ci(client, delivery_supervisor, github)
    elif where == "awaiting_ci_certification":
        task_id, _ = await green(client, delivery_supervisor, github)
    else:
        task_id, _ = await publish(client, delivery_supervisor)
    assert state(client, task_id) == where
    github.state.merge(REPOSITORY, 1, by="sentania", sha="e" * 40)
    await delivery_supervisor.tick()
    assert state(client, task_id) == "merged"
    record = pr(client, task_id)
    assert record["state"] == "merged" and record["merged_by"] == "sentania"
    merged = wakes(client, "merged")
    assert merged and f"the task was {where}" in merged[-1]["summary"]
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    moved = [e for e in events if e["kind"] == "task_merged"]
    assert moved[-1]["payload"]["merged_from"] == where


async def test_a_task_stranded_behind_a_merged_pull_request_is_moved(
    client: TestClient,
    delivery_supervisor: Supervisor,
) -> None:
    """A pull request recorded merged before this fix is never polled again; the tick
    moves the task it left behind."""
    task_id, _ = await publish(client, delivery_supervisor)
    with delivery_supervisor._fenced() as uow:
        pull_request = uow.pull_requests.get_for_task(task_id, for_update=True)
        assert pull_request is not None
        pull_request.state = PullRequestState.MERGED
        pull_request.merge_sha = "d" * 40
        uow.pull_requests.save(pull_request)
        uow.commit()
    await delivery_supervisor.tick()
    assert state(client, task_id) == "merged"
    assert wakes(client, "merged")


@pytest.mark.parametrize("diverge", [False, True])
async def test_a_close_without_merge_rejects_the_task_and_wakes_foundry(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    diverge: bool,
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    if diverge:
        github.state.push(REPOSITORY, "crucible/EX-0001", "c" * 40, by="someone-else")
        await delivery_supervisor.tick()
        assert state(client, task_id) == "head_diverged"
    github.state.close(REPOSITORY, 1, by="sentania")
    await delivery_supervisor.tick()
    assert state(client, task_id) == "rejected"
    closed = wakes(client, "pull_request_closed")
    assert closed and "closed without being merged by sentania" in closed[-1]["summary"]


# ----- 3: the operator's waivers -------------------------------------------------------


async def test_waiving_the_external_review_moves_on_to_certification(
    client: TestClient,
    operator: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
) -> None:
    task_id, view = await publish(client, delivery_supervisor)
    await delivery_supervisor.tick()
    assert state(client, task_id) == "awaiting_external_review"

    refused = waive(client, task_id, "waive_external_review", "Foundry may not waive this.")
    assert refused.status_code == 403, refused.text

    response = waive(
        operator, task_id, "waive_external_review", "Codex is not reviewing this repository."
    )
    assert response.status_code == 201, response.text
    await delivery_supervisor.tick()
    assert state(client, task_id) == "awaiting_ci_certification"
    record = pr(client, task_id)
    rounds = [g for g in record["gates"] if g["gate"] == "external_review_rounds"][-1]
    assert rounds["result"] == "skipped"
    assert "waived by the operator" in rounds["detail"]
    assert "Codex is not reviewing this repository." in rounds["detail"]
    assert "decision_recorded" in event_kinds(client, task_id)

    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ready_for_merge"


async def test_accepting_no_ci_skips_certification(
    client: TestClient,
    operator: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
) -> None:
    task_id, _ = await green(client, delivery_supervisor, github)
    await delivery_supervisor.tick()
    assert state(client, task_id) == "awaiting_ci_certification"
    assert pr(client, task_id)["ci_certifications"][-1]["state"] == "pending"

    response = waive(operator, task_id, "accept_no_ci", "This repository has no CI at all.")
    assert response.status_code == 201, response.text
    await delivery_supervisor.tick()

    assert state(client, task_id) == "ready_for_merge"
    certification = pr(client, task_id)["ci_certifications"][-1]
    assert certification["state"] == "skipped"
    assert "no CI" in certification["detail"]
    assert "This repository has no CI at all." in certification["detail"]


async def test_accepting_no_ci_does_not_hide_a_check_that_runs(
    client: TestClient,
    operator: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", status="in_progress")
    response = waive(operator, task_id, "accept_no_ci", "We thought there was no CI.")
    assert response.status_code == 201, response.text
    await delivery_supervisor.tick()
    assert state(client, task_id) == "awaiting_ci_certification"
    assert pr(client, task_id)["ci_certifications"][-1]["state"] == "pending"


async def test_accepting_no_ci_does_not_skip_a_required_check_that_never_appeared(
    client: TestClient,
    operator: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    engine: Engine,
) -> None:
    """A configured required check that has not shown up yet is late, not absent."""
    # Expectations come from the optional policy narrowing, not branch protection.
    with engine.begin() as connection:
        original = connection.execute(
            text("SELECT document FROM policies WHERE name = 'default-software' AND version = 2")
        ).scalar_one()["ci_certification"]["required_checks"]
        connection.execute(
            text(
                "UPDATE policies SET document = jsonb_set(document, "
                "'{ci_certification,required_checks}', '[\"build\"]') "
                "WHERE name = 'default-software' AND version = 2"
            )
        )
    try:
        task_id, _ = await green(client, delivery_supervisor, github)
        response = waive(operator, task_id, "accept_no_ci", "We thought there was no CI.")
        assert response.status_code == 201, response.text
        await delivery_supervisor.tick()
        assert state(client, task_id) == "awaiting_ci_certification"
        assert pr(client, task_id)["ci_certifications"][-1]["state"] == "pending"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE policies SET document = jsonb_set(document, "
                    "'{ci_certification,required_checks}', CAST(:checks AS jsonb)) "
                    "WHERE name = 'default-software' AND version = 2"
                ),
                {"checks": json.dumps(original)},
            )


async def test_accepting_no_ci_counts_a_skipped_run_as_nothing(
    client: TestClient,
    operator: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
) -> None:
    """A workflow a path filter skipped is no CI for this change."""
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_check(REPOSITORY, view["head_sha"], name="docs-only", conclusion="skipped")
    response = waive(operator, task_id, "accept_no_ci", "Only a skipped workflow here.")
    assert response.status_code == 201, response.text
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ready_for_merge"


async def test_a_waiver_is_refused_outside_a_delivery_wait(
    client: TestClient,
    operator: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.merge(REPOSITORY, 1, by="sentania", sha="e" * 40)
    await delivery_supervisor.tick()
    assert state(client, task_id) == "merged"
    response = waive(operator, task_id, "waive_external_review", "Too late for this.")
    assert response.status_code == 409, response.text


async def test_the_task_page_button_records_the_waiver(
    ctx: AppContext,
    tokens: dict[str, str],
    client: TestClient,
    delivery_supervisor: Supervisor,
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    with TestClient(create_app(ctx)) as browser:
        csrf = sign_in(browser, tokens["admin"])
        listing = browser.get("/ui/tasks")
        assert f"/ui/tasks/{task_id}" in listing.text
        page = browser.get(f"/ui/tasks/{task_id}")
        assert page.status_code == 200, page.text
        assert "Waive the remaining external review rounds" in page.text
        assert "Accept that this repository has no CI" in page.text
        response = browser.post(
            f"/ui/tasks/{task_id}/decisions",
            data={
                "csrf": csrf,
                "kind": "waive_external_review",
                "resolves": "the reviewer did not review",
                "verbatim": "The reviewer has not looked at it in two days.",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "kind=ok" in response.headers["location"]
        await delivery_supervisor.tick()
        assert state(client, task_id) == "awaiting_ci_certification"
        page = browser.get(f"/ui/tasks/{task_id}")
        assert "The reviewer has not looked at it in two days." in page.text
        assert re.search(r"waive_external_review", page.text)

    with TestClient(create_app(ctx)) as reader:
        csrf = sign_in(reader, tokens["observer"])
        page = reader.get(f"/ui/tasks/{task_id}")
        assert "Waive the remaining external review rounds" not in page.text
        refused = reader.post(
            f"/ui/tasks/{task_id}/decisions",
            data={"csrf": csrf, "kind": "accept_no_ci", "verbatim": "an observer tries"},
            follow_redirects=False,
        )
        assert "admin%20role%20required" in refused.headers["location"]


# ----- 4: a CI re-run decision, then green ---------------------------------------------


async def failed_ci(
    client: TestClient, supervisor: Supervisor, github: FakeGitHubServer
) -> tuple[str, str]:
    task_id, view = await green(client, supervisor, github)
    github.state.set_check(
        REPOSITORY,
        view["head_sha"],
        name="build",
        conclusion="failure",
        run_id="7001",
        completed_at=BEFORE_DECISION,
    )
    await supervisor.tick()
    assert state(client, task_id) == "ci_certification_failed"
    return task_id, str(view["head_sha"])


async def test_a_rerun_decision_waits_for_the_rerun_and_then_goes_green(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, head = await failed_ci(client, delivery_supervisor, github)
    response = client.post(
        f"/v1/tasks/{task_id}/ci-decision",
        json={"cause": "flaky_test", "action": "rerun", "reasoning": "A known flake."},
    )
    assert response.status_code == 200, response.text

    # Nobody has re-run anything yet: GitHub still shows the old failure, and it is not
    # counted again.
    await delivery_supervisor.tick()
    await delivery_supervisor.tick()
    assert state(client, task_id) == "awaiting_ci_certification"
    certification = pr(client, task_id)["ci_certifications"][-1]
    assert certification["state"] == "pending"
    assert "re-run was decided" in certification["detail"]
    assert len(wakes(client, "ci_certification_failed")) == 1

    github.state.rerun_check(
        REPOSITORY,
        head,
        name="build",
        run_id="7002",
        conclusion="success",
        completed_at=AFTER_DECISION,
    )
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ready_for_merge"


async def test_a_rerun_that_fails_again_is_a_new_failure(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, head = await failed_ci(client, delivery_supervisor, github)
    client.post(
        f"/v1/tasks/{task_id}/ci-decision",
        json={"cause": "flaky_test", "action": "rerun", "reasoning": "A known flake."},
    )
    github.state.rerun_check(
        REPOSITORY,
        head,
        name="build",
        run_id="7003",
        conclusion="failure",
        completed_at=AFTER_DECISION,
    )
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ci_certification_failed"
    assert len(wakes(client, "ci_certification_failed")) == 2


async def test_a_different_failure_after_a_rerun_decision_counts(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """Staleness is about the runs the decision named, not a clock: a run the decision
    never saw is a new failure even if GitHub says it concluded earlier."""
    task_id, head = await failed_ci(client, delivery_supervisor, github)
    client.post(
        f"/v1/tasks/{task_id}/ci-decision",
        json={"cause": "flaky_test", "action": "rerun", "reasoning": "A known flake."},
    )
    github.state.rerun_check(
        REPOSITORY,
        head,
        name="build",
        run_id="7006",
        conclusion="failure",
        completed_at=BEFORE_DECISION,
    )
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ci_certification_failed"


async def test_a_green_certification_moves_a_failed_task_on(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    """Someone re-ran it on GitHub without a decision: the failure is past."""
    task_id, head = await failed_ci(client, delivery_supervisor, github)
    github.state.rerun_check(REPOSITORY, head, name="build", run_id="7004", conclusion="success")
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ready_for_merge"


# ----- 5: the summary edit ---------------------------------------------------------------


async def test_the_reviewers_summary_edit_does_not_revoke_ready_for_merge(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await publish(client, delivery_supervisor)
    github.state.add_issue_comment(
        REPOSITORY, 1, login=REVIEWER, body="Codex Review Summary: in progress"
    )
    github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
    github.state.set_check(REPOSITORY, view["head_sha"], name="build", conclusion="success")
    await delivery_supervisor.tick()
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ready_for_merge"

    summary = github.state.repositories[REPOSITORY].pulls[1].issue_comments[0]
    summary["body"] = "Codex Review Summary: complete, no findings"
    summary["updated_at"] = "2030-01-01T00:00:00Z"
    await delivery_supervisor.tick()

    assert state(client, task_id) == "ready_for_merge"
    assert len(wakes(client, "external_feedback_received")) == 0


# ----- 6 and 7: the GitHub client -------------------------------------------------------


async def test_a_required_check_on_the_second_page_is_seen(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    head = view["head_sha"]
    for index in range(120):
        github.state.set_check(REPOSITORY, head, name=f"matrix-{index}", run_id=str(8000 + index))
    github.state.set_check(REPOSITORY, head, name="build", conclusion="success", run_id="8999")
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ready_for_merge"
    certification = pr(client, task_id)["ci_certifications"][-1]
    assert len(certification["check_runs"]) == 121


async def test_the_ci_log_excerpt_follows_the_redirect_once(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    github.state.workflow_log = b"step 1 ok\n" * 50 + b"error: the build failed at the end\n"
    task_id, _ = await failed_ci(client, delivery_supervisor, github)
    await delivery_supervisor.tick()
    await delivery_supervisor.tick()
    await delivery_supervisor.tick()
    failure = pr(client, task_id)["ci_certifications"][-1]["failure"]
    assert failure["log_excerpt"].endswith("error: the build failed at the end\n")
    # The job's own log endpoint, redirected to a signed URL fetched without the token,
    # and only once however many polls follow.
    assert github.state.log_downloads == ["/_signed-logs/job/7001"]
    assert github.state.log_download_authorized is False
    calls = [path for _, path in github.state.calls]
    assert calls.count(f"/repos/{REPOSITORY}/actions/jobs/7001/logs") == 1


async def test_a_failed_workflow_run_logs_its_failed_job(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, view = await green(client, delivery_supervisor, github)
    github.state.set_workflow_run(
        REPOSITORY, view["head_sha"], name="ci", conclusion="failure", run_id="5151"
    )
    await delivery_supervisor.tick()
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ci_certification_failed"
    failure = pr(client, task_id)["ci_certifications"][-1]["failure"]
    assert failure["source"] == "workflow_run"
    assert "the required check failed" in failure["log_excerpt"]
    assert github.state.log_downloads == ["/_signed-logs/job/515101"]


async def test_comment_reactions_are_fetched_only_when_there_are_some(
    client: TestClient, delivery_supervisor: Supervisor, github: FakeGitHubServer
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.add_review(
        REPOSITORY,
        1,
        login=REVIEWER,
        body="Codex Review",
        comments=[{"body": "P2 a nit", "path": "src/app.txt", "line": 2}],
    )
    github.state.add_issue_comment(REPOSITORY, 1, login="someone", body="thanks")
    await delivery_supervisor.tick()
    await delivery_supervisor.tick()
    calls = [path for _, path in github.state.calls]
    assert any(path.endswith("/issues/1/reactions") for path in calls)
    assert not any("/comments/" in path and path.endswith("/reactions") for path in calls)
    _ = task_id


async def test_a_rate_limit_defers_to_a_later_tick_without_sleeping(
    client: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    clock: FakeClock,
) -> None:
    task_id, _ = await publish(client, delivery_supervisor)
    github.state.rate_limit_once = True
    await delivery_supervisor.tick()
    assert "github_rate_limited" in event_kinds(client, task_id)
    assert delivery_supervisor.delivery.rate_limited_until is not None

    github.state.add_reaction(REPOSITORY, 1, login=REVIEWER, content="+1")
    polls = event_kinds(client, task_id).count("pull_request_polled")
    await delivery_supervisor.tick()
    # Still inside GitHub's retry-after: nothing is polled.
    assert event_kinds(client, task_id).count("pull_request_polled") == polls
    clock.advance(5)
    await delivery_supervisor.tick()
    assert state(client, task_id) == "awaiting_ci_certification"


async def test_a_decision_brings_the_next_poll_forward(
    client: TestClient,
    operator: TestClient,
    delivery_supervisor: Supervisor,
    github: FakeGitHubServer,
    clock: FakeClock,
) -> None:
    """A re-run decision or a waiver is acted on at the next tick, not after the poll
    interval: the pull request is polled again at once."""
    task_id, head = await failed_ci(client, delivery_supervisor, github)
    delivery_supervisor.delivery.config = DeliveryConfig(
        poll_interval_seconds=3600, reactions_poll_interval_seconds=3600
    )
    clock.advance(1)
    client.post(
        f"/v1/tasks/{task_id}/ci-decision",
        json={"cause": "flaky_test", "action": "rerun", "reasoning": "A known flake."},
    )
    github.state.rerun_check(
        REPOSITORY,
        head,
        name="build",
        run_id="7005",
        conclusion="success",
        completed_at=AFTER_DECISION,
    )
    await delivery_supervisor.tick()
    assert state(client, task_id) == "ready_for_merge"

    # Nothing decided since: the long interval holds and the next tick does not poll.
    polls = event_kinds(client, task_id).count("pull_request_polled")
    clock.advance(1)
    await delivery_supervisor.tick()
    assert event_kinds(client, task_id).count("pull_request_polled") == polls
    _ = operator
