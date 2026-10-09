#!/usr/bin/env python3
"""The compose smoke, in one place.

`make smoke`, the CI `compose-smoke` job and the release workflow all run this
file against an already-running compose stack. It is the only definition of what
"the stack works" means, so a change to TaskContractV1 or to the lifecycle
breaks every caller at once instead of breaking the release alone.

Stdlib only, on purpose: the compose-smoke job has a Docker daemon but no uv.

It drives one task for an `artifacts` deliverable through the fake provider:
submit, start, wait for automatic acceptance, and assert no gate is failing.
Artifacts need no GitHub delivery, so `accepted` is the terminal compose state.
Any non-2xx response stops the run with the method, the URL, the HTTP status and
the response body, because a traceback out of a JSON parser says nothing about
what the server refused.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 30.0
STATE_POLL_SECONDS = 2.0
STATE_ATTEMPTS = 30

# A task that lands in one of these will never reach the state the smoke is
# waiting for, so waiting out the full poll budget only turns a real failure
# into something that reads like a slow machine.
DEAD_END_STATES = frozenset(
    {
        "blocked",
        "pre_pr_gates_failed",
        "publish_failed",
        "ci_certification_failed",
        "head_diverged",
        "rejected",
        "cancelling",
        "cancelled",
        "closed",
    }
)


class SmokeError(Exception):
    """A failure with a message an operator can act on without reading a traceback."""


def minted_token(document: Any) -> str:
    """The token `crucible admin token create` printed: `data.token` of its envelope.

    A KeyError or TypeError here is the caller's cue that the output had no token; the
    output itself is never echoed, because it carries one.
    """
    if isinstance(document, dict) and "envelope" in document:
        if not document.get("ok"):
            raise KeyError("the envelope reports a failure")
        document = document["data"]
    return str(document["token"])


def log(message: str) -> None:
    print(message, flush=True)


def field(payload: Any, key: str, where: str) -> Any:
    """Read one field, and say which response was the wrong shape if it is missing.

    The point of this whole file is that a mismatch reports what the server
    actually sent. A bare KeyError on a renamed field would be the same class of
    unreadable failure as the JSON traceback that started this.
    """
    if not isinstance(payload, dict) or key not in payload:
        body = json.dumps(payload)[:2000] if payload is not None else "(empty body)"
        raise SmokeError(
            f"the response from {where} has no {key!r} field, so the smoke cannot "
            f"continue.\nResponse body: {body}"
        )
    return payload[key]


def request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """One HTTP call. Returns the decoded JSON body, or None for an empty one."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        text = raw.decode("utf-8", "replace").strip() or "(empty body)"
        raise SmokeError(
            f"{method} {url} returned HTTP {exc.code}.\n"
            f"Response body: {text}\n"
            "A 422 here means the request no longer matches the server's contract; "
            "compare it against examples/task-contracts/example-software-task.yaml."
        ) from None
    except urllib.error.URLError as exc:
        raise SmokeError(f"{method} {url} could not be reached: {exc.reason}") from None
    if not (200 <= status < 300):
        raise SmokeError(f"{method} {url} returned HTTP {status}")
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        text = raw.decode("utf-8", "replace").strip()
        raise SmokeError(
            f"{method} {url} returned HTTP {status} with a body that is not JSON.\n"
            f"Response body: {text}"
        ) from None


def compose(args: list[str], *, redact: bool = False) -> str:
    """Run a docker compose subcommand in the caller's compose project.

    `redact` is for commands whose output carries a credential: the failure
    message then names the command and the exit code only, because these
    messages end up in a public CI log.
    """
    command = os.environ.get("COMPOSE", "docker compose").split() + args
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        if redact:
            detail = "Its output is withheld because it can carry a token."
        else:
            # Both streams: `crucible admin` prints its envelope, error included, on stdout
            # and its logs on stderr, and the caller matches on the error's code.
            parts = (completed.stderr, completed.stdout)
            detail = "\n".join(p.strip() for p in parts if p.strip()) or "(no output)"
        raise SmokeError(f"`{' '.join(command)}` exited {completed.returncode}.\n{detail}")
    return completed.stdout


def task_contract(external_id: str) -> dict[str, Any]:
    """A minimal valid TaskContractV1 for an `artifacts` deliverable.

    Every field the server requires is named here, so a schema change surfaces as
    a 422 on the very next pull request rather than on a tag.
    """
    return {
        "schema_version": "1.0",
        "external_id": external_id,
        "title": "Compose smoke",
        "project": "example-service",
        "parent_external_id": None,
        "repository": {
            "name": "example-service",
            "base_ref": "main",
            "work_branch": f"crucible/{external_id}",
        },
        "scope": {
            "allowed_paths": ["src/**"],
            "prohibited_paths": [".github/**"],
            "may_add_dependencies": False,
            "may_modify_ci": False,
        },
        "objective": "Prove the compose stack boots and runs a task end to end.",
        "context": [],
        "project_instructions": [],
        "acceptance_criteria": [{"id": "AC1", "text": "The task reaches accepted."}],
        "required_verification": [
            {"id": "V1", "command": "make lint", "expect_exit": 0},
            {"id": "V2", "command": "make test", "expect_exit": 0},
            {"id": "V3", "command": "make scan", "expect_exit": 0},
            {"id": "V4", "kind": "artifact", "path": "report/run-evidence.md"},
        ],
        "constraints": {"prohibited_actions": [], "network": "policy"},
        "deliverables": [{"kind": "artifacts", "target": None, "draft": False, "closes": []}],
        "reporting": {
            "report_schema": "CompletionClaimV1",
            "report_dir": "/crucible/report",
            "progress_events": True,
        },
        "escalation": {
            "conditions": [],
            "action": "write report/blocked.md with the question and exit 75",
        },
        "policy": {"name": "default-software", "version": 3},
        # C6b selects the concrete harness and model from the tier. The fake provider
        # accepts the supplied image as a deterministic smoke-test affordance.
        "execution_request": {
            "tier": "trivial",
            "effort": None,
            "provider": "fake",
            "image": "crucible-worker:fake-succeed",
            "timeout_seconds": 600,
            "rationale": "compose smoke",
        },
        "lifecycle": {"max_attempts": 2, "retry_on": ["environment", "lost"], "cleanup": "policy"},
        "correction": None,
    }


def mint_token(principal: str) -> str:
    """Create an orchestrator token inside the running crucible container."""
    raw = compose(
        [
            "exec",
            "-T",
            "crucible",
            "crucible",
            "admin",
            "--reason",
            "compose smoke principal",
            "token",
            "create",
            "--principal",
            principal,
            "--role",
            "orchestrator",
        ],
        redact=True,
    )
    try:
        return minted_token(json.loads(raw))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        # The output is never echoed: it carries a live bearer token, and a
        # smoke failure is a public CI log.
        raise SmokeError(
            "`crucible admin token create` did not return a JSON object with a "
            f"`token` field ({exc}). Its output is withheld because it carries a token."
        ) from None


FIRST_RUN_FILE = "/var/lib/crucible/credentials/first-run-admin-token"
TOKEN_PATTERN = re.compile(r"\bcru_[A-Z0-9]{26}\.[A-Za-z0-9_-]+\b")
# Stable page markers; grep the data-page attribute on <main>, never a product name.
# The board uses a separate template (board.html) outside _page, so it carries no marker.
_PAGE_MARKERS: dict[str, str] = {
    "/ui": "status",
    "/ui/harnesses": "harnesses",
    "/ui/credentials": "credentials",
    "/ui/images": "images",
    "/ui/routing": "routing",
    "/ui/repositories": "repositories",
    "/ui/tokens": "tokens",
    "/ui/github": "github",
    "/ui/workers": "workers",
    "/ui/tasks": "tasks",
    "/ui/wakes": "wakes",
    "/ui/retention": "retention",
    "/ui/audit": "audit",
    "/ui/bootstrap": "bootstrap",
    "/ui/settings": "settings",
}
# The board page template (not rendered by _page) uses a product-name check.
BOARD_MARKER = b"<h1>Board</h1>"


def walk_first_run_ui(base_url: str) -> None:
    """On a fresh compose database, sign in with the migration's one-time token.

    The token is in a mode 0600 file in the credential root, never in the migration's
    log (ADR 0016, crucible#122), and the first sign-in removes the file. A stack whose
    first-run administrator already signed in has no file, so the walk does not apply.
    The token is never printed by this process.
    """
    logs = compose(["logs", "--no-color", "migrate"], redact=True)
    if TOKEN_PATTERN.search(logs):
        raise SmokeError("the migration log carries a token (crucible#122)")
    token = compose(
        ["exec", "-T", "crucible", "sh", "-c", f"cat {FIRST_RUN_FILE} 2>/dev/null || true"],
        redact=True,
    ).strip()
    if not token:
        log("first-run UI walk skipped: no first-run token file (already signed in)")
        return
    if not TOKEN_PATTERN.fullmatch(token):
        raise SmokeError("the first-run token file does not hold a Crucible token")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{base_url}/ui/sign-in", timeout=DEFAULT_TIMEOUT) as response:
        sign_in = response.read().decode("utf-8", "replace")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', sign_in)
    if csrf is None:
        raise SmokeError("the first-run sign-in page has no pre-authentication CSRF nonce")
    if FIRST_RUN_FILE not in sign_in or "logs migrate" in sign_in:
        raise SmokeError("the sign-in page does not name the first-run token file")
    body = urllib.parse.urlencode({"csrf": csrf.group(1), "token": token, "next": "/ui"}).encode()
    request_object = urllib.request.Request(
        f"{base_url}/ui/sign-in",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        # The explicit next=/ui returns to Status and its first-run setup steps.
        # The root and the default sign-in destination instead lead to the board.
        for path, marker in _PAGE_MARKERS.items():
            with opener.open(f"{base_url}{path}", timeout=DEFAULT_TIMEOUT) as response:
                if response.status != 200:
                    raise SmokeError(f"the first-run UI page {path} returned HTTP {response.status}")
                body = response.read()
                if f'data-page="{marker}"'.encode() not in body:
                    raise SmokeError(
                        f"the first-run UI page {path} lacks data-page={marker!r}"
                    )
        # The board uses board.html directly (not _page), so we check for its heading.
        with opener.open(f"{base_url}/ui/board", timeout=DEFAULT_TIMEOUT) as response:
            if response.status != 200:
                raise SmokeError("the first-run Board page returned HTTP {}".format(response.status))
            if BOARD_MARKER not in response.read():
                raise SmokeError("the first-run Board page did not render")
    except urllib.error.URLError as exc:
        raise SmokeError(f"the first-run UI walk failed: {exc}") from None
    left = compose(
        ["exec", "-T", "crucible", "sh", "-c", f"test -e {FIRST_RUN_FILE} && echo left || true"]
    )
    if left.strip():
        raise SmokeError("the first sign-in did not remove the first-run token file")
    log("first-run administrator sign-in and every UI page passed; the token file is gone")


def register_repository() -> None:
    """Register the fixture repository, tolerating one that is already there.

    `make smoke` run twice against the same stack must not fail on the second
    run for a reason that has nothing to do with the stack working.

    Registration is an administrative mutation (25), so it needs the supervisor's lease
    to be live. The stack has just started, so the first ticks may not have happened yet;
    this waits for the lease rather than going around the gate.
    """
    deadline = time.monotonic() + 60
    while True:
        try:
            _register_once()
            return
        except SmokeError as exc:
            if "supervisor-not-live" not in str(exc) or time.monotonic() > deadline:
                raise
            log("waiting for the supervisor's lease before registering")
            time.sleep(2)


def _register_once() -> None:
    try:
        compose(
            [
                "exec",
                "-T",
                "crucible",
                "crucible",
                "admin",
                # Registration under the administrative surface is a mutation like any
                # other (25): a reason, and a live supervisor lease.
                "--reason",
                "compose smoke: the example repository",
                "repository",
                "register",
                "--name",
                "example-service",
                "--url",
                "https://github.com/example-org/example-service",
                # 23: the default policy requires an external review round, and GitHub
                # exposes the reviewer's "review all pull requests" setting nowhere, so
                # registration records the operator's attestation instead. The smoke
                # stack is the operator's own, so the smoke attests.
                "--attest-external-review-all-prs",
                "--attested-by",
                "compose-smoke",
            ]
        )
        log("repository registered")
    except SmokeError as exc:
        if "exists" not in str(exc).lower():
            raise
        log("repository already registered")


def fetch_task(base_url: str, token: str, task_id: str) -> Any:
    return request("GET", f"{base_url}/v1/tasks/{task_id}", token=token)


def await_state(base_url: str, token: str, task_id: str, wanted: str) -> Any:
    where = f"GET /v1/tasks/{task_id}"
    state = "unknown"
    for attempt in range(STATE_ATTEMPTS):
        task = fetch_task(base_url, token, task_id)
        state = field(task, "state", where)
        if state == wanted:
            log(f"state: {state}")
            return task
        if state in DEAD_END_STATES:
            raise SmokeError(
                f"task {task_id} reached {state!r}, which never becomes {wanted!r}. "
                f"Gate summary: {json.dumps(task.get('gate_summary'))}"
            )
        if attempt < STATE_ATTEMPTS - 1:
            time.sleep(STATE_POLL_SECONDS)
    raise SmokeError(
        f"task {task_id} is still in {state!r} after "
        f"{int((STATE_ATTEMPTS - 1) * STATE_POLL_SECONDS)}s; wanted {wanted!r}."
    )


def smoke(
    base_url: str,
    token: str | None,
    external_id: str,
    principal: str,
    expect_version: str | None,
) -> None:
    log(f"base URL: {base_url}")

    health = request("GET", f"{base_url}/v1/health")
    log(f"health: {json.dumps(health)}")
    if expect_version is not None:
        reported = field(health, "version", "GET /v1/health")
        if reported != expect_version:
            raise SmokeError(
                f"/v1/health reports version {reported!r}, but this run is publishing "
                f"{expect_version!r}."
            )
        log(f"reported version matches {expect_version}")
    request("GET", f"{base_url}/v1/ready")
    log("ready")
    walk_first_run_ui(base_url)

    if token is None:
        token = mint_token(principal)
    register_repository()

    task = request("POST", f"{base_url}/v1/tasks", token=token, body=task_contract(external_id))
    task_id = field(task, "id", "POST /v1/tasks")
    log(f"submitted task {task_id}")

    request(
        "POST",
        f"{base_url}/v1/tasks/{task_id}/start",
        token=token,
        body={
            "provider": "fake",
            "image": "crucible-worker:fake-succeed",
            "policy_version": 3,
        },
    )
    log("started")

    task = await_state(base_url, token, task_id, "accepted")
    where = f"GET /v1/tasks/{task_id}"
    summary = field(task, "gate_summary", where)
    log(f"gate summary: {json.dumps(summary, indent=2)}")
    failing = field(summary, "failing", f"{where} (gate_summary)")
    if failing:
        raise SmokeError(f"gates failed on the collected head: {json.dumps(failing)}")

    wakes = request("GET", f"{base_url}/v1/wakes", token=token)
    log("wakes waiting for poll:")
    for wake in field(wakes, "items", "GET /v1/wakes"):
        log(f"  {wake.get('reason')} - {wake.get('summary')}")

    events = request("GET", f"{base_url}/v1/tasks/{task_id}/events?limit=200", token=token)
    log("events:")
    for event in field(events, "items", f"GET /v1/tasks/{task_id}/events"):
        log(f"  {event.get('seq')} {event.get('kind')}")

    log("compose smoke passed")


def env_or_dotenv(name: str, default: str) -> str:
    """The process environment first, then ./.env, which is what compose read.

    Deliberately not a dotenv parser: it reads one flat `KEY=value` line, drops
    a trailing comment and surrounding quotes, and falls back to the default for
    anything else. The port is the only value read this way.
    """
    if name in os.environ:
        return os.environ[name]
    try:
        with open(".env", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("#"):
                    continue
                line = line.removeprefix("export ").strip()
                if not line.startswith(f"{name}="):
                    continue
                value = line.split("=", 1)[1].split(" #", 1)[0].strip()
                return value.strip("\"'") or default
    except OSError:
        pass
    return default


def main(argv: list[str] | None = None) -> int:
    default_url = os.environ.get(
        "CRUCIBLE_SMOKE_BASE_URL",
        f"http://127.0.0.1:{env_or_dotenv('CRUCIBLE_PORT', '8080')}",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=default_url, help="base URL of the running API")
    parser.add_argument(
        "--token",
        default=os.environ.get("CRUCIBLE_SMOKE_TOKEN") or None,
        help="orchestrator token; minted with `crucible admin` in the container when omitted",
    )
    # Unique per run by default: an external id and a principal are both
    # unique in the database, so fixed values make a second `make smoke` against
    # the same stack fail for a reason that is not about the stack.
    run_id = str(int(time.time()))
    parser.add_argument(
        "--external-id",
        default=os.environ.get("CRUCIBLE_SMOKE_EXTERNAL_ID") or f"SMOKE-{run_id}",
        help="external id for the smoke task; unique per run by default",
    )
    parser.add_argument(
        "--principal",
        default=os.environ.get("CRUCIBLE_SMOKE_PRINCIPAL") or f"smoke-{run_id}",
        help="principal the smoke token and the internal review are recorded under",
    )
    parser.add_argument(
        "--expect-version",
        default=os.environ.get("CRUCIBLE_SMOKE_EXPECT_VERSION") or None,
        help="assert /v1/health reports this version; the release workflow sets it",
    )
    args = parser.parse_args(argv)

    try:
        smoke(
            args.base_url.rstrip("/"),
            args.token,
            args.external_id,
            args.principal,
            args.expect_version,
        )
    except SmokeError as exc:
        print(f"compose smoke failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
