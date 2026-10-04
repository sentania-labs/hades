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
3. Every job in the branch's CI run must succeed. Hades certifies that itself
   from the check runs on the head; there is no gate job and the ruleset on
   main names no required check. One internal review round
   happens before the PR opens (the orchestrator's review of the worker's
   branch). Codex reviews every PR once, automatically, and its findings get
   a disposition (fix, or an explanation) before merge; Codex is not
   re-requested after a fix. Hades squash-merges the certified head; there is no
   merge queue. Do not push to `main` directly.

## Kubernetes manifests

A change under `deploy/` runs `make manifests`. When it touches the workers
namespace or the provider, run `make deploy-kind` and the `e2e-kind` target.
Worker image changes: anything under `images/` changes the worker image, so
the tag and harness lines of `images/manifest.env` must match it
(`make lint` names the tag it expects; `make images` on a machine with Docker
rewrites the whole file). The `*_DIGEST` lines are CI's: never edit them by
hand or in a worker pass. On a branch, the CI images job commits the digest
it built when every tag and harness version reproduced, then runs CI again on
that commit (docs/spec/13-local-operation.md).

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
