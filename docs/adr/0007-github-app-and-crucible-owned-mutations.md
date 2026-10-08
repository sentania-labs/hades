# ADR 0007: GitHub App authentication; Crucible owns all routine GitHub mutations; workers hold no GitHub credential

Status: accepted, operator decisions 2 and 15, 2026-09-16.

## Context

Someone must push branches, open PRs, and tag releases. Giving workers a
repository credential makes every model-driven process an outward-facing
actor under the operator's identity and makes the credential's blast radius
the worker's. Personal access tokens are long-lived and user-scoped.

## Decision

- A GitHub App with Metadata read, Contents read/write, Pull requests
  read/write, Checks read, Actions read, and Issues read. Issues read was
  added after S12's rerun showed a clean external review is signaled only
  by reactions on the PR, readable through `GET /issues/{n}/reactions`;
  no Issues write is requested. Crucible mints short-lived,
  repository-scoped installation tokens on demand. The private key is a
  mounted file locally and a projected or external Secret in Kubernetes,
  on Crucible's pods only.
- Neither the key nor any token is stored in PostgreSQL, contracts, logs,
  artifacts, reports, images, or Git. Tokens live in memory and in the
  tmpfs of a publisher container for one job.
- Crucible performs: checkout preparation, pushing the work branch after
  pre-PR gates and acceptance, creating and updating the PR, rendering the
  body from contract and verified evidence, adding only authorized closing
  references, observing review and CI, tagging releases. Every such action
  is a durable event.
- Observation of the PR is by polling, complete on its own (reviews,
  comments, reactions, PR state, checks, workflows, head changes);
  webhooks are an optional accelerator whose payloads are verified in
  memory and normalized and scanned before anything is stored.
- Workers edit, run checks, commit locally, and produce claims. Their
  checkout has no push credential. Any mode granting a worker GitHub access
  is an explicit policy exception with its own ADR.
- Workers do receive their own harness credential and can misuse it within
  the egress allowlist. Mitigations: one credential set per worker, narrow
  disposable copies, allowlisted sync-back, strict egress, redaction and
  scanning, no cross-harness access, concurrency of one. A credential
  broker is a later hardening, not an initial requirement.

## Consequences

Publication needs a bundle-based publisher container so Crucible never
runs git over a worker's `.git` directory with a credential present. The App must be installed on every target repository, the external
reviewer must be set to review all pull requests there (so App-authored
PRs are reviewed, S12 rerun), and CI must run on `crucible/*` PRs. Re-running
a failed workflow needs Actions write, which is not granted; the operator
re-runs by hand until they choose to widen the permission.

## Amendment: Actions write for re-runs (hades #435, 2026-10-05)

The operator widened the permission set by one: the App manifest asks for Actions write,
so a `ci-decision` `rerun` re-runs the failed jobs through the App
(`POST /actions/runs/{id}/rerun-failed-jobs`) and records the attempt it started. Hades
reads the installation's granted permissions on every rerun decision; an installation
that still holds Actions read keeps the `ci_rerun_needed` hand-off to the operator (23).
No other permission changes.
