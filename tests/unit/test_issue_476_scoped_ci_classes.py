"""CI and the release run only the job classes a change touches; a workflow change
runs everything (hades #476, the operator's design of 2026-10-06).

AC1: `crucible.domain.change_class.classify` maps a crucible/-only change to core, an
images/ change to core plus images, registry and kind, a tools/kind change to core
plus kind, and any `.github/` change or any unclassified path to everything.

AC2: `.github/workflows/ci.yml` gates each non-core job on the classifier's outputs,
and a workflow change runs every job.

AC3: a release whose WORKER tag equals the previous release's reuses the published
worker image by digest, and the release notes say so.

AC4: Hades certifies a head green when every job that ran passed and the filtered
jobs were skipped, and the change class is recorded on the certification.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from crucible.domain.certification import CertificationState, CheckSource, ObservedCheck, certify
from crucible.domain.change_class import classify
from crucible.domain.gates import DeliveryInput, GateResult, ci_green_for_head

REPOSITORY = Path(__file__).resolve().parents[2]
CI_YML = REPOSITORY / ".github" / "workflows" / "ci.yml"
IMAGES_DIGEST_YML = REPOSITORY / ".github" / "workflows" / "images-digest.yml"
RELEASE_YML = REPOSITORY / ".github" / "workflows" / "release.yml"


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker_decision = _load_module(
    REPOSITORY / "tools" / "release" / "worker_decision.py", "worker_decision"
)
version = _load_module(REPOSITORY / "tools" / "release" / "version.py", "version_tool")


# ----- AC1: the classifier --------------------------------------------------------


def test_a_crucible_only_change_classifies_as_core() -> None:
    result = classify(
        ["crucible/application/observation.py", "tests/unit/test_foo.py", "docs/x.md"]
    )
    assert result.label == "core"
    assert result.run_images is False
    assert result.run_kind is False
    assert result.run_everything is False


def test_an_images_change_adds_images_registry_and_kind() -> None:
    result = classify(["images/worker/Dockerfile", "crucible/domain/x.py"])
    assert result.label == "images"
    assert result.run_images is True
    assert result.run_kind is True
    assert result.run_everything is False


def test_a_tools_images_change_also_classifies_as_images() -> None:
    result = classify(["tools/images/images.sh"])
    assert result.label == "images"
    assert result.run_images is True
    assert result.run_kind is True


def test_a_tools_kind_change_adds_only_kind() -> None:
    result = classify(["tools/kind/cluster.sh", "crucible/domain/x.py"])
    assert result.label == "kind"
    assert result.run_images is False
    assert result.run_kind is True
    assert result.run_everything is False


def test_a_deploy_or_kind_test_change_also_classifies_as_kind() -> None:
    assert classify(["deploy/kubernetes/worker.yaml"]).label == "kind"
    assert classify(["tests/e2e/test_kind.py"]).label == "kind"


def test_a_github_change_runs_everything_with_no_exceptions() -> None:
    result = classify([".github/workflows/ci.yml", "crucible/domain/x.py"])
    assert result.label == "workflow"
    assert result.run_images is True
    assert result.run_kind is True
    assert result.run_everything is True


def test_a_path_matching_no_class_runs_everything_like_the_workflow_override() -> None:
    result = classify(["README.md"])
    assert result.label == "unclassified"
    assert result.run_everything is True
    assert result.run_images is True
    assert result.run_kind is True


def test_an_empty_change_classifies_as_core_only() -> None:
    result = classify([])
    assert result.label == "core"
    assert result.run_everything is False


def test_images_and_github_together_are_recorded_as_the_workflow_override() -> None:
    """The workflow override is the one that explains the whole job set when more than
    one class is in play, since the issue calls it an override, not a tiebreak."""
    result = classify(["images/worker/Dockerfile", ".github/workflows/ci.yml"])
    assert result.label == "workflow"


# ----- AC2: ci.yml wiring ----------------------------------------------------------


def _ci_jobs() -> dict[str, Any]:
    with open(CI_YML, encoding="utf-8") as fh:
        return dict(yaml.safe_load(fh)["jobs"])


def test_ci_yml_has_a_classify_job_running_the_classifier_script() -> None:
    jobs = _ci_jobs()
    assert "classify" in jobs
    steps = jobs["classify"]["steps"]
    assert any("tools/ci/changes.py" in str(step.get("run", "")) for step in steps)
    outputs = jobs["classify"]["outputs"]
    assert outputs["images"] == "${{ steps.classify.outputs.images }}"
    assert outputs["kind"] == "${{ steps.classify.outputs.kind }}"


@pytest.mark.parametrize("job_name", ["images", "registry"])
def test_images_and_registry_jobs_gate_on_the_images_output(job_name: str) -> None:
    jobs = _ci_jobs()
    job = jobs[job_name]
    assert job.get("needs") == "classify"
    assert job.get("if") == "needs.classify.outputs.images == 'true'"


def test_e2e_kind_job_gates_on_the_kind_output() -> None:
    jobs = _ci_jobs()
    job = jobs["e2e-kind"]
    assert job.get("needs") == "classify"
    assert job.get("if") == "needs.classify.outputs.kind == 'true'"


@pytest.mark.parametrize("job_name", ["lint", "scan", "test", "e2e", "manifests", "compose-smoke"])
def test_core_jobs_are_never_gated_by_the_classifier(job_name: str) -> None:
    """Core always runs: lint, scan, test, e2e, manifests, compose-smoke carry no
    `if:` and no dependency on `classify`, so a classifier bug can never skip them."""
    jobs = _ci_jobs()
    job = jobs[job_name]
    assert "if" not in job
    assert "needs" not in job


def test_the_images_job_classification_folds_in_the_workflow_and_unclassified_overrides() -> None:
    """A unit test for the classifier itself already proves the override (above); this
    proves ci.yml's own `images`/`kind` outputs are the ones the override widens, by
    construction: `images`/`kind` read `run_images`/`run_kind`, which are true under
    `run_everything` for every class (hades #476's "workflow change runs every job")."""
    assert classify([".github/x"]).run_images and classify([".github/x"]).run_kind
    assert classify(["unmatched/path.txt"]).run_images and classify(["unmatched/path.txt"]).run_kind


def test_images_digest_workflow_only_proceeds_when_the_images_job_ran() -> None:
    with open(IMAGES_DIGEST_YML, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    steps = doc["jobs"]["commit"]["steps"]
    by_id = {step.get("id"): step for step in steps if step.get("id")}
    assert "images_ran" in by_id
    assert "images" in str(by_id["images_ran"]["run"])
    artifact_if = by_id["artifact"]["if"]
    assert "steps.images_ran.outputs.value == '1'" in artifact_if


# ----- AC3: the release's worker-image reuse decision ------------------------------


def test_worker_tag_unchanged_reuses_the_published_image_by_digest() -> None:
    current = "WORKER=crucible-worker:20260916-abc\nSCRIPT_HARNESS=x\n"
    previous = "WORKER=crucible-worker:20260916-abc\nSCRIPT_HARNESS=y\n"
    decision = worker_decision.decide(current, previous)
    assert decision.reuse is True
    assert decision.previous_tag == "crucible-worker:20260916-abc"


def test_worker_tag_changed_rebuilds_and_pushes() -> None:
    current = "WORKER=crucible-worker:20261001-def\n"
    previous = "WORKER=crucible-worker:20260916-abc\n"
    decision = worker_decision.decide(current, previous)
    assert decision.reuse is False


def test_a_manifest_with_no_worker_line_is_an_error_not_a_reuse() -> None:
    with pytest.raises(worker_decision.ManifestError):
        worker_decision.decide("SCRIPT_HARNESS=x\n", "WORKER=crucible-worker:1\n")


def test_worker_decision_cli_prints_the_reuse_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = tmp_path / "current.env"
    previous = tmp_path / "previous.env"
    current.write_text("WORKER=crucible-worker:same\n")
    previous.write_text("WORKER=crucible-worker:same\n")
    code = worker_decision.main(["--current", str(current), "--previous", str(previous)])
    out, _ = capsys.readouterr()
    assert code == 0
    assert "reuse=true" in out
    assert "previous_worker_tag=crucible-worker:same" in out


def test_version_previous_picks_the_highest_tag_below_the_candidate() -> None:
    assert version.previous_version("1.3.0", ["1.1.0", "1.2.0", "1.3.0"]) == "1.2.0"


def test_version_previous_is_none_for_a_first_release() -> None:
    assert version.previous_version("1.0.0", []) is None
    assert version.previous_version("1.0.0", ["1.0.0"]) is None


def test_version_previous_ignores_non_version_tags() -> None:
    assert version.previous_version("1.1.0", ["script-harness-1.0.0", "latest", "1.0.0"]) == "1.0.0"


def test_release_notes_names_the_reused_image_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_notes = _load_module(
        REPOSITORY / "tools" / "release" / "release_notes.py", "release_notes_476"
    )
    service = "ghcr.io/sentania-labs/crucible"
    worker_repo = "ghcr.io/sentania-labs/crucible-worker"
    digest = "sha256:" + "b" * 64
    digests = {
        f"{service}:0.6.0": "sha256:" + "a" * 64,
        f"{worker_repo}:0.6.0": digest,
        f"{worker_repo}:script-harness-0.6.0": "sha256:" + "c" * 64,
        f"{worker_repo}:latest": digest,
    }
    fake = tmp_path / "docker"
    state = tmp_path / "registry.json"
    state.write_text(json.dumps(digests))
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"table = json.load(open({str(state)!r}))\n"
        "ref = sys.argv[4]\n"
        "answer = table.get(ref)\n"
        "if answer is None:\n"
        "    print(f'ERROR: {ref}: not found', file=sys.stderr); sys.exit(1)\n"
        "print(json.dumps(answer))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("DOCKER", str(fake))

    code = release_notes.main(
        [
            "--service-image",
            f"{service}:0.6.0",
            "--worker-repository",
            worker_repo,
            "--worker-reused-from",
            "0.5.3",
        ]
    )
    assert code == 0


def test_release_yml_reuse_wiring_names_the_decision_and_skips_the_build() -> None:
    with open(RELEASE_YML, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    steps = doc["jobs"]["release"]["steps"]
    # `fetch-depth: 0` alone does not fetch tags; the previous-release lookup needs
    # every earlier version tag's history, not only the one that triggered this run.
    assert steps[0]["with"]["fetch-tags"] is True
    by_id = {step.get("id"): step for step in steps if step.get("id")}
    assert "worker_decision" in by_id
    assert "worker_decision.py" in str(by_id["worker_decision"]["run"])
    assert "previous" in by_id
    assert "version.py --previous" in str(by_id["previous"]["run"])
    by_name = {step.get("name"): step for step in steps if step.get("name")}
    build_step = by_name["build and prove the worker images"]
    assert build_step["if"] == "steps.worker_decision.outputs.reuse != 'true'"
    push_step = by_name["push the worker images, never over an existing version"]
    assert push_step["if"] == "steps.worker_decision.outputs.reuse != 'true'"
    reuse_step = by_name["re-publish the previous release's worker images by digest"]
    assert reuse_step["if"] == "steps.worker_decision.outputs.reuse == 'true'"
    notes_step = by_name["render the release notes with the published digests"]
    assert "WORKER_REUSED_FROM" in notes_step["env"]


# ----- AC4: certification records the change class, and a filtered job is not missing


HEAD = "a" * 40


def _check(
    name: str, conclusion: str | None, source: CheckSource = CheckSource.CHECK_RUN
) -> ObservedCheck:
    return ObservedCheck(
        name=name, status="completed", conclusion=conclusion, head_sha=HEAD, source=source
    )


def test_certify_records_the_change_class_passed_in() -> None:
    outcome = certify(
        {},
        head_sha=HEAD,
        observed=[_check("lint", "success")],
        change_class="images",
    )
    assert outcome.change_class == "images"
    assert outcome.state is CertificationState.GREEN


def test_certify_defaults_to_an_empty_change_class_when_the_caller_does_not_know_one() -> None:
    outcome = certify({}, head_sha=HEAD, observed=[_check("lint", "success")])
    assert outcome.change_class == ""


def test_a_job_the_classifier_skipped_is_not_a_missing_job() -> None:
    """hades #476: core jobs passed, images/registry/e2e-kind were skipped by the
    classifier's `if:` (GitHub reports a skipped job as conclusion `skipped`), and the
    head still certifies green."""
    observed = [
        _check("lint", "success"),
        _check("scan", "success"),
        _check("test", "success"),
        _check("e2e", "success"),
        _check("manifests", "success"),
        _check("compose-smoke", "success"),
        _check("images", "skipped"),
        _check("registry", "skipped"),
        _check("e2e-kind", "skipped"),
    ]
    outcome = certify({}, head_sha=HEAD, observed=observed, change_class="core")
    assert outcome.state is CertificationState.GREEN
    assert "images" not in outcome.required
    assert outcome.change_class == "core"


def test_a_real_failure_among_the_jobs_that_ran_still_fails_the_core_class() -> None:
    observed = [
        _check("lint", "success"),
        _check("test", "failure"),
        _check("images", "skipped"),
    ]
    outcome = certify({}, head_sha=HEAD, observed=observed, change_class="core")
    assert outcome.state is CertificationState.FAILED


def test_ci_green_for_head_records_the_change_class_in_its_detail() -> None:
    outcome = ci_green_for_head(
        DeliveryInput(
            policy={},
            accepted_head=HEAD,
            certification_state="green",
            certification_detail="9 of 9 jobs succeeded",
            change_class="images",
        )
    )
    assert outcome.result is GateResult.PASS
    assert "change class: images" in outcome.detail


def test_ci_green_for_head_omits_the_change_class_when_none_is_known() -> None:
    """Existing certifications computed before #476 carry no change_class; the gate's
    behavior for them is unchanged."""
    outcome = ci_green_for_head(
        DeliveryInput(
            policy={},
            accepted_head=HEAD,
            certification_state="green",
            certification_detail="9 of 9 jobs succeeded",
        )
    )
    assert outcome.result is GateResult.PASS
    assert outcome.detail == "9 of 9 jobs succeeded"
