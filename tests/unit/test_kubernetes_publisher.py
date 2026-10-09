"""The publisher as a Job on Kubernetes (23, 26, S10, hades FDY-0133).

What would be a serious defect to lose, each asserted against the objects the provider
actually sends to the (fake) API server: the token reaches the push through a Secret
volume and nowhere else, the Secret is gone when the push is, the NetworkPolicy selects
the publisher's Pod and opens only the git host, the bundle is the one leaf of the claim
the Pod sees, and the outcome follows the same rules as the Docker publisher's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec, scripts
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.execution.k8spublisher import (
    BUNDLE_LEAF,
    ByWorkspacePublisher,
    KubernetesPublisher,
    KubernetesPublisherConfig,
    job_name,
    policy_name,
    token_secret_name,
)
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.execution.publisher import BUNDLE_SEAL_REFUSED
from crucible.ports.execution import WORK_MOUNT, ProviderError
from crucible.ports.github import InstallationToken
from crucible.ports.publish import (
    MergeMainOutcome,
    MergeMainRequest,
    PublishOutcome,
    PublishRequest,
)
from tests.integration.fake_github import installation_token_value
from tests.unit.kubernetes_fixtures import ATTEMPT, IMAGE, TASK, build

HEAD = "a" * 40
BUNDLE = b"a verified branch bundle"
DIGEST_IMAGE = "ghcr.io/sentania-labs/crucible-worker@sha256:" + "d" * 64


def _setup(
    *, bundle: bytes | None = BUNDLE, claim: bool = True, **api_kwargs: Any
) -> tuple[FakeKubernetesApi, KubernetesProvider, KubernetesPublisher]:
    api, _registry, provider = build(
        config=KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            storage_class="lab-ssd",
            probe_image=IMAGE,
        ),
        **api_kwargs,
    )
    name = k8sspec.object_name("ws", ATTEMPT)
    if claim:
        api.create(
            "persistentvolumeclaims",
            k8sspec.workspace_claim(
                name=name,
                namespace=provider.config.namespace,
                object_labels={k8sspec.LABEL_ATTEMPT: ATTEMPT},
                size="1Gi",
                storage_class="lab-ssd",
            ),
        )
        if bundle is not None:
            api.claims[name][BUNDLE_LEAF] = bundle
    return api, provider, KubernetesPublisher(provider, KubernetesPublisherConfig())


def _request(**overrides: Any) -> PublishRequest:
    base: dict[str, Any] = {
        "attempt_id": ATTEMPT,
        "task_id": TASK,
        "owner": "EX-0001",
        "repository_url": "https://github.com/example-org/example-service.git",
        "work_branch": "crucible/EX-0001",
        "base_ref": "main",
        "expected_head": HEAD,
        "bundle_path": f"k8s://hades-workers/ws-{ATTEMPT.lower()}/output/work_branch.bundle",
        "bundle_sha256": hashlib.sha256(BUNDLE).hexdigest(),
        "image": DIGEST_IMAGE,
        "policy": {"resources": {"cpus": 1, "memory": "512MiB"}},
    }
    base.update(overrides)
    return PublishRequest(**base)


def _merge_request(**overrides: Any) -> MergeMainRequest:
    base: dict[str, Any] = {
        "attempt_id": ATTEMPT,
        "task_id": TASK,
        "owner": "EX-0001",
        "repository_url": "https://github.com/example-org/example-service.git",
        "work_branch": "crucible/EX-0001",
        "base_ref": "main",
        "expected_head": HEAD,
        "image": DIGEST_IMAGE,
        "policy": {"resources": {"cpus": 1, "memory": "512MiB"}},
        "workspace_path": f"k8s://hades-workers/ws-{ATTEMPT.lower()}",
    }
    base.update(overrides)
    return MergeMainRequest(**base)


def _token(value: str) -> InstallationToken:
    return InstallationToken(
        value, expires_at=datetime.now(UTC) + timedelta(hours=1), repository="example-service"
    )


def _created(api: FakeKubernetesApi, kind: str) -> list[dict[str, Any]]:
    return [row["body"] for row in api.created if row["kind"] == kind]


def _publisher_job(api: FakeKubernetesApi) -> dict[str, Any]:
    jobs = [
        body
        for body in _created(api, "jobs")
        if body["metadata"]["labels"][k8sspec.LABEL_ROLE] == k8sspec.ROLE_PUBLISHER
    ]
    assert len(jobs) == 1, jobs
    return jobs[0]


async def test_a_push_is_one_job_and_the_token_reaches_it_only_through_a_secret_volume(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    value = installation_token_value()
    api, _provider, publisher = _setup()

    outcome = await publisher.push(_request(), _token(value))

    assert outcome.pushed, outcome
    assert (outcome.step, outcome.head_sha, outcome.exit_code) == ("done", HEAD, 0)
    # The value reached the push, through the Secret the Pod mounted.
    assert [(p["branch"], p["head"], p["token"]) for p in api.pushes] == [
        ("crucible/EX-0001", HEAD, value)
    ]
    # One Secret carried it, and only that Secret: not the Job, not the Pod, not the
    # NetworkPolicy, not the reader, not an env var, not the script.
    secrets = _created(api, "secrets")
    assert [s["metadata"]["name"] for s in secrets] == [token_secret_name(ATTEMPT)]
    for row in api.created:
        if row["kind"] != "secrets":
            assert value not in json.dumps(row["body"]), row["kind"]
    job = _publisher_job(api)
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert all(value not in str(env) for env in container["env"])
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["publish-token"]["secret"] == {
        "secretName": token_secret_name(ATTEMPT),
        "defaultMode": 0o400,
        "items": [{"key": "token", "path": "token", "mode": 0o400}],
        "optional": False,
    }
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert mounts[scripts.TOKEN_MOUNT] == {
        "name": "publish-token",
        "mountPath": scripts.TOKEN_MOUNT,
        "readOnly": True,
    }
    # The bundle is the one leaf of the claim the Pod sees, read-only; the outcome leaf
    # is its own; nothing mounts the checkout, the report or the claim's root.
    assert mounts[f"{scripts.BUNDLE_MOUNT}/work_branch.bundle"] == {
        "name": "ws",
        "mountPath": f"{scripts.BUNDLE_MOUNT}/work_branch.bundle",
        "readOnly": True,
        "subPath": BUNDLE_LEAF,
    }
    assert mounts[scripts.PUBLISH_MOUNT]["subPath"] == "publish"
    claim_mounts = sorted(
        m.get("subPath", "") for m in container["volumeMounts"] if m["name"] == "ws"
    )
    assert claim_mounts == [BUNDLE_LEAF, "publish"]
    # The script reads the token file and never takes it from stdin or removes it.
    script = container["command"][-1]
    assert 'cat > "$TOKDIR/token"' not in script
    assert "drop_token() { :; }" in script
    # 26's pod shape, the same as every other role.
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert job["spec"]["backoffLimit"] == 0
    # No token in any log record this process wrote.
    assert all(value not in record.getMessage() for record in caplog.records)
    assert all(value not in str(record.__dict__) for record in caplog.records)
    assert value not in repr(outcome)


async def test_the_secret_job_pod_and_policy_are_gone_when_the_push_is() -> None:
    api, _provider, publisher = _setup()
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert outcome.pushed
    assert not api.secret_exists(token_secret_name(ATTEMPT))
    assert job_name(ATTEMPT) not in api.object_names("jobs")
    assert policy_name(ATTEMPT) not in api.object_names("networkpolicies")
    assert api.object_names("pods") == []
    # The Secret was deleted after the Job and its Pod, never before the push ran.
    order = [(kind, name) for kind, name in api.deleted if kind in ("jobs", "secrets", "pods")]
    assert order.index(("secrets", token_secret_name(ATTEMPT))) > order.index(
        ("jobs", job_name(ATTEMPT))
    )
    # The workspace claim, which holds the bundle, is not the publisher's to remove.
    assert k8sspec.object_name("ws", ATTEMPT) in api.object_names("persistentvolumeclaims")


async def test_the_network_policy_selects_the_publisher_pod_and_opens_only_the_git_host() -> None:
    api, _provider, publisher = _setup()
    await publisher.push(_request(), _token(installation_token_value()))
    policies = [
        p
        for p in _created(api, "networkpolicies")
        if p["metadata"]["labels"].get(k8sspec.LABEL_ATTEMPT) == ATTEMPT
    ]
    assert [p["metadata"]["name"] for p in policies] == [policy_name(ATTEMPT)]
    policy = policies[0]
    pod_labels = _publisher_job(api)["spec"]["template"]["metadata"]["labels"]
    selector = policy["spec"]["podSelector"]["matchLabels"]
    assert selector == {
        k8sspec.LABEL_ATTEMPT: ATTEMPT,
        k8sspec.LABEL_ROLE: k8sspec.ROLE_PUBLISHER,
    }
    assert all(pod_labels.get(k) == v for k, v in selector.items())
    assert policy["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS] == (
        "api.github.com,github.com"
    )
    https = [rule for rule in policy["spec"]["egress"] if rule["ports"][0]["port"] == 443]
    assert sorted(peer["ipBlock"]["cidr"] for peer in https[0]["to"]) == [
        "140.82.121.4/32",
        "140.82.121.6/32",
    ]
    # hades #191: the Pod connects to the addresses its policy permits.
    aliases = _publisher_job(api)["spec"]["template"]["spec"]["hostAliases"]
    assert {"ip": "140.82.121.4", "hostnames": ["github.com"]} in aliases


async def test_a_job_that_cannot_be_created_still_removes_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _provider, publisher = _setup()
    await _provider.ensure_ready()
    create = api.create

    def refuse_the_publisher_job(kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind == "jobs" and body["metadata"]["name"] == job_name(ATTEMPT):
            raise KubernetesApiError(500, "the fake refuses to create the publisher Job")
        return create(kind, body)

    monkeypatch.setattr(api, "create", refuse_the_publisher_job)
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert not outcome.pushed and outcome.step == "container"
    assert token_secret_name(ATTEMPT) in [s["metadata"]["name"] for s in _created(api, "secrets")]
    assert not api.secret_exists(token_secret_name(ATTEMPT))
    assert api.pushes == []


async def test_a_bundle_that_no_longer_matches_its_seal_is_refused_before_any_remote() -> None:
    api, _provider, publisher = _setup(bundle=b"a bundle somebody changed")
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert not outcome.pushed
    assert (outcome.step, outcome.exit_code) == ("bundle-seal", BUNDLE_SEAL_REFUSED)
    assert "sealed sha256" in outcome.detail
    assert api.pushes == []
    assert not api.secret_exists(token_secret_name(ATTEMPT))


@pytest.mark.parametrize(
    "bundle_path",
    [
        # Another attempt's claim, another namespace, another leaf, a local path.
        "k8s://hades-workers/ws-01otherattempt00000000000/output/work_branch.bundle",
        f"k8s://elsewhere/ws-{ATTEMPT.lower()}/output/work_branch.bundle",
        f"k8s://hades-workers/ws-{ATTEMPT.lower()}/repo/.git/objects/pack/x.pack",
        f"/var/lib/crucible/artifacts/workspaces/{ATTEMPT}/output/work_branch.bundle",
    ],
)
async def test_a_bundle_path_that_is_not_this_attempts_collected_bundle_creates_nothing(
    bundle_path: str,
) -> None:
    api, _provider, publisher = _setup()
    before = len(api.created)
    outcome = await publisher.push(
        _request(bundle_path=bundle_path), _token(installation_token_value())
    )
    assert not outcome.pushed and outcome.step == "bundle-seal"
    assert len(api.created) == before


async def test_an_unsealed_bundle_or_a_missing_claim_is_refused_before_a_token_is_placed() -> None:
    api, _provider, publisher = _setup()
    outcome = await publisher.push(_request(bundle_sha256=""), _token(installation_token_value()))
    assert (outcome.pushed, outcome.step) == (False, "bundle-seal")
    assert _created(api, "secrets") == []

    api, _provider, publisher = _setup(claim=False)
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert (outcome.pushed, outcome.step) == (False, "bundle-seal")
    assert "is gone" in outcome.detail
    assert _created(api, "secrets") == []


async def test_a_namespace_without_egress_enforcement_gets_no_token() -> None:
    api, _provider, publisher = _setup(egress_enforced=False)
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert (outcome.pushed, outcome.step) == (False, "namespace")
    assert _created(api, "secrets") == []


async def test_a_retried_push_replaces_a_secret_an_earlier_try_left() -> None:
    api, _provider, publisher = _setup()
    api.put_harness_secret(token_secret_name(ATTEMPT), {"token": b"an expired token"})
    value = installation_token_value()
    outcome = await publisher.push(_request(), _token(value))
    assert outcome.pushed
    assert api.pushes[-1]["token"] == value
    assert not api.secret_exists(token_secret_name(ATTEMPT))


async def test_cleanup_removes_the_job_pod_policy_and_secret_a_push_left() -> None:
    api, provider, publisher = _setup()
    labels = {k8sspec.LABEL_ATTEMPT: ATTEMPT, k8sspec.LABEL_ROLE: k8sspec.ROLE_PUBLISHER}
    api.put_harness_secret(token_secret_name(ATTEMPT), {"token": b"left behind"}, labels)
    api.pending_forever.add(job_name(ATTEMPT) + "-abc12")
    api.create(
        "jobs",
        k8sspec.job(
            name=job_name(ATTEMPT),
            namespace=provider.config.namespace,
            object_labels=labels,
            pod={"containers": [{"name": "crucible", "image": DIGEST_IMAGE}]},
            active_deadline_seconds=600,
        ),
    )
    api.create(
        "networkpolicies",
        {"metadata": {"name": policy_name(ATTEMPT), "labels": labels}, "spec": {}},
    )
    assert api.object_names("pods")

    assert await publisher.cleanup([ATTEMPT]) == 1
    assert job_name(ATTEMPT) not in api.object_names("jobs")
    assert policy_name(ATTEMPT) not in api.object_names("networkpolicies")
    assert api.object_names("pods") == []
    assert not api.secret_exists(token_secret_name(ATTEMPT))
    # Nothing left is still "removed".
    assert await publisher.cleanup([ATTEMPT]) == 1


class _Recording:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        self.paths.append(request.bundle_path)
        return PublishOutcome(pushed=True, head_sha=request.expected_head, step="done")

    async def merge_main(
        self, request: MergeMainRequest, token: InstallationToken
    ) -> MergeMainOutcome:
        self.paths.append(request.workspace_path)
        return MergeMainOutcome(merged=False, step="up-to-date")

    async def cleanup(self, attempt_ids: Any) -> int:
        return len(attempt_ids)


async def test_with_both_providers_the_bundle_path_picks_the_publisher() -> None:
    docker, kubernetes = _Recording(), _Recording()
    both = ByWorkspacePublisher(docker, kubernetes)
    token = _token(installation_token_value())
    await both.push(_request(), token)
    await both.push(_request(bundle_path="/var/lib/crucible/x/output/work_branch.bundle"), token)
    assert kubernetes.paths == [_request().bundle_path]
    assert docker.paths == ["/var/lib/crucible/x/output/work_branch.bundle"]
    assert await both.cleanup(["a"]) == 2
    # hades #411: a merge of main runs where the attempt's workspace is, too.
    await both.merge_main(_merge_request(), token)
    await both.merge_main(_merge_request(workspace_path="/var/lib/crucible/x"), token)
    assert kubernetes.paths[-1] == _merge_request().workspace_path
    assert docker.paths[-1] == "/var/lib/crucible/x"


def test_a_credential_host_other_than_github_is_the_only_extra_destination() -> None:
    _api, provider, _publisher = _setup()
    plan = KubernetesPublisher(provider, KubernetesPublisherConfig())._plan()
    assert (plan.hosts, plan.endpoints) == (("api.github.com", "github.com"), ())
    plan = KubernetesPublisher(
        provider, KubernetesPublisherConfig(credential_host="git.example.com")
    )._plan()
    assert plan.hosts == ("api.github.com", "github.com", "git.example.com")
    plan = KubernetesPublisher(
        provider, KubernetesPublisherConfig(credential_host="git.example.com:8443")
    )._plan()
    assert plan.endpoints == ("git.example.com:8443",)


def test_the_script_checks_the_seal_before_it_contacts_any_remote() -> None:
    for source in ("stdin", "file"):
        script = scripts.publisher_script(
            clone_url="https://github.com/o/r.git",
            work_branch="crucible/X",
            base_ref="main",
            expected_head=HEAD,
            author_name="hades-worker",
            author_email="crucible-worker@users.noreply.github.com",
            token_source=source,
            bundle_sha256="b" * 64,
        )
        seal = script.index('ACTUAL=$(sha256sum "$BUNDLE"')
        assert seal < script.index("git fetch --quiet origin")
        assert seal < script.index("git push --quiet origin")
        assert f"exit {BUNDLE_SEAL_REFUSED}" in script
        push = next(line for line in script.splitlines() if "git push" in line)
        assert "--force " not in script and " -f" not in push and ":+" not in push
        assert '--force-with-lease="refs/heads/$WORK_BRANCH:$REMOTE"' in script
    with pytest.raises(ValueError, match="unknown token source"):
        scripts.publisher_script(
            clone_url="u",
            work_branch="b",
            base_ref="main",
            expected_head=HEAD,
            author_name="a",
            author_email="e",
            token_source="env",
        )


async def test_a_push_whose_outcome_cannot_be_read_back_still_reports_the_push() -> None:
    """The script exits 0 only after its push; a reader Pod that cannot be created then
    must not turn a branch already on the remote into a failed publication. The
    supervisor's own remote-head check is what confirms it."""
    api, _provider, publisher = _setup()
    api.on_push = lambda _push: api.refuse_create.add("pods")
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert (outcome.pushed, outcome.head_sha, outcome.step, outcome.exit_code) == (
        True,
        HEAD,
        "done",
        0,
    )
    assert "could not be read" in outcome.detail
    assert not api.secret_exists(token_secret_name(ATTEMPT))


async def test_a_pod_slow_to_be_removed_does_not_fail_a_finished_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, provider, publisher = _setup()
    gone = provider._await_job_pods_gone

    async def lingering(
        name: str, *, timeout: float = 15, force: bool = False, collection: bool = False
    ) -> None:
        if name != job_name(ATTEMPT) and not name.startswith("cleaner"):
            return await gone(name)
        raise ProviderError(f"Pods for Job {name!r} were still present after {timeout:g} seconds")

    monkeypatch.setattr(provider, "_await_job_pods_gone", lingering)
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert outcome.pushed and outcome.step == "done"
    assert not api.secret_exists(token_secret_name(ATTEMPT))
    # Every other role still treats a lingering Pod as the error it is.
    with pytest.raises(ProviderError, match="still present"):
        await provider._run_role_job(
            _request_spec(),
            role=k8sspec.ROLE_CLEANER,
            image=DIGEST_IMAGE,
            script="exit 0\n",
            mounts=[],
            volumes=[],
            limits=provider._limits(_request_spec()),
            timeout=30,
            plan=k8sspec.EgressPlan(),
        )


def _request_spec() -> Any:
    from crucible.adapters.execution.publisher import request_spec  # noqa: PLC0415

    return request_spec(_request())


def test_the_script_asks_the_helper_for_the_token_before_any_remote() -> None:
    for source in ("stdin", "file"):
        script = scripts.publisher_script(
            clone_url="https://github.com/o/r.git",
            work_branch="crucible/X",
            base_ref="main",
            expected_head=HEAD,
            author_name="hades-worker",
            author_email="crucible-worker@users.noreply.github.com",
            token_source=source,
            bundle_sha256="b" * 64,
        )
        check = script.index("git credential fill")
        assert check < script.index("git fetch --quiet origin")
        assert check < script.index("git push --quiet origin")
        # Only whether a password came back: the answer goes to grep and nowhere else.
        line = script[check : script.index("\n", check)]
        assert "| grep -q '^password=.'" in line
        assert "printf 'protocol=https\\nhost=%s\\n\\n'" in script


def _run_leaf_script(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", scripts.publish_leaf_script(str(root))],
        capture_output=True,
        text=True,
        check=False,
    )


def _claim_with_output(root: Path, *, mode: int = 0o750) -> Path:
    output = root / "output"
    output.mkdir()
    (output / "work_branch.bundle").write_bytes(BUNDLE)
    output.chmod(mode)
    return output


@pytest.mark.parametrize("mode", [0o750, 0o2750])
def test_the_leaf_script_makes_publish_owned_and_moded_as_output_is(
    tmp_path: Path, mode: int
) -> None:
    """The kubelet is never left to create the `publish` subPath: the script does, as the
    worker uid, with the owner and access bits `output/` has (hades PR 225 review)."""
    output = _claim_with_output(tmp_path, mode=mode).stat()
    result = _run_leaf_script(tmp_path)
    assert result.returncode == 0, result.stderr
    made = (tmp_path / "publish").stat()
    assert stat.S_ISDIR(made.st_mode)
    assert (made.st_uid, made.st_mode & 0o777) == (output.st_uid, output.st_mode & 0o777)
    # The setgid bit grants no access; it decides the group new files get. On a Pod's
    # fsGroup volume the claim root, and so pytest's tmp_path under /tmp, carries it
    # (hades #184), and the leaf keeps it whether it came from `output/` or from the
    # parent, so the publisher's files stay in the fsGroup as the preparer's do.
    parent = tmp_path.stat()
    expected = (output.st_mode | parent.st_mode) & stat.S_ISGID
    assert stat.S_IMODE(made.st_mode) & ~0o777 == expected
    # A second push of the same attempt finds the leaf already there.
    assert _run_leaf_script(tmp_path).returncode == 0


def test_the_leaf_script_refuses_a_missing_bundle_or_a_leaf_that_is_not_a_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "output").mkdir()
    result = _run_leaf_script(tmp_path)
    assert result.returncode == 7 and "no branch bundle" in result.stderr
    assert not (tmp_path / "publish").exists()

    (tmp_path / "output" / "work_branch.bundle").write_bytes(BUNDLE)
    (tmp_path / "publish").write_bytes(b"not a directory")
    result = _run_leaf_script(tmp_path)
    assert result.returncode == 8 and "not a directory" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a directory whatever its mode")
def test_the_leaf_script_refuses_a_publish_leaf_the_worker_uid_cannot_write(
    tmp_path: Path,
) -> None:
    """What a root-made, root-squashed subPath looks like to the worker uid: there, but
    not writable. The script says so rather than letting the push fail inside the Pod."""
    _claim_with_output(tmp_path, mode=0o555)
    (tmp_path / "publish").mkdir(mode=0o555)
    try:
        result = _run_leaf_script(tmp_path)
    finally:
        (tmp_path / "output").chmod(0o755)
        (tmp_path / "publish").chmod(0o755)
    assert result.returncode == 8
    assert "not owned and writable as output is" in result.stderr


async def test_the_publish_leaf_is_made_by_the_worker_uid_before_the_publisher_pod() -> None:
    api, _provider, publisher = _setup()
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert outcome.pushed, outcome
    jobs = _created(api, "jobs")
    roles = [body["metadata"]["labels"][k8sspec.LABEL_ROLE] for body in jobs]
    assert roles == [k8sspec.ROLE_PREPARER, k8sspec.ROLE_PUBLISHER]
    leaf_pod = jobs[0]["spec"]["template"]["spec"]
    container = leaf_pod["containers"][0]
    assert scripts.PUBLISH_LEAF_MARKER in container["command"][-1]
    assert leaf_pod["securityContext"]["runAsUser"] == k8sspec.WORKER_UID
    claim_mounts = [m for m in container["volumeMounts"] if m["name"] == "ws"]
    assert [(m["mountPath"], m.get("subPath")) for m in claim_mounts] == [(WORK_MOUNT, None)]
    assert "publish-token" not in json.dumps(leaf_pod)
    # No network for it: no policy of its own, so the namespace's default deny holds.
    assert k8sspec.ROLE_PREPARER not in [
        body["metadata"]["labels"][k8sspec.LABEL_ROLE] for body in _created(api, "networkpolicies")
    ]


async def test_a_publish_leaf_that_cannot_be_made_is_refused_by_name_before_a_token() -> None:
    api, _provider, publisher = _setup()
    api.publish_leaf_unwritable = True
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert not outcome.pushed
    assert outcome.step == "publish-leaf"
    assert "publish/ leaf of the workspace claim" in outcome.detail
    assert f"ws-{ATTEMPT.lower()}" in outcome.detail
    assert _created(api, "secrets") == []
    assert k8sspec.ROLE_PUBLISHER not in [
        body["metadata"]["labels"][k8sspec.LABEL_ROLE] for body in _created(api, "jobs")
    ]
    assert api.pushes == []


async def test_a_missing_bundle_is_refused_before_a_subpath_is_mounted() -> None:
    """The bundle is mounted as a single-file subPath; a missing one would have the
    kubelet make a directory there. The leaf Job finds it missing first."""
    api, _provider, publisher = _setup(bundle=None)
    outcome = await publisher.push(_request(), _token(installation_token_value()))
    assert not outcome.pushed and outcome.step == "bundle-seal"
    assert f"no branch bundle at {BUNDLE_LEAF}" in outcome.detail
    assert _created(api, "secrets") == []
    assert k8sspec.ROLE_PUBLISHER not in [
        body["metadata"]["labels"][k8sspec.LABEL_ROLE] for body in _created(api, "jobs")
    ]


# ----- hades #411: merge-main in a publisher Job -----------------------------------


async def test_a_clean_merge_main_is_one_publisher_job_that_pushes_with_a_lease() -> None:
    value = installation_token_value()
    api, _provider, publisher = _setup()

    outcome = await publisher.merge_main(_merge_request(), _token(value))

    assert outcome.merged, outcome
    assert (outcome.head_sha, outcome.conflicting_files) == (api.merge_main_head, ())
    assert [(m["branch"], m["base"], m["lease"], m["token"]) for m in api.merges] == [
        ("crucible/EX-0001", "main", HEAD, value)
    ]
    assert [(p["branch"], p["head"]) for p in api.pushes] == [
        ("crucible/EX-0001", api.merge_main_head)
    ]
    job = _publisher_job(api)
    container = job["spec"]["template"]["spec"]["containers"][0]
    script = container["command"][-1]
    assert scripts.MERGE_MAIN_MARKER in script
    assert '--force-with-lease="refs/heads/$WORK_BRANCH:$EXPECTED"' in script
    # No bundle: the remote tip is what is merged. The token arrives as for a push.
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert f"{scripts.BUNDLE_MOUNT}/work_branch.bundle" not in mounts
    assert mounts[scripts.TOKEN_MOUNT]["name"] == "publish-token"
    for row in api.created:
        if row["kind"] != "secrets":
            assert value not in json.dumps(row["body"]), row["kind"]
    assert ("secrets", token_secret_name(ATTEMPT)) in api.deleted


async def test_a_conflicting_merge_main_reports_the_files_and_pushes_nothing() -> None:
    api, _provider, publisher = _setup()
    api.merge_main_conflicts = ["crucible/application/delivery_tick.py", "docs/spec/23.md"]

    outcome = await publisher.merge_main(_merge_request(), _token(installation_token_value()))

    assert not outcome.merged
    assert outcome.conflicting_files == (
        "crucible/application/delivery_tick.py",
        "docs/spec/23.md",
    )
    assert outcome.step == "conflict"
    assert outcome.exit_code == scripts.MERGE_MAIN_CONFLICT
    assert api.pushes == []


async def test_a_merge_main_without_its_workspace_claim_is_refused_before_a_token() -> None:
    api, _provider, publisher = _setup(claim=False)

    outcome = await publisher.merge_main(_merge_request(), _token(installation_token_value()))

    assert (outcome.merged, outcome.step) == (False, "claim")
    assert _created(api, "secrets") == []
