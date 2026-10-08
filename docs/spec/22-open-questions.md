# 22. Decisions taken and questions still open

Status: the decisions below are the 2026-09-16 set. Later decisions are ADRs under docs/adr/ (0015 onward, through 2026-09-29) and are not folded back into this table; where an ADR and a row here differ, the ADR wins.

## Decided by the operator, 2026-09-16 (incorporated in v0.3)

| # | Topic | Decision | Where |
|---|---|---|---|
| 1 | Worker images | Build locally in early phases; publish versioned, digest-pinned images to `ghcr.io/sentania-labs/` once the live harness phase and release workflow exist | 13, 20 |
| 2 | GitHub authentication | GitHub App with Metadata read, Contents rw, Pull requests rw, Checks and Actions read; short-lived repository-scoped installation tokens minted on demand; key and tokens never stored; workers hold no GitHub credential; Crucible owns all routine GitHub mutations | 12, 23, ADR 0007 |
| 3 | Internal review | Required, non-author, before any PR; Foundry decides when and what; Crucible runs it as a `review` execution when asked, Foundry uploads the report otherwise | 09, 11 |
| 4 | External review | Configurable per repository policy; default one Codex round, no retrigger after correction, not required on the final SHA, disposition required; allowlisted reviewer logins only | 05b, 23, ADR 0008 |
| 5 | PR and CI observation | Crucible watches the PR by webhook plus polling; Foundry keeps no loop; feedback never reaches a worker without a Foundry decision | 23 |
| 6 | CI certification | Pre-PR verification is the proof, PR CI the certification; a required failure is an escalation with evidence and no automatic retry or correction; distinct states added | 09, 23, ADR 0009 |
| 7 | Release | Foundry proposes, operator authorizes, Crucible verifies gates and tags; domain designed now, implemented after readiness | 24, ADR 0010 |
| 8 | Timezone | Stored UTC with offset; rendered in the operator's private configuration; repository default UTC | 01, examples |
| 9 | Concurrency | 3 per provider, 1 per subscription harness while auth state is shared | 05b |
| 10 | Retention | Logs and transcripts 90 days, bootstrap archive 180, completed workspaces 14, credential volumes immediate, everything else indefinite; cleanup as events | 05b, 16 |
| 11 | Host-process provider | Designed, not implemented unless a spike proves a harness cannot run in a container; weaker isolation requiring explicit policy authorization | 08 |
| 12 | Crucible delivery | Branches and PRs, tagged releases, tag-triggered SDLC pipeline, GitHub-hosted runners | 20 |
| 13 | Docker authority | Rootless daemon dedicated to Crucible preferred, spike early; host socket as documented temporary fallback; no container other than Crucible ever sees the socket or proxy | 13, ADR 0004 |
| 14 | Prior-art reuse | No code reuse from Sandcastle, earlier Foundry, Stewart, Vault by default; any helper needs an ADR with source, license, justification, attribution, fit | 02 |
| 15 | Harness credentials | Per-harness mounts, narrow disposable copies, allowlisted sync-back, concurrency 1; a worker may misuse its own credential, mitigations listed, broker later | 12, ADR 0007 |
| 16 | Harness versions | No self-update; version pinned per image; digest recorded per attempt; adapter version ranges with refusal; Renovate weekly PRs, canary, explicit promotion, one retained prior version | 07, 13, ADR 0011 |

| 17 | Harness and model rotation | Selection by capability, cost, and speed with rotation across providers and local models; frontier models never spent on simple work; quality feedback informs selection; routing policy as data, metrics per attempt, selection stays Foundry's | 00, 03, 04, 05, 05b, 07, 14, 21 |

| 18 | External reviewer on App-authored PRs | Repository setting "review all pull requests" makes the reviewer handle App-authored PRs automatically; that setting is a repository-onboarding prerequisite; Issues read added to the App for reaction observation; reaction-only clean results bound to a head by time; no automatic re-review on a new head | 23, 05b, ADR 0007 |

## Interpretations made while incorporating (confirm or correct)

- **I1. Foundry's acceptance sits between pre-PR gates and the push.** The
  operator's flow says Crucible pushes after the pre-PR gates pass. This
  specification inserts Foundry's `AcceptanceResult` for the collected head
  before `publishing` (policy `publish_requires_acceptance: true`), so the
  outward-facing push under the App identity always follows a recorded
  judgment. It costs one API call per head. Set the flag false to push on
  gates alone.
- **I2 (revised by the operator).** A correction reruns every required
  test, scan, verification command, and mechanical pre-PR gate. It does
  not automatically require another internal review; Foundry requests one
  through the correction contract when the correction is substantial,
  expands scope, or creates architectural risk.
- **I3. A PR closed without merge rejects the task**; cancelling a task
  never closes its PR. Both are recorded, neither is reversible by Crucible.
- **I4. Merge is observed, never performed.** Crucible has no merge
  endpoint, matching "I review and merge".
- **I5. CI certification counts observed runs only**: every non-skipped
  check run and workflow job on the accepted SHA counts. The default-empty
  `ci_certification.required_checks` list only explicitly narrows those runs
  by name when set. Branch protection and ruleset names do not participate.
  All must succeed; any failure fails; running jobs or no runs are pending,
  never a pass, unless the policy explicitly allows a repository without CI.
- **I6. Branch-only deliverables publish before acceptance.** `branch`
  deliverables pass through `publishing` and `branch_pushed_at_head` and
  only then become `accepted`; the kind itself needs a policy allowance.
- **I7. An out-of-band PR head blocks.** The task moves to
  `head_diverged`, the previous head's acceptance and gates are
  superseded, and Foundry chooses recollect or reject. Green CI on the new
  SHA is never sufficient.

## Resolved after the external review of PR #1 (operator, 2026-09-16)

- **Q13 Webhook route.** Polling is the complete initial observation path
  and covers reviews, review comments, issue comments, reactions, PR state,
  checks, workflows, and head changes. No public route on the workstation.
  Webhooks are added when Crucible sits behind Kubernetes ingress.
- **Q14 Release authorization.** Foundry records the operator's verbatim
  approval and identity as the durable authorization the release contract
  references (`authorization_recorder: orchestrator_relay`). Direct
  operator-token authorization stays available as a stricter policy.
- **Q15 Reviewer login.** Confirmed from PR #1: `chatgpt-codex-connector[bot]`
  submits the review; the summary comment posts as `chatgpt-codex-connector`.
  The reviewer reacts with a thumbs-up when a review finishes with no
  findings, so `reaction:+1` is an accepted signal.
- **Q16 `source.migrated`.** Included in the bootstrap export as
  informational metadata; verified and recorded, never used to decide
  authority.

## External review of PR #1: findings and dispositions

One Codex round on commit `5f3e516`, seven inline findings, all
dispositioned `fix` by the operator's direction and corrected in this
revision: branch-only publication (09), out-of-band head invalidation (09,
23), no vacuous CI pass (11, 23), configured round counts (09, 11),
reaction observation (23), tag bound to the authorized version (24),
webhook payloads normalized and scanned before storage (04, 14, 23). No
second review round requested.

## Still open (operator judgment required)

None at this revision.

## New conflicts introduced by the decisions

- **Codex review and CI on `crucible/*` branches.** The external reviewer
  and required checks must be configured on each target repository to run
  on PRs from `crucible/*` branches; that is repository setup, not
  Crucible code, and it is a readiness prerequisite per repository.
- **Egress allowlist still contains `github.com`.** Workers keep read
  access for dependencies and context. Without a credential they cannot
  push, but a worker could still read public repositories the contract did
  not name. Accepted for now; tighten per policy if needed.
- **App permissions versus re-run.** Re-requesting a failed workflow run
  needs Actions write. Resolved by the operator (hades #435, 2026-10-05):
  the App manifest asks for Actions write, and a `ci-decision` `rerun`
  re-runs the failed jobs itself when the installation grants it. An
  installation that still holds Actions read gets the operator hand-off
  as before (23).
