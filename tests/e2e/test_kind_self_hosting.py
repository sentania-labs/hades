"""hades #184 on a real kind cluster: this repository's own checks in the worker image.

A Hermes task against a bare copy of this repository at HEAD, under
examples/policies/hades-self-hosting.yaml, on the Kubernetes provider with the combined
worker image images/manifest.env pins and the real per-worker NetworkPolicy (Calico,
no broad egress). The model is a stub Pod in its own namespace, reached the way the
lab's gateway is (an in-cluster endpoint selector); it has Hermes run one scripted
command that first runs `make images-check` and `make registry-check` (hades #475: the
worker builds both images through Hades's own rootless BuildKit, which
deploy/kind/workers.yaml starts in `crucible-buildkit`, and resolves the published
worker image on GHCR through its per-attempt policy), then appends a line to
docs/roadmap.md, commits it and writes the report. The verifier then runs `make lint`,
`make test-unit`, `make scan` and the two image checks again from the collected tree,
fetching the locked dependencies from PyPI through the policy's allowlist, and the
task has to get past `verification_ran` with all five passing.

`make e2e-kind-self-hosting` runs it (tools/kind/e2e-kind.sh with
CRUCIBLE_E2E_KIND_WORKER_IMAGE). Local only: it needs the network and a committed HEAD.
The Postgres the API and the supervisor use is a testcontainers one of its own, not the
Docker e2e tier's stack, whose fixed worker subnet another run may hold.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sregistry import CraneRegistryClient
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.harness.registry import default_registry as application_harnesses
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.auth import mint_token
from crucible.application.harnesses import set_harness_enabled
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.cluster_egress import ClusterEgress
from crucible.domain.entities import Role
from tests.e2e.conftest import e2e_contract, gate_results
from tests.e2e.test_kind import CreateRecordingClient, _recording_client
from tests.fixtures import promote_for_test

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "policies" / "hades-self-hosting.yaml"
STUB = ROOT / "tests" / "e2e" / "stub_model.py"
POSTGRES_IMAGE = (
    "postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
)
MODEL_NAMESPACE = "crucible-kind-model"
MODEL_PORT = 8765
MODEL_LABELS = {"app": "stub-model"}
ENDPOINT = f"http://stub-model.{MODEL_NAMESPACE}.svc.cluster.local:{MODEL_PORT}/v1"
CHECKS = (
    "make lint",
    "make test-unit",
    "make scan",
    "make images-check",
    "make registry-check",
)
SETTLED = {
    "accepted",
    "pre_pr_gates_failed",
    "failed",
    "blocked",
    "escalated",
    "cancelled",
}

# What the stub model has Hermes run, once, through its terminal tool: the trivial,
# correct change, its commit, and the report (only the judgement fields; Crucible
# fills the facts, section 7 of the identity bundle).
CHANGE = r"""set -eu
cd /crucible/repo
# Issue 475: the worker itself reaches Hades's rootless BuildKit and GHCR through its
# per-attempt policy. The verifier repeats these required checks from the collected tree.
make images-check
make registry-check
printf '\n%s\n' 'A worker in the combined image wrote this for the hades 184 kind proof.' \
  >> docs/roadmap.md
git add docs/roadmap.md
git commit -q -m 'docs: a line from the hades 184 kind proof' \
  -m "Crucible-Attempt: ${CRUCIBLE_ATTEMPT_ID:-unknown}"
cat > /crucible/report/report.yaml <<'EOF'
schema_version: "1.0"
summary: Appended one line to docs/roadmap.md for the hades 184 kind proof.
self_review:
  documentation: ["Updated docs/roadmap.md."]
  acceptance_criteria:
    - {id: AC1, status: met, evidence: "docs/roadmap.md gains the line"}
  omissions: []
acceptance_mapping:
  - {id: AC1, status: met, evidence: "docs/roadmap.md gains the line"}
proposed_pull_request:
  title: "docs: a line from the hades 184 kind proof"
  body: Produced by the stub model driving Hermes on kind.
limitations: []
risks: []
blockers: []
follow_ups: []
EOF
crucible-report check /crucible/report/report.yaml
"""

pytestmark = [
    pytest.mark.e2e,
    # Image pulls of the combined image, a uv sync from PyPI in the verifier, and the
    # unit tier on the policy's two CPUs.
    pytest.mark.timeout(2700),
    pytest.mark.skipif(
        not os.environ.get("CRUCIBLE_E2E_KIND_WORKER_REGISTRY"),
        reason="needs make e2e-kind-self-hosting",
    ),
]


def _kubectl(*args: str, stdin: str | None = None) -> str:
    """The cluster admin's kubectl (e2e-kind.sh exports KUBECONFIG), for the stub model's
    own namespace: the supervisor's credential is confined to the workers namespace."""
    return subprocess.run(
        ["kubectl", *args], input=stdin, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url)
        yield url


BUILDKIT_NAMESPACE = "crucible-buildkit"


@pytest.fixture(scope="module")
def buildkit() -> None:
    """Hades's own BuildKit, which deploy/kind/workers.yaml starts (hades #475): ready
    before the task is submitted, so the worker's `make images-check` finds a daemon
    and not a Pod still pulling its image."""
    _kubectl(
        "-n",
        BUILDKIT_NAMESPACE,
        "rollout",
        "status",
        "deployment/crucible-buildkit",
        "--timeout=600s",
    )
    pods = json.loads(_kubectl("-n", BUILDKIT_NAMESPACE, "get", "pods", "-o", "json"))["items"]
    ready = [
        pod["metadata"]["name"]
        for pod in pods
        if all(c.get("ready") for c in pod["status"].get("containerStatuses", []))
    ]
    print(f"hades-475: BuildKit ready in {BUILDKIT_NAMESPACE}: {ready}")
    assert ready


@pytest.fixture(scope="module")
def stub_model() -> Iterator[None]:
    image = os.environ["CRUCIBLE_E2E_KIND_WORKER_REGISTRY"]
    _kubectl("create", "namespace", MODEL_NAMESPACE)
    _kubectl(
        "-n", MODEL_NAMESPACE, "create", "configmap", "stub-model", f"--from-file=stub.py={STUB}"
    )
    labels = MODEL_LABELS
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "stub-model", "namespace": MODEL_NAMESPACE, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000},
            "containers": [
                {
                    "name": "stub",
                    "image": image,
                    "command": ["python3", "/stub/stub.py", str(MODEL_PORT)],
                    "env": [
                        {"name": "STUB_BIND", "value": "0.0.0.0"},
                        {"name": "STUB_COMMAND", "value": CHANGE},
                        {"name": "STUB_LOG", "value": "/tmp/stub.jsonl"},
                    ],
                    "ports": [{"containerPort": MODEL_PORT}],
                    "readinessProbe": {
                        "tcpSocket": {"port": MODEL_PORT},
                        "periodSeconds": 1,
                    },
                    "volumeMounts": [
                        {"name": "stub", "mountPath": "/stub"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
            "volumes": [
                {"name": "stub", "configMap": {"name": "stub-model"}},
                {"name": "tmp", "emptyDir": {}},
            ],
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "stub-model", "namespace": MODEL_NAMESPACE},
        "spec": {"selector": labels, "ports": [{"port": MODEL_PORT, "targetPort": MODEL_PORT}]},
    }
    _kubectl(
        "apply",
        "-f",
        "-",
        stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": [pod, service]}),
    )
    _kubectl(
        "-n", MODEL_NAMESPACE, "wait", "--for=condition=Ready", "pod/stub-model", "--timeout=600s"
    )
    yield
    with_log = subprocess.run(
        ["kubectl", "-n", MODEL_NAMESPACE, "exec", "stub-model", "--", "cat", "/tmp/stub.jsonl"],
        capture_output=True,
        text=True,
        check=False,
    )
    calls = [json.loads(line) for line in with_log.stdout.splitlines() if line.strip()]
    print(f"hades-184: the stub model answered {len(calls)} request(s)")
    _kubectl("delete", "namespace", MODEL_NAMESPACE, "--wait=false")


def _repository(name: str) -> str:
    """A bare copy of this repository at HEAD, on the tier's reference cache, whose
    default branch is `main` as on GitHub. The preparer clones it by file://."""
    cache = Path(os.environ["CRUCIBLE_E2E_KIND_CACHE"])
    bare = cache / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(
        ["git", "-C", str(ROOT), "push", "-q", str(bare), "HEAD:refs/heads/main"], check=True
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "config",
            "remote.origin.url",
            f"file:///crucible/cache/{bare.name}",
        ],
        check=True,
    )
    for path in [bare, *bare.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    return f"file:///crucible/cache/{bare.name}"


def _provider(client: CreateRecordingClient, image: str) -> KubernetesProvider:
    return KubernetesProvider(
        KubernetesConfig(
            storage_class="standard",
            workspace_size="4Gi",
            cache_claim="crucible-reference-cache",
            poll_interval_seconds=0.5,
            launch_timeout_seconds=600,
            prepare_timeout_seconds=300,
            collector_timeout_seconds=300,
            verifier_timeout_seconds=1800,
            cluster_dns_ip=os.environ["CRUCIBLE_E2E_KIND_DNS_IP"],
            # The real per-host rules and hostAliases pinning (hades #191), not the
            # tier's usual public-internet rule: PyPI is reached through the policy.
            broad_egress=False,
            image_repositories=(image,),
            probe_image=image,
            pod_pid_limit_override=512,
            local_endpoint_url=ENDPOINT,
            egress=ClusterEgress(
                endpoint_namespace=MODEL_NAMESPACE,
                endpoint_pod_labels=tuple(MODEL_LABELS.items()),
                endpoint_port=MODEL_PORT,
            ),
        ),
        client,
        CraneRegistryClient(timeout=30),
    )


def _upload(admin: TestClient, ctx: AppContext) -> None:
    """The operator's side, as docs/deployment.md has it: a routing version with the
    gateway's `fast` model enabled for Hermes and a default-software version naming it
    (what the gateway page writes), then the example uploaded with the routing policy
    that default-software names. The one change to the example is the image allowlist,
    which has to admit this cluster's registry."""
    usage = admin.get("/v1/routing/usage", params={"policy": "default-software"}).json()
    ref = usage["routing_policy"]
    routing = admin.get(f"/v1/routing/{ref['name']}/{ref['version']}").json()["document"]
    local = next(m for m in routing["models"] if m["endpoint"] == "local")
    for model in routing["models"]:
        model["enabled"] = False
        model["disabled_reason"] = "not picked on this cluster"
    routing["models"].append(
        {
            **local,
            "id": "fast",
            "endpoint_url": ENDPOINT,
            "capability": "mid",
            "speed": "fast",
            "enabled": True,
            "disabled_reason": None,
        }
    )
    routing["version"] = ref["version"] + 1
    response = admin.put(f"/v1/routing/{routing['name']}/{routing['version']}", json=routing)
    assert response.status_code == 200, response.text
    with ctx.uow_factory() as uow:
        newest = max(uow.policies.list_versions("default-software"), key=lambda p: p.version)
    default = dict(newest.document)
    default["version"] = newest.version + 1
    default["routing"] = {"policy": {"name": routing["name"], "version": routing["version"]}}
    response = admin.put(f"/v1/policies/default-software/{default['version']}", json=default)
    assert response.status_code == 200, response.text

    usage = admin.get("/v1/routing/usage", params={"policy": "default-software"}).json()
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["routing"] = {"policy": usage["routing_policy"]}
    document["images"]["allowlist"] = [*document["images"]["allowlist"], "localhost:*/*"]
    response = admin.put("/v1/policies/hades-self-hosting/2", json=document)
    assert response.status_code == 200, response.text


async def test_hades_184_a_hermes_task_passes_the_repositorys_own_checks_in_the_verifier(
    database: str, stub_model: None, buildkit: None
) -> None:
    image = os.environ["CRUCIBLE_E2E_KIND_WORKER_REGISTRY"]
    client = _recording_client()
    provider = _provider(client, image)
    registry = CraneRegistryClient(timeout=30)
    engine: Engine = make_engine(database)
    clock = SystemClock()
    harnesses = application_harnesses(test_fixtures=True)
    artifacts = Path(tempfile.mkdtemp(prefix="crucible-kind-184-"))
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=clock,
        providers=[provider],
        database_url=database,
        engine=engine,
        artifact_store=DiskArtifactStore(artifacts),
        harnesses=harnesses,
    )
    tokens: dict[str, str] = {}
    resolved = await asyncio.to_thread(registry.resolve, image)
    with ctx.uow_factory() as uow:
        for role in Role:
            tokens[role.value] = mint_token(uow, clock, name=f"kind-{role.value}", role=role).token
        set_harness_enabled(
            uow,
            clock,
            principal_name="e2e-kind",
            name="hermes",
            enabled=True,
            reason="hades #184 kind proof",
        )
        promote_for_test(
            uow,
            digest=resolved.digest,
            reference=resolved.reference,
            harnesses=dict(resolved.harnesses),
            at=clock.now(),
            by="e2e-kind",
            reason="the combined worker image under test",
        )
        uow.commit()
    app = create_app(ctx)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        _upload(admin, ctx)

    url = _repository("hades-self")
    with ctx.uow_factory() as uow:
        register_repository(
            uow,
            clock,
            principal_name="e2e-kind",
            name="hades-self",
            registration=RepositoryRegistration(
                url=url,
                default_branch="main",
                policy_name="hades-self-hosting",
                external_review=ExternalReviewAttestation(
                    attested_all_prs=True, attested_by="e2e-kind"
                ),
            ),
        )
        uow.commit()
    document = e2e_contract("HADES-184-KIND", "hades-self", image)
    document["scope"]["allowed_paths"] = ["docs/**"]
    document["acceptance_criteria"] = [{"id": "AC1", "text": "docs/roadmap.md gains one line."}]
    document["required_verification"] = [
        {"id": f"V{i}", "command": check, "expect_exit": 0} for i, check in enumerate(CHECKS, 1)
    ]
    document["policy"] = {"name": "hades-self-hosting", "version": 2}
    document["execution_request"].update({"provider": "kubernetes", "timeout_seconds": 900})

    supervisor = Supervisor(
        ctx.uow_factory,
        {"kubernetes": provider},
        clock,
        holder="e2e-kind-184",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=120,
        grace_seconds=5,
        harnesses=harnesses,
    )
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['operator']}"}) as operator:
        response = operator.post("/v1/tasks", json=document)
        assert response.status_code == 201, response.text
        task_id = str(response.json()["id"])
        response = operator.post(
            f"/v1/tasks/{task_id}/start",
            json={"provider": "kubernetes", "policy_version": 2},
        )
        assert response.status_code == 200, response.text

        started = time.monotonic()
        state = ""
        while time.monotonic() - started < 2400:
            await supervisor.tick()
            state = str(operator.get(f"/v1/tasks/{task_id}").json()["state"])
            if state in SETTLED:
                break
            await asyncio.sleep(1)
        await supervisor.stop()
        view = operator.get(f"/v1/tasks/{task_id}").json()
        results = gate_results(operator, task_id)
        attempt = view["executions"][0]["attempts"][0]
        print(
            f"hades-184: task {task_id} settled in {state} after {time.monotonic() - started:.0f}s"
        )
        print(f"hades-184: attempt {attempt['id']} ran {attempt['harness']}/{attempt['model']}")
        print("hades-184: gates", json.dumps(results, sort_keys=True))

    with engine.begin() as connection:
        runs = [
            row.payload
            for row in connection.execute(
                text(
                    "SELECT payload FROM evidence WHERE attempt_id = :id "
                    "AND kind = 'verification_run' ORDER BY id"
                ),
                {"id": attempt["id"]},
            )
        ]
    for run in runs:
        print(
            f"hades-184: verifier {run['id']} `{run['command']}` exited {run['exit_code']} "
            f"in {run['seconds']}s"
        )

    verifier = next(
        body
        for _, kind, body in client.made
        if kind == "jobs"
        and body["metadata"]["labels"].get(k8sspec.LABEL_ATTEMPT) == attempt["id"]
        and body["metadata"]["labels"].get(k8sspec.LABEL_ROLE) == k8sspec.ROLE_VERIFIER
    )
    aliases = verifier["spec"]["template"]["spec"].get("hostAliases", [])
    pinned = sorted({name for alias in aliases for name in alias["hostnames"]})
    print(f"hades-184: the verifier Pod pinned {pinned} through hostAliases")
    policies = [
        body
        for _, kind, body in client.made
        if kind == "networkpolicies"
        and body["metadata"]["labels"].get(k8sspec.LABEL_ROLE) == k8sspec.ROLE_VERIFIER
    ]

    assert (attempt["harness"], attempt["model"]) == ("hermes", "fast")
    assert state == "accepted", json.dumps(results, sort_keys=True)
    assert results["verification_ran"] == "pass", results
    assert sorted(run["command"] for run in runs) == sorted(CHECKS)
    assert all(run["ran"] and run["exit_code"] == 0 for run in runs), runs
    assert all(isinstance(run["seconds"], int) for run in runs), runs
    assert {"pypi.org", "files.pythonhosted.org"} <= set(pinned)
    # hades #475: the registry `make registry-check` resolves, with GHCR's redirect host.
    assert {"ghcr.io", "pkg-containers.githubusercontent.com"} <= set(pinned)
    assert "github.com" not in pinned
    buildkit_rules = [
        rule
        for policy in policies
        for rule in policy["spec"]["egress"]
        if any(
            peer.get("namespaceSelector", {})
            .get("matchLabels", {})
            .get("kubernetes.io/metadata.name")
            == BUILDKIT_NAMESPACE
            for peer in rule.get("to", [])
        )
    ]
    assert buildkit_rules and all(
        rule["ports"] == [{"protocol": "TCP", "port": 1234}] for rule in buildkit_rules
    )
    assert policies and all(
        rule.get("to")
        and all(
            "ipBlock" not in peer or peer["ipBlock"]["cidr"] != "0.0.0.0/0" for peer in rule["to"]
        )
        for policy in policies
        for rule in policy["spec"]["egress"]
    )
    engine.dispose()
