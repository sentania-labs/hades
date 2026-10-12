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

## Amendment: one author on main (FDY-0630, 2026-10-11)

Hades requests each squash with an explicit `commit_title` and `commit_message`: the
title and body it published, as the pull request row stores them (`title` and
`body_sha256`). The merge tick's live read of the pull request, already made for the
head and base checks, carries the body; the tick compares the live title with the
stored one and the live body's SHA-256 with the stored hash, and refuses the merge
with a wake (`pull request title and body changed since Hades published it`) when
either differs, including a row with no recorded hash. A collaborator's edit after
publication therefore never reaches main, neither a changed completion record nor an
issue-closing keyword. The adapter never reads the pull request for the squash text;
it drops any case-insensitive `Co-Authored-By:` line from the message it is given as
a backstop, and an empty body is sent as an explicit empty message. GitHub must not
generate the squash body from branch authors or their commit messages. The App
installation remains the caller and the Hades App is main's sole author. The expected
head SHA and merge-result verification remain in force.

The worker `commit-msg` hook removes those lines before adding `Crucible-Attempt`.
It preserves other trailers and remains idempotent on amend. This is a backstop
for harness settings, not a new publication gate: `--no-verify` can bypass a Git
hook, and the explicit squash message still protects main. No accepted bundle,
correction resume point, or existing commit is rewritten.

Harness audit against the pinned adapters:

- Claude Code 2.1.280: the owned `settings.json` sets `attribution.commit` and
  `attribution.pr` to empty strings. These are the documented replacement for
  deprecated `includeCoAuthoredBy`; see the [Claude Code attribution reference](https://code.claude.com/docs/en/settings-reference#attribution).
  Use the object form, compatible with this pinned version, rather than the newer
  boolean form. The hook covers a harness that ignores the setting.
- Codex 0.156.0: the adapter's owned template injects no attribution. This release
  resolves attribution through the backend's `commit_attribution_enabled`, not a
  local `commit_attribution` setting; see its [policy implementation](https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/ext/git-attribution/src/policy.rs).
  There is no equivalent local setting to disable in this adapter. The common
  hook strips any backend-enabled co-author line.
- Qwen Code 0.25.0: `general.gitCoAuthor.commit` and `.pr` default to true; both
  are set false through `QWEN_CODE_SYSTEM_SETTINGS_PATH`, documented in the
  [Qwen configuration reference](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/settings/).
  The adapter creates the settings file in the worker's temporary directory
  before executing the existing wrapper. This works with or without a gateway
  credential and leaves the image-owned user settings intact. The disposable
  worker removes the temporary file at teardown.
- Hermes 0.19.0: no co-author default or equivalent setting was found in the
  adapter or the installed terminal/configuration code. Its launch ignores user
  configuration and rules and disables plugins through safe mode. The hook is
  the backstop for model-written trailers.
- AGY 1.2.8: no co-author default or equivalent setting was found in the adapter
  or the installed CLI's attribution strings. It has no owned settings template
  adding an author. The hook is the backstop for model-written trailers.

`attempt_routed` still records the selected harness and model. The existing
`Crucible-Attempt` trailer links the worker commit to Hades' task history; it does
not encode the model itself. Publication and CONTRIBUTING document this split
so removing GitHub co-authors does not remove the execution record.
