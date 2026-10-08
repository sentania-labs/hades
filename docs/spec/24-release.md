# 24. Release contract and release lifecycle

Foundry decides that merged changes form a release and proposes it. The
operator authorizes it. Crucible performs the deterministic mechanics:
verify the gates, create and push the annotated tag, observe the
repository's own tag-triggered release workflow, record the outcome. Foundry
never pushes a tag. Crucible never decides a release should occur.

Domain design is in scope now; implementation is phase C7 (20), after the
worker-supervision readiness gate.

## Normal flow

1. The operator reviews and merges one or more ready PRs on GitHub.
2. Crucible observes and records those merges (23).
3. Foundry determines whether the merged changes form a releasable batch.
4. Foundry proposes version, included changes, and summary to the operator.
5. The operator approves in their own words. Foundry records that as a
   `Decision` of kind `release_authorization` carrying the verbatim text,
   the operator's identity, the repository, the version, and the target
   SHA (`authorization_recorder: orchestrator_relay`, the default). A
   stricter policy (`operator_token`) requires the operator's own
   principal to record it.
6. Foundry submits a `ReleaseContractV1` referencing that decision.
7. Crucible verifies the release gates.
8. Crucible creates and pushes the annotated tag through a publisher
   container (23) with a token minted for the job.
9. The tag triggers the repository's established release workflow.
10. Crucible observes the workflow run and records evidence and result.
11. Foundry reports the outcome.

## ReleaseContractV1

```yaml
schema_version: "1.0"
external_id: "FDY-0057"
repository: { name: "example-service" }
target_branch: "main"
target_sha: "0123abcd..."              # exact; verified still current before tagging
version: "1.4.0"
tag: "v1.4.0"
included:
  pull_requests: [18, 19]
  issues: ["https://github.com/example-org/example-service/issues/17"]
notes: |
  Release summary or notes, rendered into the tag message and, if the
  policy says so, checked against the changelog.
required_gates: ["all"]                # or a named subset; may not remove policy-required gates
authorization: { decision_id: "01J..." }
```

Immutable once submitted. A change is a new contract.

## Gates (deterministic, evaluated in `verifying`)

| Gate | Passes when |
|---|---|
| `authorization_present` | the referenced decision exists, is kind `release_authorization`, names this repository, this version, and this target SHA, carries the operator's verbatim words and identity, and was recorded by a principal the policy's `authorization_recorder` accepts |
| `included_prs_merged` | every included PR is `merged` in Crucible's record and on GitHub, with merge SHA reachable from `target_sha` |
| `target_sha_current` | the remote `target_branch` head equals `target_sha` at verification time and again immediately before the push |
| `ci_green_for_target` | required checks are green for `target_sha` |
| `tag_matches_version` | `tag` equals `tag_pattern` rendered with exactly the contract's `version` components (for `v{major}.{minor}.{patch}` and version `1.4.0`, only `v1.4.0`); any other tag name fails, whatever else is true |
| `version_increases` | `version` parses as semver and is greater than the highest existing tag matching `tag_pattern` |
| `version_files_agree` | every path in the policy's `version_files` contains `version` at `target_sha` |
| `changelog_satisfied` | when `changelog_required`: the changelog at `target_sha` has an entry for `version` |
| `tag_absent` | the tag does not exist on the remote |
| `evidence_present` | every included PR has its internal review, external review dispositions, and CI certification records |
| `worker_image_policy` | the release workflow's policy check passes: the worker image carries every program a shipped policy's required check starts with or declares (`make images-policy-check`), verified between the build and the first push |

Any failure moves the release to `gates_failed` and wakes Foundry. Nothing
is pushed.

## Tagging

Annotated tag, message from `notes`, tagger identity from policy, pushed
by a publisher container holding a token minted for this job. Events before
and after. On success `tagged`; on a push rejection `gates_failed` with the
reason (the remote moved, the tag appeared). Never `--force`.

## Observation

Crucible watches workflow runs triggered by the tag (webhook and poll, as
in 23), records the run URL, conclusion, and artifacts list, and moves the
release to `succeeded` or `workflow_failed`. Both wake Foundry. A failed
release workflow is an escalation, never an automatic retry or re-tag.
Included tasks move from `release_candidate` to `released` on success and
back to `merged` on failure or cancellation.

## What this is not

Not an approval workflow system. One decision, one contract, one tag. A
standing release policy (`require_operator_approval: false` for a
repository) may be added later by the operator and is itself a recorded
decision.

Not a deployment trigger. `deploy/kubernetes` in this repository is examples that
track `latest` and never pin themselves; a real deployment copies the lab overlay
into the deployer's own GitOps repository and pins the tag this release cuts there
(13, docs/deployment.md). A tag here changes nothing a cluster is running.

**The worker image publishes under this release's version, the same as the
service image, and is pushed the same way, with `docker push`** (the operator's
decisions, 2026-09-23, 13): the tag workflow pushes `crucible-worker` and the
script-harness image under the contract's `version`, never over an existing
version tag, and its one "move latest" step moves the service image's `latest`
and the worker images' `latest` and `script-harness-latest` together, only when
that version is the highest one published (`tools/release/version.py`), each
copied on the registry from its own version tag rather than pushed from a local
build (2026-09-23). A lower version released after a higher one, or an old
release job re-run, leaves `latest` where it is, which the run log and the
release notes say plainly. The release body records all three images as
`name:<version>@<digest>`, read back from the registry after the push, so a
deployer pins from the release, never from a local guess.

**The worker and script-harness images are rebuilt only when their own tag
changed (hades #476, the operator's design of 2026-10-06, corrected per finding
01M4CG0K1TQH50GM73R9H9CQRB).** Before building, the tag workflow finds the
previous release (the highest `vMAJOR.MINOR.PATCH` tag below this one;
`tools/release/version.py --previous`) and reads that release's own committed
`images/manifest.env`. `tools/release/worker_decision.py` compares both the
`WORKER` tag and the `SCRIPT_HARNESS` tag with this release's, independently:
when both are unchanged, the release skips the image build and push entirely
and instead copies the previous release's already-published worker and
script-harness images to this version's tags with `docker buildx imagetools
create`, by digest, the same way the "move latest" step above moves tags
without pushing from a local build; when either tag changed, it builds and
pushes both. `tools/images/images.sh` builds the two images together and is
not split by this change, so a harness-only change still rebuilds the worker
alongside it rather than silently republishing the previous release's
script-harness image under a new version -- there is no way to build only one
of the two without editing `tools/images/images.sh`, which is out of scope for
this contract. Either way the candidate's own crane still resolves the
published worker image (Gate 3, part three) before anything is tagged. The
release notes name a reused image explicitly (`- the worker image is unchanged
since ...; republished ... by digest, not rebuilt`), so a reader never mistakes
a copy for a fresh build; that note, like the reuse decision itself, only fires
when both tags are unchanged. This does not change what a job tests, and it is
not the per-harness promotion flow on Images; both are explicitly out of scope
for #476. Proven directly at `tests/unit/test_issue_476_scoped_ci_classes.py`.

## Retention of `ci-*` proof tags on GHCR

Image listing stopped resolving `ci-*` proof tags (111), but nothing pruned the
versions behind them, and they accumulate in the `crucible-worker` package's
storage forever. A daily scheduled workflow
(`.github/workflows/ghcr-ci-tag-retention.yml`) deletes them: `tools/registry/
ci_tag_retention.py` lists the package's versions through the GitHub Packages
API, selects the ones whose tags are all `ci-*` and whose `created_at` is older
than N days, and deletes exactly those.

Never selected, whatever its age: a version carrying any tag that is not `ci-*`
(a release version or `latest`), an untagged version, or a version whose digest
is named by a `*_DIGEST` line of `images/manifest.env` on `main` or appears in a
GitHub release body for this repository (a release pins and republishes images
by digest, above).

`--dry-run` is the script's default: it prints every version it would delete
and deletes nothing, until the caller passes `--execute`. The workflow calls it
with `--execute` and the run's own token, scoped to `packages: write`, which is
the narrowest permission the delete needs (`contents: read` to check out the
script and `images/manifest.env`); the delete call itself logs the version it
is about to remove before removing it, so the run's own log is the record of
what it deleted.

N=14 days. Decision: Foundry, on Scott's delegation of 2026-10-07 5:22 PM
("You know the vision- choose answers that align to the vision") (140).
