#!/usr/bin/env python3
"""The deployed-on-Kubernetes smoke (C9).

`make deploy-kind` runs this against the deployment manifests brought up on a
disposable kind cluster. It is the Kubernetes counterpart of `compose_smoke.py` and it
asks for more, because the thing being proved is different: compose proves the stack
boots and drives a task through the fake provider, and this proves the *manifests* boot
and drive a task through the real Kubernetes provider, in real Pods, on the cluster the
deployment describes.

What it asserts, in order:

1. the deployed api answers `/v1/health` and `/v1/ready` through the Service;
2. the admin status page (25) reports the Kubernetes provider `ok`, with 26's namespace
   readiness probe passed, the CNI egress enforcement result, and the pod PID limit;
3. one trivial script-harness task runs end to end through the deployed API on the
   Kubernetes provider and reaches `accepted`, the same terminal state the compose
   smoke expects.

Stdlib only, like the compose smoke: the point of both files is that they run wherever
the thing they are smoking runs.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 60.0
STATE_POLL_SECONDS = 3.0
STATE_ATTEMPTS = 120

ORIGIN = "kind-deploy"
ORIGIN_URL = f"file:///crucible/cache/{ORIGIN}.git"
POLICY = "kind-deploy"
ROUTING = "kind-routing"
WORKERS_NAMESPACE = "crucible-workers"
NAMESPACE = "crucible"

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

# The three checks the seeded origin carries, standing in for a repository's own
# `make lint`, `make test` and `make scan` (18).
CHECKS = {"lint.sh": "echo lint ok", "test.sh": "echo test ok", "scan.sh": "echo scan ok"}


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
    if not isinstance(payload, dict) or key not in payload:
        body = json.dumps(payload)[:2000] if payload is not None else "(empty body)"
        raise SmokeError(f"the response from {where} has no {key!r} field.\nResponse body: {body}")
    return payload[key]


# The live `kubectl port-forward`, so a transport failure can re-establish it rather
# than end the run. A long call does happen here: `GET /v1/admin/status` asks the
# Kubernetes provider for its health and the provider answers by running 26's readiness
# canary, which is a real Pod.
ACTIVE: dict[str, PortForward] = {}
TRANSPORT_ATTEMPTS = 3


def request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    raw = b""
    status = 0
    for attempt in range(TRANSPORT_ATTEMPTS):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as response:
                raw = response.read()
                status = response.status
            break
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace").strip() or "(empty body)"
            raise SmokeError(
                f"{method} {url} returned HTTP {exc.code}.\nResponse body: {text}"
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            # A dropped forward is a fact about kubectl's tunnel, not about the
            # deployment, and it must not be reported as one.
            if attempt == TRANSPORT_ATTEMPTS - 1:
                raise SmokeError(f"{method} {url} could not be reached: {exc}") from None
            log(f"{method} {url}: {type(exc).__name__}; re-establishing the port forward")
            forward = ACTIVE.get("api")
            if forward is not None:
                forward.ensure()
            time.sleep(2)
    if not (200 <= status < 300):
        raise SmokeError(f"{method} {url} returned HTTP {status}")
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        text = raw.decode("utf-8", "replace").strip()
        raise SmokeError(f"{method} {url} returned a body that is not JSON.\n{text}") from None


def kubectl(args: list[str], *, redact: bool = False, check: bool = True) -> str:
    """Run one kubectl command against the run's own kubeconfig.

    `redact` is for commands whose output carries a credential: the failure message then
    names the command and the exit code only, because these messages end up in a public
    CI log.
    """
    command = ["kubectl", *args]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        detail = (
            "Its output is withheld because it can carry a token."
            if redact
            else "\n".join(p.strip() for p in (completed.stderr, completed.stdout) if p.strip())
            or "(no output)"
        )
        raise SmokeError(f"`{' '.join(command)}` exited {completed.returncode}.\n{detail}")
    return completed.stdout


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class PortForward:
    """`kubectl port-forward` to the api Service, for as long as the smoke runs.

    The deployment's own route is the Ingress, and a kind cluster has no ingress
    controller and no DNS; this reaches the same Service the Ingress points at.
    """

    def __init__(self) -> None:
        self.port = free_port()
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ensure(self) -> None:
        """Start the forward, or replace one that has died."""
        if self.process is not None and self.process.poll() is None:
            return
        self.stop()
        self.process = subprocess.Popen(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "port-forward",
                "service/crucible-api",
                f"{self.port}:8080",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

    def __enter__(self) -> str:
        ACTIVE["api"] = self
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            self.ensure()
            try:
                request("GET", f"{self.base_url}/v1/health")
                return self.base_url
            except SmokeError:
                time.sleep(2)
        raise SmokeError(f"the deployed api never answered on {self.base_url}")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10)
            self.process = None

    def __exit__(self, *_: object) -> None:
        ACTIVE.pop("api", None)
        self.stop()


def mint_token(principal: str, role: str) -> str:
    raw = kubectl(
        [
            "-n",
            NAMESPACE,
            "exec",
            "deployment/crucible-api",
            "--",
            "crucible",
            "admin",
            "--reason",
            "kubernetes deploy smoke principal",
            "token",
            "create",
            "--principal",
            principal,
            "--role",
            role,
        ],
        redact=True,
    )
    try:
        return minted_token(json.loads(raw))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SmokeError(
            "`crucible admin token create` did not return a JSON object with a `token` "
            f"field ({exc}). Its output is withheld because it carries a token."
        ) from None


def seed_origin(image: str) -> None:
    """Create the throwaway origin repository on the reference-cache claim.

    The preparer clones from `/crucible/cache` (26), so the origin is put there by a Pod
    that mounts the same claim in the same namespace under the same `restricted`
    admission the workers run under. Nothing reaches the cluster's node: there is no
    hostPath anywhere in this run, which is what makes the claim the manifests describe
    the one being exercised.
    """
    script = "\n".join(
        [
            "set -eu",
            "export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1",
            "export GIT_AUTHOR_NAME=kind GIT_AUTHOR_EMAIL=kind@example.invalid",
            "export GIT_COMMITTER_NAME=kind GIT_COMMITTER_EMAIL=kind@example.invalid",
            "rm -rf /tmp/work",
            "mkdir -p /tmp/work/src /tmp/work/checks",
            "printf 'succeed\\n' > /tmp/work/e2e-behavior",
            "printf '# kind-deploy\\n' > /tmp/work/README.md",
            "printf 'base\\n' > /tmp/work/src/app.txt",
            *[f"printf '{body}\\n' > /tmp/work/checks/{name}" for name, body in CHECKS.items()],
            "cd /tmp/work",
            "git init -q -b main .",
            "git add -A",
            "git commit -q -m 'kind deploy fixture'",
            f"rm -rf /crucible/cache/{ORIGIN}.git",
            f"git clone -q --bare /tmp/work /crucible/cache/{ORIGIN}.git",
            f"git --git-dir /crucible/cache/{ORIGIN}.git config remote.origin.url {ORIGIN_URL}",
            "echo seeded",
        ]
    )
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "crucible-origin-seed", "namespace": WORKERS_NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "serviceAccountName": "crucible-worker",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "seed",
                    "image": image,
                    "command": ["sh", "-c", script],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "env": [{"name": "HOME", "value": "/home/worker"}],
                    # The namespace ResourceQuota demands both on every container, which
                    # is 26's concurrency bound doing its job on a Pod it never saw.
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "500m", "memory": "256Mi"},
                    },
                    "volumeMounts": [
                        {"name": "cache", "mountPath": "/crucible/cache"},
                        {"name": "tmp", "mountPath": "/tmp"},
                        {"name": "home", "mountPath": "/home/worker"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "cache",
                    "persistentVolumeClaim": {"claimName": "crucible-reference-cache"},
                },
                {"name": "tmp", "emptyDir": {}},
                {"name": "home", "emptyDir": {}},
            ],
        },
    }
    kubectl(
        ["-n", WORKERS_NAMESPACE, "delete", "pod", "crucible-origin-seed", "--ignore-not-found"]
    )
    apply = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(pod),
        capture_output=True,
        text=True,
        check=False,
    )
    if apply.returncode != 0:
        raise SmokeError(f"the origin seed Pod was refused:\n{apply.stderr.strip()}")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        phase = kubectl(
            [
                "-n",
                WORKERS_NAMESPACE,
                "get",
                "pod",
                "crucible-origin-seed",
                "-o",
                "jsonpath={.status.phase}",
            ]
        ).strip()
        if phase == "Succeeded":
            log(f"origin seeded at {ORIGIN_URL}")
            kubectl(["-n", WORKERS_NAMESPACE, "delete", "pod", "crucible-origin-seed"])
            return
        if phase == "Failed":
            logs = kubectl(["-n", WORKERS_NAMESPACE, "logs", "crucible-origin-seed"], check=False)
            raise SmokeError(f"the origin seed Pod failed:\n{logs}")
        time.sleep(2)
    described = kubectl(
        ["-n", WORKERS_NAMESPACE, "describe", "pod", "crucible-origin-seed"], check=False
    )
    raise SmokeError(f"the origin seed Pod never finished:\n{described}")


def routing_document() -> dict[str, Any]:
    """One entry: the script harness, no model, no cost (05b)."""
    return {
        "schema_version": "1.0",
        "name": ROUTING,
        "version": 1,
        "tiers": {
            tier: {"allowed_capability": ["small"], "prefer": ["small"]}
            for tier in ("trivial", "standard", "complex")
        },
        "models": [
            {
                "id": "none",
                "harness": "script-harness",
                "endpoint": "subscription",
                "capability": "small",
                "cost": "none",
                "speed": "fast",
                "pool": "kind",
                "weight": 1,
                "enabled": True,
            }
        ],
        "pools": {"kind": {"window": "1h", "budget_units": "attempts", "soft_limit": 0}},
        "rotation": {
            "strategy": "weighted-least-recent",
            "quality_feedback": False,
            "quality_window": 10,
        },
    }


def policy_document(base: dict[str, Any]) -> dict[str, Any]:
    """The deployment's own policy, derived from the shipped default rather than copied.

    Deriving is what keeps this file from being a second, drifting definition of the
    policy schema: a field 05b adds arrives here without an edit.
    """
    document: dict[str, Any] = json.loads(json.dumps(base))
    document["name"] = POLICY
    document["version"] = 1
    document["description"] = "The script harness on the Kubernetes provider (C9)."
    document["repository"]["required_checks"] = [f"sh checks/{name}" for name in CHECKS]
    # The worker image lives in the run's disposable registry, which is why the
    # allowlist is a pattern and not the shipped `crucible-worker:*`.
    document["images"]["allowlist"] = ["*/crucible-worker:*"]
    document["limits"]["grace_seconds"] = 5
    document["limits"]["timeout_seconds"] = {"min": 5, "max": 3600, "default": 600}
    document["resources"] = {
        "cpus": 1,
        "memory": "512MiB",
        "pids": 256,
        "tmpfs_total": "256MiB",
    }
    # The worker needs no egress at all: its origin is the cache claim and its harness
    # is a script. 26 then writes no NetworkPolicy for it, and the namespace default
    # deny is its whole answer.
    document["network"]["egress_allowlist"] = []
    document["routing"] = {"policy": {"name": ROUTING, "version": 1}}
    document["concurrency"]["per_harness"] = {
        **document["concurrency"]["per_harness"],
        "script-harness": 1,
    }
    document["cleanup"]["workspace_on_success"] = "keep_diff_only"
    return document


def shipped_policy(base_url: str, token: str) -> dict[str, Any]:
    for version in range(9, 0, -1):
        try:
            view = request("GET", f"{base_url}/v1/policies/default-software/{version}", token=token)
        except SmokeError:
            continue
        log(f"deriving the smoke policy from default-software version {version}")
        return dict(field(view, "document", "GET /v1/policies/default-software"))
    raise SmokeError("the deployed database carries no default-software policy to derive from")


def task_contract(external_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "external_id": external_id,
        "title": "Kubernetes deployment smoke",
        "project": ORIGIN,
        "parent_external_id": None,
        "repository": {
            "name": ORIGIN,
            "base_ref": "main",
            "work_branch": f"crucible/{external_id}",
        },
        "scope": {
            "allowed_paths": ["src/**", "checks/**"],
            "prohibited_paths": [".github/**"],
            "may_add_dependencies": False,
            "may_modify_ci": False,
        },
        "objective": "Prove the deployment manifests run a task on the Kubernetes provider.",
        "context": [],
        "project_instructions": [],
        "acceptance_criteria": [{"id": "AC1", "text": "The task reaches awaiting_acceptance."}],
        "required_verification": [
            {"id": "V1", "command": "sh checks/lint.sh", "expect_exit": 0},
            {"id": "V2", "command": "sh checks/test.sh", "expect_exit": 0},
            {"id": "V3", "command": "sh checks/scan.sh", "expect_exit": 0},
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
        "policy": {"name": POLICY, "version": 1},
        # No image: the tier and the routing policy select the harness, and the promoted
        # image is what the provider resolves for it (C6b, 13).
        "execution_request": {
            "tier": "trivial",
            "effort": None,
            "provider": "kubernetes",
            "timeout_seconds": 600,
            "rationale": "kubernetes deployment smoke",
        },
        "lifecycle": {"max_attempts": 1, "retry_on": [], "cleanup": "policy"},
        "correction": None,
    }


def await_supervisor(base_url: str, token: str) -> None:
    """Every administrative mutation needs the supervisor's lease to be live (25)."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        view = request("GET", f"{base_url}/v1/supervisor", token=token)
        if view and view.get("healthy"):
            log("the supervisor lease is live")
            return
        time.sleep(3)
    raise SmokeError("the deployed supervisor never took its lease")


def promote_worker_image(base_url: str, token: str) -> dict[str, Any]:
    """25 and 13: the provider reports what the registry holds; promotion is an admin act,
    and it is per harness (ADR 0018), so it names the script harness."""
    deadline = time.monotonic() + 120
    while True:
        listing = request("GET", f"{base_url}/v1/admin/images", token=token)
        items = [
            item
            for item in field(listing, "items", "GET /v1/admin/images")
            if "script-harness" in (item.get("harnesses") or {})
        ]
        if items:
            break
        if time.monotonic() > deadline:
            raise SmokeError(
                "the deployed provider sees no script-harness image in the configured "
                f"repositories: {json.dumps(listing)[:1000]}"
            )
        time.sleep(3)
    image = items[0]
    digest = field(image, "digest", "GET /v1/admin/images")
    request(
        "POST",
        f"{base_url}/v1/admin/images/{digest}/promote",
        token=token,
        body={
            "harness": "script-harness",
            "reason": "kind deployment smoke: the script harness image under test",
        },
    )
    log(f"promoted {image.get('reference')} ({digest})")
    return dict(image)


def provider_checks(base_url: str, token: str) -> dict[str, Any]:
    """26 and 25: the status page's Kubernetes row, which is the namespace readiness
    probe, the CNI egress enforcement result, the pod PID limit and the runtime class."""
    document = request("GET", f"{base_url}/v1/admin/status", token=token)
    providers = field(document, "providers", "GET /v1/admin/status")
    for provider in providers:
        if provider.get("name") == "kubernetes":
            return dict(provider)
    raise SmokeError(
        "the deployed status document reports no kubernetes provider: "
        f"{json.dumps([p.get('name') for p in providers])}"
    )


def assert_status_page(base_url: str, token: str) -> dict[str, Any]:
    provider = provider_checks(base_url, token)
    log("status page, providers.kubernetes:")
    log(json.dumps(provider, indent=2, sort_keys=True))
    checks = field(provider, "checks", "the kubernetes provider row")
    if provider.get("health") != "ok":
        raise SmokeError(f"the Kubernetes provider is {provider.get('health')!r}, not 'ok'")
    if checks.get("namespace_ready") is not True:
        raise SmokeError("26's namespace readiness probe did not pass on the deployed cluster")
    if checks.get("egress_enforced") is not True:
        raise SmokeError("the CNI does not enforce egress NetworkPolicy on this cluster")
    if not checks.get("pod_pid_limit"):
        raise SmokeError("the node reports no pod PID limit, so 26 refuses to launch")
    capabilities = field(provider, "capabilities", "the kubernetes provider row")
    if capabilities.get("isolation") != "pod":
        raise SmokeError(f"the provider reports isolation {capabilities.get('isolation')!r}")
    return provider


FIRST_RUN_SECRET = "crucible-first-run-admin"
TOKEN_PATTERN = re.compile(r"\bcru_[A-Z0-9]{26}\.[A-Za-z0-9_-]+\b")


def first_run_token() -> str:
    """The first-run administrator token, from the Secret the migrate Job wrote (ADR
    0016), after proving the Job's log does not carry it (crucible#122). The token is
    never printed by this process."""
    logs = kubectl(["-n", NAMESPACE, "logs", "job/crucible-migrate"], redact=True, check=False)
    if TOKEN_PATTERN.search(logs):
        raise SmokeError("the migrate Job's log carries a token (crucible#122)")
    if FIRST_RUN_SECRET not in logs:
        raise SmokeError("the migrate Job's log does not say where the first-run token is")
    encoded = kubectl(
        [
            "-n",
            NAMESPACE,
            "get",
            "secret",
            FIRST_RUN_SECRET,
            "-o",
            "jsonpath={.data.token}",
        ],
        redact=True,
    )
    try:
        token = base64.b64decode(encoded.strip()).decode("ascii").strip()
    except ValueError:
        raise SmokeError(f"the Secret {FIRST_RUN_SECRET} holds no readable token") from None
    if not TOKEN_PATTERN.fullmatch(token):
        raise SmokeError(f"the Secret {FIRST_RUN_SECRET} does not hold a Crucible token")
    log(f"the migrate Job's log has no token; the Secret {FIRST_RUN_SECRET} holds one")
    return token


def await_first_run_secret_gone(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = kubectl(
            [
                "-n",
                NAMESPACE,
                "get",
                "secret",
                FIRST_RUN_SECRET,
                "--ignore-not-found",
                "-o",
                "name",
            ],
            check=False,
        )
        if not found.strip():
            log(f"the first sign-in removed the Secret {FIRST_RUN_SECRET}")
            return
        time.sleep(1)
    raise SmokeError(f"the Secret {FIRST_RUN_SECRET} is still there after the first sign-in")


def walk_status_ui(base_url: str) -> None:
    """The rendered page, not only the document behind it (25, operator rule 7).

    Signs in with the first-run administrator token from its Secret, which the sign-in
    then removes (ADR 0016). The token is never printed by this process.
    """
    token = first_run_token()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{base_url}/ui/sign-in", timeout=DEFAULT_TIMEOUT) as response:
        sign_in = response.read().decode("utf-8", "replace")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', sign_in)
    if csrf is None:
        raise SmokeError("the first-run sign-in page has no pre-authentication CSRF nonce")
    if FIRST_RUN_SECRET not in sign_in or "logs migrate" in sign_in:
        raise SmokeError("the sign-in page does not name the first-run Secret")
    body = urllib.parse.urlencode({"csrf": csrf.group(1), "token": token, "next": "/ui"}).encode()
    post = urllib.request.Request(
        f"{base_url}/ui/sign-in",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(post, timeout=DEFAULT_TIMEOUT) as response:
            page = response.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise SmokeError(f"the first-run administrator sign-in failed: {exc}") from None
    if b'data-page="status"' not in page:
        raise SmokeError("the first-run administrator sign-in did not render the Status page")
    if "kubernetes" not in page:
        raise SmokeError("the rendered status page does not name the kubernetes provider")
    log("the rendered /ui status page names the kubernetes provider")
    await_first_run_secret_gone()


def task_events(base_url: str, token: str, task_id: str) -> str:
    """What Crucible recorded, so a dead end says why and not only that it happened."""
    try:
        events = request("GET", f"{base_url}/v1/tasks/{task_id}/events?limit=200", token=token)
    except SmokeError as exc:
        return f"(the event log could not be read: {exc})"
    lines = []
    for event in field(events, "items", "GET /v1/tasks/{id}/events"):
        payload = json.dumps(event.get("payload"))
        lines.append(f"  {event.get('seq')} {event.get('kind')} {payload[:600]}")
    return "\n".join(lines)


def await_state(base_url: str, token: str, task_id: str, wanted: str) -> Any:
    where = f"GET /v1/tasks/{task_id}"
    state = "unknown"
    for attempt in range(STATE_ATTEMPTS):
        task = request("GET", f"{base_url}/v1/tasks/{task_id}", token=token)
        state = field(task, "state", where)
        if state == wanted:
            log(f"state: {state}")
            return task
        if state in DEAD_END_STATES:
            raise SmokeError(
                f"task {task_id} reached {state!r}, which never becomes {wanted!r}.\n"
                f"Gate summary: {json.dumps(task.get('gate_summary'))}\n"
                f"Events:\n{task_events(base_url, token, task_id)}"
            )
        if attempt < STATE_ATTEMPTS - 1:
            time.sleep(STATE_POLL_SECONDS)
    raise SmokeError(
        f"task {task_id} is still in {state!r} after "
        f"{int((STATE_ATTEMPTS - 1) * STATE_POLL_SECONDS)}s; wanted {wanted!r}."
    )


def run_task(base_url: str, admin: str, operator: str, external_id: str, principal: str) -> str:
    task = request("POST", f"{base_url}/v1/tasks", token=operator, body=task_contract(external_id))
    task_id = str(field(task, "id", "POST /v1/tasks"))
    log(f"submitted task {task_id}")
    request(
        "POST",
        f"{base_url}/v1/tasks/{task_id}/start",
        token=operator,
        body={"provider": "kubernetes", "policy_version": 1},
    )
    log("started on the kubernetes provider")

    task = await_state(base_url, operator, task_id, "awaiting_internal_review")
    where = f"GET /v1/tasks/{task_id}"
    summary = field(task, "gate_summary", where)
    log(f"gate summary: {json.dumps(summary, indent=2, sort_keys=True)}")
    failing = field(summary, "failing", f"{where} (gate_summary)")
    if failing:
        raise SmokeError(f"gates failed on the collected head: {json.dumps(failing)}")

    request(
        "POST",
        f"{base_url}/v1/tasks/{task_id}/review",
        token=operator,
        body={
            "report": {
                "schema_version": "1.0",
                "task_external_id": external_id,
                "reviewed_head_sha": field(task, "head_sha", where),
                "reviewer": {"kind": "orchestrator", "principal": principal},
                "verdict": "approve",
                "findings": [],
                "summary": "Kubernetes deployment smoke non-author review.",
            }
        },
    )
    log("internal review recorded")
    await_state(base_url, operator, task_id, "awaiting_acceptance")
    request(
        "POST",
        f"{base_url}/v1/tasks/{task_id}/accept",
        token=operator,
        body={
            "verdict": "accepted",
            "reasoning": (
                "Kubernetes deployment smoke: the gates passed and the review is recorded."
            ),
        },
    )
    await_state(base_url, operator, task_id, "accepted")

    attempt = field(
        request("GET", f"{base_url}/v1/tasks/{task_id}", token=operator),
        "latest_attempt",
        where,
    )
    log("attempt evidence:")
    log(
        json.dumps(
            {k: attempt.get(k) for k in ("id", "state", "exit_code", "image_digest")}, indent=2
        )
    )
    del admin
    return task_id


def smoke(worker_image: str, external_id: str, principal: str) -> None:
    if not os.environ.get("KUBECONFIG"):
        raise SmokeError("KUBECONFIG must name the disposable cluster's kubeconfig")
    seed_origin(worker_image)
    with PortForward() as base_url:
        log(f"base URL: {base_url}")
        log(f"health: {json.dumps(request('GET', f'{base_url}/v1/health'))}")
        request("GET", f"{base_url}/v1/ready")
        log("ready")

        admin = mint_token(f"{principal}-admin", "admin")
        operator = mint_token(principal, "operator")
        await_supervisor(base_url, admin)

        assert_status_page(base_url, admin)
        walk_status_ui(base_url)

        request(
            "PUT",
            f"{base_url}/v1/routing/{ROUTING}/1",
            token=admin,
            body=routing_document(),
        )
        request(
            "PUT",
            f"{base_url}/v1/policies/{POLICY}/1",
            token=admin,
            body=policy_document(shipped_policy(base_url, admin)),
        )
        log("routing and policy uploaded")
        promote_worker_image(base_url, admin)
        request(
            "PUT",
            f"{base_url}/v1/admin/repositories/{ORIGIN}",
            token=admin,
            body={
                "url": ORIGIN_URL,
                "default_branch": "main",
                "policy_name": POLICY,
                "attested_all_prs": True,
                "attested_by": "kubernetes-deploy-smoke",
                "reason": "kind deployment smoke: the throwaway origin on the cache claim",
            },
        )
        log("repository registered")

        run_task(base_url, admin, operator, external_id, principal)
        # Re-read the provider row after a real attempt: the probe, the quota-derived
        # concurrency and the enforcement result all have to still hold afterwards.
        assert_status_page(base_url, admin)
        log("kubernetes deployment smoke passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--worker-image",
        default=os.environ.get("CRUCIBLE_DEPLOY_KIND_WORKER_IMAGE"),
        help="the script-harness image reference as the cluster sees it",
    )
    run_id = str(int(time.time()))
    parser.add_argument("--external-id", default=f"KIND-DEPLOY-{run_id}")
    parser.add_argument("--principal", default=f"kind-deploy-{run_id}")
    args = parser.parse_args(argv)
    if not args.worker_image:
        print("set --worker-image or CRUCIBLE_DEPLOY_KIND_WORKER_IMAGE", file=sys.stderr)
        return 2
    try:
        smoke(args.worker_image, args.external_id, args.principal)
    except SmokeError as exc:
        print(f"kubernetes deployment smoke failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
