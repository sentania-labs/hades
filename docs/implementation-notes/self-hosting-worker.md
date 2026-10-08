# Hades works on its own repository from a worker (hades #184, part of #85, FDY-0131)

## Decision

The operator, 2026-09-28, on FDY-0131: every task against this repository failed
`verification_ran`, because the worker image lacked what `make lint`, `make test` and
`make scan` call here. Deliver the toolchain in the worker image, a way to the locked
dependencies without opening worker egress beyond named hosts, and a policy for this
repository whose required checks are `make lint`, `make test-unit` and `make scan`. The integration and e2e tiers
need Postgres and Docker, and branch CI remains the full proof of record (the operator's
decision, 2026-09-28). ADR 0020 records the choices.

## What changed (2026-09-28)

- **The image.** uv 0.10.12, CPython 3.12.13 and gitleaks 8.30.1 in
  `images/worker/Dockerfile`, pinned in `images/pins.env`; both image tags moved and
  `images/manifest.env` was rewritten by `make images`. `make images-check NO_CACHE=1`
  reproduced both digests with every RUN step rebuilt.
- **Dependencies.** Option (a) of the contract: PyPI at run time, `uv sync --frozen`,
  through the policy's allowlist and the provider's hostAliases pinning (#191). Why not
  (b) is in ADR 0020.
- **The policy.** `examples/policies/hades-self-hosting.yaml`, uploaded through the
  policies API with the routing policy the default-software in force names
  (docs/deployment.md). `repository.required_programs` is new in 05b, a declaration
  `make images-policy-check` checks against the image.
- **The three targets in the image.** `make lint` and `make scan` needed nothing: in a
  Crucible checkout `origin/main` exists (the worker's clone keeps its remote-tracking
  ref, and the verifier's tree is cloned from that checkout, whose local `main` becomes
  its `origin/main`), and `check-image-manifest` only needs bash, jq and tar. What
  failed was the unit tier: `pgrep`, the Docker CLI and a mode check that did not allow
  for a Pod's setgid `/tmp`. `scan-history` now names a missing `origin/main` instead of
  failing inside gitleaks. `-n auto` follows the Pod's CPU quota (tests/conftest.py).
- **Evidence.** The verifier writes each check's wall-clock seconds; `verification_run`
  evidence carries them as `seconds`.
- **Routing.** Nothing reads a policy's name on the submit, start or selection path.
  `tests/integration/test_self_hosting_policy.py` uploads the policy the documented way
  and a task under it selects the Hermes gateway model `fast`. The admin UI's routing
  pages still write only default-software versions, so this policy follows a routing
  change only when its next version is uploaded.
- **No new tunable.** The two `UV_*` variables are constants of the image; the new
  `CRUCIBLE_COMPOSE_REQUIRED` and `CRUCIBLE_E2E_KIND_*` variables belong to the test
  tiers.

## The integration tier in the worker (hades #558, #85)

The policy's `services: [{kind: postgres}]` (05b) gives each attempt the database
`make test-integration` needs: on Kubernetes a native sidecar of the worker Job, on
Docker a container in the worker's network namespace, both reached as
`CRUCIBLE_TEST_DATABASE_URL=postgresql://crucible:crucible@127.0.0.1:5432/crucible`,
which `tests/integration/postgres.py` reads (a bare `postgresql://` is read as psycopg).
The image is the postgres:16 digest the CI workflow's integration tier runs. A task
against this repository can therefore run `make test-integration` in the worker and
read its own failures before CI does; `required_checks` stays `make lint`, `make
test-unit` and `make scan`, because the verifier re-runs required checks without a
service and branch CI remains the proof of record. The attempt's launch evidence
records the service and its digest. A CI failure on the `test` job also names the
failing tests from the `junit-test-<attempt>` artifact on the `ci_certification_failed`
wake (23), once the GitHub client can read workflow artifacts; until then the wake says
the artifact was not read.

## The kind proof

`make e2e-kind-self-hosting` (tests/e2e/test_kind_self_hosting.py): a Calico kind
cluster, the combined worker image, the real per-worker NetworkPolicy with no broad
egress, a stub model Pod reached as the lab's gateway is, a Hermes task under the policy
against a bare copy of this repository at HEAD. On 2026-09-28 at 9:58 PM, at commit
1f45019 with worker image `crucible-worker:20260916-17c611edd8d9`, it reached
`awaiting_internal_review` in 125 s with every pre-PR gate but the internal review
passing, and the verifier's own clock read `make lint` 14 s (uv sync from PyPI
included), `make test-unit` 57 s, `make scan` 2 s.
