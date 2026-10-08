# Contributing

The project is Hades (the package, CLIs and images still say `crucible`).

## Delivery pipeline

1. Branch from `main`. Nothing lands on `main` without a pull request.
2. Lint and test with the same definitions CI uses: `make lint` (ruff
   format and check, mypy --strict, import-linter, and the image manifest
   check) and `make test` (the unit tier, then the integration tier against
   a Postgres container); `make test PYTEST_WORKERS=0` runs serially. Run
   it with `make up` and exercise the change against the live API or `/ui`;
   describe in the PR what you saw working and what you could not exercise.
   Open the PR when the work is done, not to find out whether it works.
   When the author is a worker, see "When the author is a worker" below:
   the worker runs the lint, unit and scan tiers and the test file its
   contract names, and leaves the tiers that need Docker to CI.
3. Every job in the branch's CI run must succeed. Hades certifies that itself
   from the check runs on the head; there is no gate job and the ruleset on
   main names no required check. The worker's required report self-review
   covers documentation, every acceptance criterion with evidence, and anything
   knowingly left out and why. Passing blocking gates and a complete report
   let Hades record acceptance and publish. An advisory gate failure still requires an
   orchestrator review before automatic acceptance. Codex reviews every PR once,
   automatically, and its findings get
   a disposition (fix, or an explanation) before merge; Codex is not
   re-requested after a fix. Hades squash-merges the certified head; there is no
   merge queue. Do not push to `main` directly.

## When the author is a worker

A Crucible worker (a task on the `hades-self-hosting` policy) runs in the
worker image, which carries the check toolchain and nothing that drives a
container or a cluster (ADR 0020). Docker, kind and kubectl are absent in a
worker and are CI's; a missing one is expected and is not a reason to stop.
The tiers split by who has the tools:

- Worker-local tiers, run before the commit and listed in the report's
  checks: `make lint`, `make test-unit`, `make scan`, and the unit test file
  the contract names (`uv run pytest -q tests/unit/test_issue_<n>_<slug>.py`).
  These are the only checks a contract may require; the contract model
  refuses a `required_verification` command that runs `docker`, `kind` or
  `kubectl`, or a `make` target named for one of them, naming the program.
- CI-only tiers, run by the branch's CI on the pushed head: the integration
  tier (`make test-integration`, a Postgres container), the compose smoke
  (`make up` and `make smoke`), the Docker e2e tier (`make e2e`), the kind
  tier (`make e2e-kind`), and the image builds and digests (the `images` job
  and `images-digest.yml`). A worker does not run them, does not write a
  substitute for them, and does not write `blocked.md` because they are
  missing. The PR body says which of them the author could not exercise, and
  CI's run is the proof of record.

`make deploy-kind` is not in either list: CI does not run it, and a worker
cannot. A worker-authored change to the manifests relies on `make manifests`
and the `e2e-kind` job in CI, and says so in the PR body; a person with
Docker and kind runs `make deploy-kind` when the change warrants it.

## Kubernetes manifests

A change under `deploy/` runs `make manifests`. When it touches the workers
namespace or the provider, an author with Docker and kind runs
`make deploy-kind` and the `e2e-kind` target; a worker leaves both to CI
(see "When the author is a worker").
Worker image changes: anything under `images/` changes the worker image, so
the tag and harness lines of `images/manifest.env` must match it
(`make lint` names the tag it expects; `make images` on a machine with Docker
rewrites the whole file). A worker edits only the tag line(s) that
`images/check-manifest.sh` reports, by hand, and never builds the image. The
`*_DIGEST` lines are CI's: never edit them by hand or in a worker pass. On a
branch, when every tag and harness version reproduced, CI commits the digest
it built and runs CI again on that commit.
That is two workflows, so that no branch code ever holds a write token: the
`images` job in `ci.yml` runs the branch's scripts with the read-only token
and only uploads the built digest lines as the `images-digests` artifact;
`images-digest.yml`, which `workflow_run` starts from `main`, runs only
`main`'s `tools/images/digest_commit.py`, re-checks the artifact against the
branch's manifest, then commits, pushes and dispatches CI. A change to
`digest_commit.py` therefore takes effect for the commit half only once it is
on `main` (docs/spec/13-local-operation.md).

## Releases

Releases are annotated `vMAJOR.MINOR.PATCH` tags on `main`. The tag is the
release; the merge is not. Pushing the tag runs the release workflow
(`.github/workflows/release.yml`): it refuses a tag that is not on `main`,
builds the service and worker images, smokes them, publishes the images and
the bundle, and creates the GitHub release. Watch that run; the tag is not
published until it finishes.

## Style

Typed Python 3.12, `ruff` formatting, `mypy --strict`, no em-dashes in
prose, comments, or commit messages. Use plain words. Use local Central
time in operator-facing text.

## Migrations

A migration that has been applied to any database, including a
developer's, is never edited. Schema changes are a new revision. Before the
first tagged release the initial revision may be squashed, only together with a
`make reset` (compose down with volumes) called out in the PR, because every
existing database is wrong after a squash. Readiness compares the live schema to
the ORM and reports "schema drift" when they differ; that check exists because
revision 0001 was once rewritten in place after it had been applied.

Integration tier: `make test-integration` runs `tests/integration` with `-n auto`
by default. The pytest controller starts one Postgres container before tests run
(so a cold image pull is outside individual test timeouts), passes its URL to all
xdist workers through hooks registered in `tests/conftest.py` (including broad
`pytest tests -n auto` and `pytest -n auto` invocations), and stops it at controller
shutdown after the workers finish.
Each worker creates and drops its own database using `worker_database_name`;
a serial run uses one container and one database. `CRUCIBLE_TEST_DATABASE_URL`
uses an existing server instead, with the same database isolation and without
starting or stopping a container.

## The kind tier runs as shards

CI runs `tests/e2e/test_kind.py` as three jobs, each on its own disposable cluster,
selected from `tools/kind/shards/1.txt`, `2.txt` and `3.txt` (one pytest node id per
line). A new kind test goes into one of those files, balanced by how long it runs;
`tests/unit/test_kind_shards.py` fails when a test is in no shard or in two. Locally
`make e2e-kind` still runs the whole file; `CRUCIBLE_E2E_KIND_SHARD=2 make e2e-kind`
runs one shard. Every test's time is in the job log (`--durations=0`), which is the
data for rebalancing. CI runs on every push, main included: the run on main proves the
squashed result and builds the images from scratch, saving the BuildKit cache branches
restore.

## Flaky tests

A job that fails on attempt 1 and passes on a later attempt at the same commit is a
flake, not a success: nothing about the code changed between attempts, only the
result did. The `test`, `e2e` and `e2e-kind` jobs keep their JUnit XML as CI artifacts,
and a weekly scheduled workflow (`.github/workflows/flakes.yml`, `tools/ci/flakes.py`,
also runnable locally as `make flakes REPO=owner/repo`) reads them, names the tests
that flaked, and opens or updates one issue per flaky test, labelled `flaky`, with the
flake rate (reruns per 100 runs); it also keeps a weekly summary issue with the
overall rate.

Never mark a test flaky in code as a fix: not a retry wrapper, not a widened
assertion, not a skip, not a rerun marker. A test that flakes gets fixed, or its wait
or polling gets rewritten to wait on the real condition instead of a sleep. The
tracking issue records the flake; it is not a substitute for fixing it.

## Harness changes

The worker carries five production harnesses: Claude Code (`claude_code`), Codex
(`codex`), AGY (`agy`), Hermes (`hermes`) and Qwen Code (`qwen_code`). The script
harness is only a test fixture. See the [README harness table](README.md#the-harnesses).
Qwen Code 0.25.0 uses Node 22 and the same read-only gateway credential as Hermes.
Its wrapper writes `model.maxToolCallsPerTurn: 0` and
`model.generationConfig.contextWindowSize` in `~/.qwen/settings.json` before exec.
The latter is the routing entry's positive `context_length`, default 131072,
so Qwen budgets output within the engine window. Keep settings and stream-json
fixtures aligned with the pinned release. Image builds and version smoke checks
run in CI; leave digest lines to CI and promote each harness separately on Images.
