"""The live harness tier (07, 12, 18): the real subscription and Hermes images,
one harness at a time, local only, never in CI.

Per harness, a trivial task (add one line to a file and report) runs in the hardened
image with the credential seeded into a uid-1000 per-attempt copy, reaches a collected
head, passes the gates, is accepted, is published to the throwaway repository, and
reaches `ready_for_merge` (`allow_no_ci` and `required_rounds: 0`, as the C4 live tier
does). Cleanup closes the pull request and deletes the branch; nothing is merged and
`main` is never touched.

What is recorded, never a value: the exact time of the run, the model requested and the
model the transcript named, the duration, the exit class, whether each named auth file
changed during the run (by hash), the mount mode, and the state reached. The absence
proof reads the credential values into memory once and asserts none of them appears in
the worker's create request, the events, every text column of the database, the log
chunks, the artifact store, or the pull request body.

Inputs, all paths, names or flags:

    CRUCIBLE_LIVE_CREDENTIAL_ROOT   the dedicated root holding claude_code/, codex/, agy/
    CRUCIBLE_LIVE_HARNESSES         comma list; default all three
    CRUCIBLE_LIVE_MODELS            optional JSON {harness: model} overriding the defaults
    CRUCIBLE_LIVE_IMAGES            optional JSON {harness: image tag}; default the pin
                                    in images/manifest.env (never "the newest": every
                                    reproducible image has the same creation time)
    CRUCIBLE_LIVE_AGY_MOUNT_MODE    ro (the adapter's minimum) or rw-narrow; default
                                    rw-narrow so a token refresh is observed, not lost
    CRUCIBLE_LIVE_REPORT            where the JSON summary of every run is appended
    plus the C4 live tier's CRUCIBLE_GITHUB_APP_JSON, CRUCIBLE_GITHUB_APP_KEY and
    CRUCIBLE_GITHUB_TARGET_REPO for the publication half.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.publisher import DockerPublisher, PublisherConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.harness.registry import default_registry
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.harnesses import set_harness_enabled
from crucible.application.supervisor import Supervisor
from crucible.domain.secrets import scan_text
from crucible.ports.execution import image_harnesses
from crucible.ports.harness import CredentialSource, MountMode
from tests.e2e import daemon, github_live
from tests.e2e.conftest import (
    NET_WORKERS,
    RUN_ID,
    e2e_contract,
    submit_and_start,
    upload_review,
)
from tests.e2e.github_live import LiveConfig
from tests.e2e.policy import e2e_routing_document
from tests.e2e.test_github_live import live_policy, register
from tests.fixtures import promote_for_test

CREDENTIAL_ROOT_ENV = "CRUCIBLE_LIVE_CREDENTIAL_ROOT"
HARNESSES_ENV = "CRUCIBLE_LIVE_HARNESSES"
MODELS_ENV = "CRUCIBLE_LIVE_MODELS"
AGY_MODE_ENV = "CRUCIBLE_LIVE_AGY_MOUNT_MODE"
REPORT_ENV = "CRUCIBLE_LIVE_REPORT"
LOCAL_ENDPOINT_ENV = "CRUCIBLE_LOCAL_ENDPOINT_URL"
LOCAL_TZ = ZoneInfo("America/Chicago")

# The cheapest model each harness offers that completes a trivial task: routing's rule
# that trivial work never goes to a frontier model (05b). Overridable per run.
# Verified by the C5 live runs. Codex with a ChatGPT-plan login refuses the codex-suffixed
# ids and gpt-5.6; the 5.6 family needs the code-mode host the image carries since the
# correction round. AGY's effort is part of the id.
DEFAULT_MODELS = {
    "claude_code": "claude-haiku-4-5",
    "codex": "gpt-5.6-luna",
    "agy": "gemini-3.8-flash-low",
    "hermes": "coder",
}
IMAGES_ENV = "CRUCIBLE_LIVE_IMAGES"
# AGY 1.2.4 refuses --effort for gemini-3.1-flash-lite (found live), so none is passed.
EFFORT = {"claude_code": None, "codex": "low", "agy": None, "hermes": None}
ALL_HARNESSES = ("claude_code", "codex", "agy", "hermes")


def _selected() -> tuple[str, ...]:
    raw = os.environ.get(HARNESSES_ENV, "").strip()
    if not raw or raw == "all":
        return ALL_HARNESSES
    return tuple(h.strip() for h in raw.split(",") if h.strip())


def _why_not_configured() -> str:
    root = os.environ.get(CREDENTIAL_ROOT_ENV, "")
    if not root:
        return f"set {CREDENTIAL_ROOT_ENV} to the dedicated Crucible credential root (12)"
    if root and not Path(root).is_dir():
        return f"{CREDENTIAL_ROOT_ENV} does not name a directory"
    unknown = sorted(set(_selected()) - set(ALL_HARNESSES))
    if unknown:
        return f"{HARNESSES_ENV} names unknown harnesses: {unknown}"
    if "hermes" in _selected() and not os.environ.get(LOCAL_ENDPOINT_ENV, "").strip():
        return f"set {LOCAL_ENDPOINT_ENV} to the OpenAI-compatible /v1 endpoint"
    return github_live.why_not_configured()


NOT_CONFIGURED = _why_not_configured()
pytestmark = [
    pytest.mark.e2e_live,
    # A real harness does real work against a real model, local only (issue 192).
    pytest.mark.timeout(3600),
    pytest.mark.skipif(bool(NOT_CONFIGURED), reason=NOT_CONFIGURED or "configured"),
]

POLL = DeliveryConfig(poll_interval_seconds=0, reactions_poll_interval_seconds=0)


def _models() -> dict[str, str]:
    override = os.environ.get(MODELS_ENV, "").strip()
    models = dict(DEFAULT_MODELS)
    if override:
        models.update({str(k): str(v) for k, v in json.loads(override).items()})
    return models


def _images() -> dict[str, str]:
    override = os.environ.get(IMAGES_ENV, "").strip()
    return {str(k): str(v) for k, v in json.loads(override).items()} if override else {}


def _local(moment: datetime) -> str:
    return moment.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _sources(root: Path) -> dict[str, CredentialSource]:
    agy_mode = MountMode(os.environ.get(AGY_MODE_ENV, "").strip() or "rw-narrow")
    return {
        "claude_code": CredentialSource(str(root / "claude_code")),
        "codex": CredentialSource(str(root / "codex")),
        "agy": CredentialSource(str(root / "agy"), agy_mode),
        "hermes": CredentialSource(str(root / "hermes"), MountMode.RO),
    }


def _secret_values(root: Path, harness: str) -> list[str]:
    """The credential values, in memory only, for the absence proof. Never printed."""
    registry = default_registry()
    spec = registry.require(harness).credential_spec()
    if spec is None:
        return []
    values: list[str] = []
    for auth in spec.auth_files:
        path = spec.source_path(str(root / harness), auth.name)
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        if auth.json:
            try:
                document = json.loads(raw)
            except ValueError:
                document = None
            for value in _leaves(document):
                if isinstance(value, str) and len(value) >= 20:
                    values.append(value)
        else:
            values.append(raw.strip())
    return [v for v in values if v]


def _leaves(document: Any) -> list[Any]:
    if isinstance(document, dict):
        return [leaf for value in document.values() for leaf in _leaves(value)]
    if isinstance(document, list):
        return [leaf for value in document for leaf in _leaves(value)]
    return [document]


@pytest.fixture(scope="session")
def live_config() -> LiveConfig:
    return github_live.load_config()


@pytest.fixture(scope="session")
def credential_root() -> Path:
    return Path(os.environ.get(CREDENTIAL_ROOT_ENV, "/credential-not-required"))


@pytest.fixture
def github(live_config: LiveConfig) -> RestGitHubClient:
    return github_live.client(live_config)


@pytest.fixture
def mirror(artifact_root: Path, live_config: LiveConfig) -> str:
    return github_live.seed_mirror(artifact_root / "e2e-repos", live_config)


@pytest.fixture
def cleanup(github: RestGitHubClient, live_config: LiveConfig) -> Any:
    record = github_live.Cleanup(config=live_config, github=github, branches=[], pull_requests=[])
    yield record
    result = record.run()
    print(f"live cleanup: {result}")
    assert not result["errors"], result["errors"]


@pytest.fixture
def live_provider(
    docker_config: DockerConfig, credential_root: Path, stack: dict[str, Any]
) -> DockerProvider:
    """The e2e provider with the dedicated credential directories configured (12) and the
    proxy allowlist the stack's squid was brought up with."""
    registry = default_registry()
    endpoints = {h for a in registry for h in a.capabilities().endpoints}
    config = replace(
        docker_config,
        credentials=_sources(credential_root),
        proxy_allowlist=tuple(sorted(set(docker_config.proxy_allowlist) | endpoints)),
    )
    return DockerProvider(config, harnesses=registry)


@pytest.fixture
def live_ctx(
    engine: Engine, migrated: str, artifact_root: Path, live_provider: DockerProvider
) -> AppContext:
    from crucible.adapters.clock import SystemClock  # noqa: PLC0415
    from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory  # noqa: PLC0415
    from crucible.adapters.storage.disk import DiskArtifactStore  # noqa: PLC0415

    return AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=SystemClock(),
        providers=[live_provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "store"),
        harnesses=live_provider.harnesses,
        credential_sources=dict(live_provider.config.credentials),
    )


@pytest.fixture
def live_tokens(live_ctx: AppContext) -> dict[str, str]:
    from crucible.application.auth import mint_token  # noqa: PLC0415
    from crucible.domain.entities import Role  # noqa: PLC0415

    out: dict[str, str] = {}
    with live_ctx.uow_factory() as uow:
        for role in Role:
            out[role.value] = mint_token(
                uow, live_ctx.clock, name=f"{role.value}-principal", role=role
            ).token
        uow.commit()
    return out


def _routing(models: dict[str, str]) -> dict[str, Any]:
    document = e2e_routing_document()
    for harness, model in models.items():
        local = harness == "hermes"
        document["models"].append(
            {
                "id": model,
                "harness": harness,
                "endpoint": "local" if local else "subscription",
                **({"endpoint_url": os.environ[LOCAL_ENDPOINT_ENV]} if local else {}),
                "capability": "small",
                "cost": "none" if local else "low",
                "speed": "fast",
                "pool": "lab-local" if local else "e2e",
                "weight": 1,
                "enabled": True,
            }
        )
    document["pools"]["lab-local"] = {
        "window": "1h",
        "budget_units": "attempts",
        "soft_limit": 0,
        "max_concurrency": 4,
    }
    return document


def _policy() -> dict[str, Any]:
    document = live_policy()
    registry = default_registry()
    endpoints = sorted({h for a in registry for h in a.capabilities().endpoints})
    document["network"]["egress_allowlist"] = sorted({"github.com", *endpoints})
    document["concurrency"]["per_harness"] = {h: 1 for h in (*ALL_HARNESSES, "script-harness")}
    # S1: the real CLIs want memory and processes a script does not.
    document["resources"] = {"cpus": 2, "memory": "3GiB", "pids": 1024, "tmpfs_total": "2GiB"}
    document["limits"]["timeout_seconds"] = {"min": 5, "max": 3600, "default": 900}
    return document


@pytest.fixture
def live_client(
    live_ctx: AppContext,
    live_tokens: dict[str, str],
    live_provider: DockerProvider,
) -> Any:
    app = create_app(live_ctx)
    models = _models()
    with TestClient(app, headers={"Authorization": f"Bearer {live_tokens['admin']}"}) as admin:
        routing = _routing({harness: models[harness] for harness in _selected()})
        response = admin.put(f"/v1/routing/{routing['name']}/{routing['version']}", json=routing)
        assert response.status_code in (200, 201), response.text
        policy = _policy()
        response = admin.put(f"/v1/policies/{policy['name']}/{policy['version']}", json=policy)
        assert response.status_code in (200, 201), response.text
        available = {image.reference: image for image in asyncio.run(live_provider.list_images())}
        overrides = _images()
        with live_ctx.uow_factory() as uow:
            for harness in _selected():
                reference = overrides.get(harness) or daemon.worker_image()
                image = available[reference]
                promote_for_test(
                    uow,
                    digest=image.digest,
                    reference=image.reference,
                    harnesses=dict(image.harnesses),
                    at=live_ctx.clock.now(),
                    by="e2e-live",
                    reason="live harness image selected for this test run",
                )
            uow.commit()
    # The live contract pins the exact harness and model under test, which is an
    # operator-only submission.
    with TestClient(app, headers={"Authorization": f"Bearer {live_tokens['operator']}"}) as c:
        yield c


@pytest.fixture
def live_supervisor(
    live_ctx: AppContext,
    live_provider: DockerProvider,
    github: RestGitHubClient,
    stack: dict[str, Any],
) -> Supervisor:
    publisher = DockerPublisher(
        live_provider,
        PublisherConfig(
            network=NET_WORKERS, egress_proxy=str(stack["egress_proxy"]), timeout_seconds=300
        ),
    )
    return Supervisor(
        live_ctx.uow_factory,
        {"docker": live_provider},
        live_ctx.clock,
        holder=f"e2e-live-{RUN_ID}",
        artifact_store=live_ctx.artifact_store,
        github=github,
        publisher=publisher,
        delivery_config=POLL,
        lease_ttl_seconds=600,
        grace_seconds=15,
        harnesses=live_provider.harnesses,
        credential_sources=dict(live_provider.config.credentials),
    )


def _enable(live_ctx: AppContext, harness: str) -> None:
    """The tier enables the harness under test in its own scratch database with a
    recorded, test-only reason. This never enables it for production (12, S1b)."""
    with live_ctx.uow_factory() as uow:
        set_harness_enabled(
            uow,
            live_ctx.clock,
            principal_name="e2e-live",
            name=harness,
            enabled=True,
            reason=(
                "e2e-live: test-only enablement in the tier's scratch database to exercise "
                "the adapter live; production enablement waits on S1b steps 5 and 6"
            ),
        )
        uow.commit()


OBJECTIVE = """Make exactly this change and nothing else.

1. Append one line to the file `notes/c5-live.txt` in the checkout at /crucible/repo,
   creating the file if it does not exist. The line is:
   `{harness} completed a Crucible live run for task {external_id}.`
2. Commit that one change on the current branch with `git add notes/c5-live.txt` and
   `git commit -m "c5 live run for {external_id}" \
   --trailer "Crucible-Attempt: $CRUCIBLE_ATTEMPT_ID"` (the environment variable
   CRUCIBLE_ATTEMPT_ID is set; the git author is already configured; never push).
3. Run the four verification commands listed in section 6 of IDENTITY.md and write each
   command's output to /crucible/report/<id>.log, for example /crucible/report/V1.log.
4. Write /crucible/report/run-evidence.md with two lines: the commit sha from
   `git rev-parse HEAD`, and the harness name.
5. Write /crucible/report/report.yaml exactly matching CompletionClaimV1 (the schema is
   /crucible/identity/report-schema.json). Use schema_version "1.0", task_external_id
   "{external_id}", changed_files ["notes/c5-live.txt"], refs.branch from
   `git rev-parse --abbrev-ref HEAD`, refs.head_sha from `git rev-parse HEAD`,
   refs.commits 1, one checks entry per verification command with its exit code and
   log file name, acceptance_mapping with id AC1 status met evidence run-evidence.md,
   self_review with documentation naming notes/c5-live.txt, acceptance_criteria mapping
   AC1 with status met and evidence run-evidence.md, and omissions [],
   run_evidence ["run-evidence.md"], proposed_pull_request with title
   "c5 live run for {external_id}", an empty body, and closes []; empty lists for
   limitations, risks, blockers and follow_ups.
6. Exit 0. Do not open a pull request, do not push, do not touch any other file.
"""

HERMES_SCRIPT = r"""import os
import subprocess
from pathlib import Path
import yaml

external_id = "{external_id}"
repo = Path("/crucible/repo")
report = Path("/crucible/report")
note = repo / "notes/c5-live.txt"
note.parent.mkdir(parents=True, exist_ok=True)
with note.open("a", encoding="utf-8") as handle:
    handle.write("hermes completed a Crucible live run for task {external_id}.\n")
subprocess.run(["git", "add", "notes/c5-live.txt"], cwd=repo, check=True)
subprocess.run(
    ["git", "commit", "-m", f"c5 live run for {{external_id}}", "--trailer",
     f"Crucible-Attempt: {{os.environ['CRUCIBLE_ATTEMPT_ID']}}"],
    cwd=repo,
    check=True,
)
report.mkdir(parents=True, exist_ok=True)
for check_id, text in (("V1", "lint ok"), ("V2", "test ok"), ("V3", "scan ok")):
    (report / f"{{check_id}}.log").write_text(text + "\n", encoding="utf-8")
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
branch = subprocess.check_output(
    ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
).strip()
(report / "run-evidence.md").write_text(head + "\nhermes\n", encoding="utf-8")
claim = {{
    "schema_version": "1.0",
    "task_external_id": external_id,
    "summary": "Hermes appended and committed the required live-run line.",
    "self_review": {{
        "documentation": ["Updated notes/c5-live.txt."],
        "acceptance_criteria": [
            {{"id": "AC1", "status": "met", "evidence": "run-evidence.md"}}
        ],
        "omissions": []
    }},
    "changed_files": ["notes/c5-live.txt"],
    "refs": {{"branch": branch, "head_sha": head, "commits": 1}},
    "checks": [
        {{"id": check_id, "command": f"echo {{text}}", "exit": 0,
         "log": f"{{check_id}}.log"}}
        for check_id, text in (("V1", "lint ok"), ("V2", "test ok"), ("V3", "scan ok"))
    ],
    "acceptance_mapping": [
        {{"id": "AC1", "status": "met", "evidence": "run-evidence.md"}}
    ],
    "run_evidence": ["run-evidence.md"],
    "proposed_pull_request": {{
        "title": f"c5 live run for {{external_id}}", "body": "", "closes": []
    }},
    "limitations": [],
    "risks": [],
    "blockers": [],
    "follow_ups": [],
}}
(report / "report.yaml").write_text(yaml.safe_dump(claim, sort_keys=False), encoding="utf-8")
"""


def _hermes_objective(external_id: str) -> str:
    script = HERMES_SCRIPT.format(external_id=external_id)
    return f"""Complete the task with one terminal tool call. Do not use the file tool,
inspect other files, or split this into multiple calls. Run this exact command:

python3 - <<'PY'
{script}PY

After that one command succeeds, exit without calling another tool.
"""


def _contract(harness: str, model: str, image: str, config: LiveConfig, external_id: str) -> Any:
    document = e2e_contract(external_id, config.repository, image)
    # No colon in the title: an unquoted colon inside a YAML value is the one thing a
    # small model gets wrong most, and the tier proves the pipeline, not YAML quoting.
    document["title"] = f"c5 live run for {external_id}"
    document["objective"] = (
        _hermes_objective(external_id)
        if harness == "hermes"
        else OBJECTIVE.format(harness=harness, external_id=external_id)
    )
    document["repository"]["work_branch"] = f"crucible/C5-{external_id}"
    document["scope"] = {
        "allowed_paths": ["notes/**"],
        "prohibited_paths": [".github/**"],
        "may_add_dependencies": False,
        "may_modify_ci": False,
    }
    document["acceptance_criteria"] = [
        {"id": "AC1", "text": "notes/c5-live.txt gained exactly one line and was committed."}
    ]
    document["required_verification"] = [
        {"id": "V1", "command": "echo lint ok", "expect_exit": 0},
        {"id": "V2", "command": "echo test ok", "expect_exit": 0},
        {"id": "V3", "command": "echo scan ok", "expect_exit": 0},
        {"id": "V4", "kind": "artifact", "path": "report/run-evidence.md"},
    ]
    document["deliverables"] = [
        {
            "kind": "pull_request",
            "target": f"https://github.com/{config.repository}",
            "draft": False,
            "closes": [],
        }
    ]
    document["execution_request"] = {
        **document["execution_request"],
        "tier": "trivial",
        "harness": harness,
        "model": model,
        "pin_reason": "live acceptance verifies each configured harness and model",
        "effort": EFFORT[harness],
        "timeout_seconds": 900,
        "rationale": "trivial work goes to the cheapest model the harness offers (05b)",
    }
    del image
    document["lifecycle"] = {"max_attempts": 1, "retry_on": [], "cleanup": "policy"}
    return document


async def _drive(
    supervisor: Supervisor,
    client: TestClient,
    task_id: str,
    states: set[str],
    *,
    max_ticks: int,
    pause: float,
    inspected: dict[str, Any],
) -> str:
    """Tick until the task reaches one of the states; inspect the worker once while it
    runs, for the absence proof over its create request."""
    state = ""
    for _ in range(max_ticks):
        await supervisor.tick()
        view = client.get(f"/v1/tasks/{task_id}").json()
        state = str(view["state"])
        latest = view.get("latest_attempt") or {}
        if not inspected and latest.get("state") == "running" and latest.get("handle"):
            found = daemon.inspect(str(latest["handle"]))
            if found is not None:
                inspected["env"] = list(found.get("Config", {}).get("Env") or [])
                inspected["cmd"] = list(found.get("Config", {}).get("Cmd") or [])
                inspected["labels"] = dict(found.get("Config", {}).get("Labels") or {})
                inspected["mounts"] = [
                    {k: m.get(k) for k in ("Type", "Source", "Destination", "RW")}
                    for m in found.get("Mounts", [])
                ]
        if state in states:
            return state
        time.sleep(pause)
    raise AssertionError(f"task never reached {states}; last state {state}")


def _payload(client: TestClient, task_id: str, kind: str) -> dict[str, Any] | None:
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    hits = [e["payload"] for e in events if e["kind"] == kind]
    return hits[-1] if hits else None


def _haystack(client: TestClient, engine: Engine, artifact_root: Path, task_id: str) -> str:
    parts: list[str] = [client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).text]
    with engine.begin() as connection:
        for table, column in connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type IN "
                "('text', 'character varying', 'jsonb')"
            )
        ).all():
            values = connection.execute(
                text(f'SELECT CAST("{column}" AS TEXT) FROM "{table}"')
            ).scalars()
            parts.extend(str(v) for v in values if v is not None)
        for content, gzipped in connection.execute(
            text("SELECT content, gzipped FROM log_chunks")
        ).all():
            raw = bytes(content)
            parts.append((gzip.decompress(raw) if gzipped else raw).decode("utf-8", "replace"))
    for path in (artifact_root / "store").rglob("*"):
        if path.is_file():
            parts.append(path.read_bytes()[:4_000_000].decode("utf-8", "replace"))
    return "\n".join(parts)


def _record(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, sort_keys=True)
    print(f"live-run: {line}")
    target = os.environ.get(REPORT_ENV, "").strip()
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@pytest.mark.parametrize("harness", _selected())
async def test_a_trivial_task_reaches_ready_for_merge_live(
    harness: str,
    live_ctx: AppContext,
    live_client: TestClient,
    live_supervisor: Supervisor,
    live_config: LiveConfig,
    github: RestGitHubClient,
    mirror: str,
    cleanup: Any,
    credential_root: Path,
    engine: Engine,
    artifact_root: Path,
) -> None:
    image = _images().get(harness) or daemon.worker_image()
    model = _models()[harness]
    secrets = _secret_values(credential_root, harness)
    if default_registry().require(harness).credential_spec() is not None:
        assert secrets, f"no credential files found for {harness} under the configured root"
    register(live_ctx, live_config, mirror)
    _enable(live_ctx, harness)
    external_id = f"{harness.replace('_', '-')}-{RUN_ID}"
    branch = f"crucible/C5-{external_id}"
    cleanup.add_branch(branch)
    started = datetime.now(UTC)
    task_id = submit_and_start(
        live_client, _contract(harness, model, image, live_config, external_id)
    )
    inspected: dict[str, Any] = {}
    entry: dict[str, Any] = {
        "harness": harness,
        "image": image,
        "model_requested": model,
        "started_local": _local(started),
        "task_id": task_id,
    }
    try:
        state = await _drive(
            live_supervisor,
            live_client,
            task_id,
            {"awaiting_internal_review", "pre_pr_gates_failed", "blocked"},
            max_ticks=240,
            pause=5.0,
            inspected=inspected,
        )
        view = live_client.get(f"/v1/tasks/{task_id}").json()
        attempt = view["latest_attempt"]
        running_version = image_harnesses(inspected.get("labels", {})).get(harness)
        harness_view = next(
            item
            for item in live_client.get("/v1/harnesses").json()["items"]
            if item["name"] == harness
        )
        assert running_version, inspected
        assert running_version in harness_view["installed_versions"], {
            "running_label": running_version,
            "harness_view": harness_view,
        }
        sync = _payload(live_client, task_id, "credential_synced") or {}
        metrics = _payload(live_client, task_id, "attempt_metrics_recorded")
        assert metrics is not None, "the completed live attempt has no metrics event"
        entry.update(
            {
                "state_after_run": state,
                "exit_class": attempt.get("exit_class"),
                "exit_code": attempt.get("exit_code"),
                "duration_s": None,
                "model_reported": metrics.get("model"),
                "tokens_in": metrics.get("tokens_in"),
                "tokens_out": metrics.get("tokens_out"),
                "cost_usd": metrics.get("cost_usd"),
                "mount_mode": sync.get("mount_mode"),
                "auth_file_changed": sync.get("changed"),
                "auth_files": sync.get("files"),
                "credential_copy_removed": sync.get("removed"),
                "gate_summary": view.get("gate_summary", {}).get("results"),
                "refusal": _payload(live_client, task_id, "harness_refused"),
                "collected": _payload(live_client, task_id, "attempt_collected"),
                "collection_failed": _payload(live_client, task_id, "collection_failed"),
                "running_harness_version_label": running_version,
                "api_installed_versions": harness_view["installed_versions"],
            }
        )
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT wall_ms, harness_duration_ms, tool_calls FROM attempt_metrics "
                    "WHERE attempt_id = :id"
                ),
                {"id": attempt["id"]},
            ).one_or_none()
        assert row is not None and row.wall_ms is not None, "AttemptMetrics was not recorded"
        entry["duration_s"] = round(row.wall_ms / 1000, 1)
        entry["harness_duration_ms"] = row.harness_duration_ms
        entry["tool_calls"] = row.tool_calls
        history = live_client.get(
            "/v1/routing/history",
            params={"model": model, "project": view["project"]},
        ).json()["items"]
        routing_row = next((item for item in history if item["attempt_id"] == attempt["id"]), None)
        assert routing_row is not None, "the live attempt is absent from routing history"
        assert routing_row["pool"] == ("lab-local" if harness == "hermes" else "e2e")
        assert routing_row["wall_ms"] == row.wall_ms
        entry["routing_history_recorded"] = True
        assert state == "awaiting_internal_review", entry
        if harness != "hermes":
            assert sync, "no credential_synced event: the copy was never synced back"
            assert sync.get("removed") is True, sync

        upload_review(live_client, task_id)
        await _drive(
            live_supervisor,
            live_client,
            task_id,
            {"awaiting_acceptance"},
            max_ticks=20,
            pause=1.0,
            inspected=inspected,
        )
        view = live_client.get(f"/v1/tasks/{task_id}").json()
        accepted = live_client.post(
            f"/v1/tasks/{task_id}/accept",
            json={
                "verdict": "accepted",
                "reasoning": "The live tier accepts its own head to exercise publication.",
                "head_sha": view["head_sha"],
            },
        )
        assert accepted.status_code == 200, accepted.text
        state = await _drive(
            live_supervisor,
            live_client,
            task_id,
            {"ready_for_merge", "publish_failed", "rejected"},
            max_ticks=30,
            pause=2.0,
            inspected=inspected,
        )
        record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
        if "number" in record:
            cleanup.add_pull_request(int(record["number"]))
            entry["pull_request"] = record.get("url")
        entry["state_reached"] = state
        assert state == "ready_for_merge", _payload(live_client, task_id, "task_publish_failed")

        # ----- the absence proof (12) -------------------------------------------
        haystack = _haystack(live_client, engine, artifact_root, task_id)
        haystack += "\n" + json.dumps(inspected)
        access = github_live.token(github, live_config)
        try:
            live = github.get_pull_request(
                access, repository=live_config.repository, number=int(record["number"])
            )
        finally:
            access.discard()
        haystack += "\n" + (getattr(live, "body", None) or "") + "\n" + live.title
        for value in secrets:
            assert value not in haystack, "a credential value reached a Crucible record"
        assert scan_text(haystack) is None, "a secret-shaped value reached a Crucible record"
        env_names = [e.split("=", 1)[0] for e in inspected.get("env", [])]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_names, inspected.get("env")
        entry["absence_proof"] = {
            "values_checked": len(secrets),
            "haystack_bytes": len(haystack),
            "worker_env_names": env_names,
            "worker_mounts": inspected.get("mounts"),
        }
    finally:
        _record(entry)


async def test_hermes_lab_local_pool_runs_four_and_defers_the_fifth_live(
    live_ctx: AppContext,
    live_client: TestClient,
    live_supervisor: Supervisor,
    live_config: LiveConfig,
    mirror: str,
    engine: Engine,
) -> None:
    if "hermes" not in _selected():
        pytest.skip("the live selection does not include Hermes")
    register(live_ctx, live_config, mirror)
    _enable(live_ctx, "hermes")
    image = _images().get("hermes") or daemon.worker_image()
    task_ids: list[str] = []
    started: dict[str, datetime] = {}
    for number in range(5):
        external_id = f"hermes-pool-{number}-{RUN_ID}"
        task_id = submit_and_start(
            live_client,
            _contract("hermes", "coder", image, live_config, external_id),
        )
        task_ids.append(task_id)
        started[task_id] = datetime.now(UTC)

    for _ in range(3):
        await live_supervisor.tick()
        states = [live_client.get(f"/v1/tasks/{task_id}").json()["state"] for task_id in task_ids]
        if states.count("running") == 4 and states.count("scheduled") == 1:
            break
    assert states.count("running") == 4, states
    assert states.count("scheduled") == 1, states
    deferred = live_client.get(f"/v1/tasks/{task_ids[-1]}/events").json()["items"]
    assert any(
        event["kind"] == "harness_launch_deferred"
        and "lab-local pool" in event["payload"]["detail"]
        for event in deferred
    )
    waiting = live_client.get(f"/v1/tasks/{task_ids[-1]}").json()
    waiting_attempt = waiting["latest_attempt"]["id"]
    response = live_client.post(
        f"/v1/tasks/{task_ids[-1]}/cancel",
        json={
            "reason": "the fifth task proved the four-slot pool cap",
            "verbatim": "cancel the deferred fifth pool-cap task",
            "decided_by": "e2e-live",
        },
    )
    assert response.status_code == 200, response.text

    completed = task_ids[:4]
    final_states: dict[str, str] = {}
    views: dict[str, dict[str, Any]] = {}
    terminal = {"awaiting_internal_review", "pre_pr_gates_failed", "blocked", "cancelled"}
    for _ in range(240):
        await live_supervisor.tick()
        views = {task_id: live_client.get(f"/v1/tasks/{task_id}").json() for task_id in completed}
        final_states = {task_id: str(view["state"]) for task_id, view in views.items()}
        if all(state in terminal for state in final_states.values()):
            break
        await asyncio.sleep(5.0)
    assert all(state in terminal for state in final_states.values()), final_states

    runs: list[dict[str, Any]] = []
    attempt_ids: set[str] = set()
    work_branches: set[str] = set()
    for task_id in completed:
        view = views[task_id]
        attempt = view["latest_attempt"]
        assert attempt["id"] not in attempt_ids
        work_branch = view["contract"]["repository"]["work_branch"]
        assert work_branch not in work_branches
        attempt_ids.add(attempt["id"])
        work_branches.add(work_branch)
        gates = view["gate_summary"]["results"]
        gates_passed = all(
            result == "pass" for name, result in gates.items() if name != "internal_review_recorded"
        )
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT wall_ms, harness_duration_ms, tokens_in, tokens_out, tool_calls "
                    "FROM attempt_metrics WHERE attempt_id = :id"
                ),
                {"id": attempt["id"]},
            ).one_or_none()
        assert row is not None and row.wall_ms is not None
        runs.append(
            {
                "task_id": task_id,
                "attempt_id": attempt["id"],
                "started_local": _local(started[task_id]),
                "wall_seconds": round(row.wall_ms / 1000, 1),
                "harness_duration_ms": row.harness_duration_ms,
                "tokens_in": row.tokens_in,
                "tokens_out": row.tokens_out,
                "tool_calls": row.tool_calls,
                "state": view["state"],
                "exit_class": attempt["exit_class"],
                "exit_code": attempt["exit_code"],
                "pre_pr_gates_passed": gates_passed,
            }
        )
    all_completed = all(
        run["state"] == "awaiting_internal_review"
        and run["exit_class"] == "completed"
        and run["exit_code"] == 0
        and run["pre_pr_gates_passed"]
        for run in runs
    )
    _record(
        {
            "kind": "hermes-pool-run",
            "pool": "lab-local",
            "max_concurrency": 4,
            "all_completed": all_completed,
            "runs": runs,
            "deferred_fifth": {
                "task_id": task_ids[-1],
                "attempt_id": waiting_attempt,
                "state_when_observed": "scheduled",
                "durable_defer_event": True,
            },
        }
    )

    for task_id in completed:
        if final_states[task_id] != "awaiting_internal_review":
            continue
        response = live_client.post(
            f"/v1/tasks/{task_id}/cancel",
            json={
                "reason": "the completed four-way gate evidence is recorded",
                "verbatim": "cancel the completed pool-gate task before review",
                "decided_by": "e2e-live",
            },
        )
        assert response.status_code == 200, response.text
    assert all_completed, runs
