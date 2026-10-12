"""Red main at 2e467bf, after #639: the kind tier's shard 3 failed.

#639 itself did not break it. #637 (ADR 0032) made the Kubernetes publisher read the
sealed bundle from `provider.artifact_store` and refuse a push when the provider has
none. A deployment gets that store from `wiring.kubernetes_provider`, but the kind
tier's providers were built without one, so `test_fdy_0133_...` (shard 3) ended in
`publish_failed`. #637 touched no kind-class path, so the tier did not run until #639
touched `CONTRIBUTING.md`, an unclassified path that runs everything (hades #476).

These cases pin, without a cluster: the kind app gives its provider the store that
collection writes to, every kind test that pushes uses such a provider, and a provider
given that store publishes the bundle collection stored where one without it refuses.

Finding 01M4QFXTN0EDD6PX84FYYQ1623 closed the CI gap itself: a change to the Kubernetes
provider or publisher modules now classifies as kind, so the tier runs on the change
that touched them rather than on the next one to touch an unclassified path."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.domain.change_class import classify
from tests.integration.fake_github import installation_token_value
from tests.unit.test_kubernetes_publisher import BUNDLE, _request, _setup, _token

REPO = Path(__file__).resolve().parents[2]
KIND_TESTS = REPO / "tests/e2e/test_kind.py"
SHARD_3 = REPO / "tools/kind/shards/3.txt"
PUSH_TEST = "test_fdy_0133_a_task_in_publishing_is_pushed_by_the_kubernetes_publisher"


def _functions() -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    tree = ast.parse(KIND_TESTS.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name
    ]


def _gives_provider_the_app_store(function: ast.AST) -> bool:
    """`provider.artifact_store = <name>` and `AppContext(..., artifact_store=<name>)`."""
    assigned = {
        node.value.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Name)
        and any(
            isinstance(target, ast.Attribute)
            and target.attr == "artifact_store"
            and isinstance(target.value, ast.Name)
            and target.value.id == "provider"
            for target in node.targets
        )
    }
    passed = {
        keyword.value.id
        for call in _calls(function, "AppContext")
        for keyword in call.keywords
        if keyword.arg == "artifact_store" and isinstance(keyword.value, ast.Name)
    }
    return bool(assigned) and assigned == passed


def test_the_kind_app_gives_its_provider_the_store_collection_writes() -> None:
    functions = _functions()
    assert _gives_provider_the_app_store(functions["_kind_app"])
    assert _gives_provider_the_app_store(
        functions["test_rows_5_7_11_23_supervisor_restart_and_full_gate_lifecycle"]
    )


def test_every_kind_push_runs_on_a_provider_the_kind_app_gave_a_store() -> None:
    functions = _functions()
    pushing = {
        name: node for name, node in functions.items() if _calls(node, "KubernetesPublisher")
    }
    assert PUSH_TEST in pushing and PUSH_TEST in SHARD_3.read_text(encoding="utf-8")
    for name, node in pushing.items():
        [app] = _calls(node, "_kind_app")
        app_provider = app.args[3]
        for publisher in _calls(node, "KubernetesPublisher"):
            pushed_by = publisher.args[0]
            assert isinstance(app_provider, ast.Name) and isinstance(pushed_by, ast.Name), name
            assert app_provider.id == pushed_by.id, name


async def test_a_provider_given_the_collection_store_pushes_and_one_without_refuses(
    tmp_path: Path,
) -> None:
    collected = DiskArtifactStore(tmp_path / "kind-store")
    stored = collected.put(BUNDLE)
    request = _request(
        bundle_artifact=stored.path, bundle_sha256=hashlib.sha256(BUNDLE).hexdigest()
    )

    _api, provider, publisher = _setup()
    provider.artifact_store = None
    refused = await publisher.push(request, _token(installation_token_value()))
    assert (refused.pushed, refused.step) == (False, "bundle-seal")
    assert "artifact store" in refused.detail

    _api, provider, publisher = _setup()
    provider.artifact_store = collected
    pushed = await publisher.push(request, _token(installation_token_value()))
    assert pushed.pushed, pushed


def test_the_red_change_ran_the_kind_tier_and_now_so_does_the_one_that_broke_it() -> None:
    """Why #639 showed what #637 broke: only #639 touched a path that ran kind. #637's
    own paths, the Kubernetes publisher and its unit tests, classified as core, so the
    only real-cluster publishing test never ran on it. Finding 01M4QFXTN0EDD6PX84FYYQ1623:
    those paths are kind-triggering now, so the same change would run the tier."""
    assert classify(["CONTRIBUTING.md", "crucible/adapters/github/client.py"]).run_kind
    broke_it = classify(
        ["crucible/adapters/execution/k8spublisher.py", "tests/unit/test_work_bundle_artifact.py"]
    )
    assert broke_it.run_kind
    assert broke_it.label == "kind"
    assert not broke_it.run_images and not broke_it.run_everything
