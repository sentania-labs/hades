# Hades roadmap

This is the working plan that turns `docs/vision.md` into milestones with exit tests.
The vision is the operator's directive and wins where the two disagree; this document
is the plan Foundry is executing against it, and it changes as decisions land.
Last revised 2026-10-01 (reconciliation against main d0e4bcd and the v0.7.0 lab; milestone order unchanged).

## Where we are

Hades has a substantial execution service: versioned task contracts, PostgreSQL
state and evidence, isolated workers, mechanical gates, GitHub delivery, Docker
and Kubernetes providers, and an administrative UI/API/CLI. Claude Code, Codex,
AGY and Hermes have adapters; harness images are now independently promoted
(ADR 0018), not one mandatory combined image.

This is a dated source/backlog snapshot at
[`d0e4bcd`](https://github.com/sentania-labs/hades/commit/d0e4bcd8) (2026-10-01).
The lab runs v0.7.0; v0.7.1 was tagged 2026-10-01 and is not yet deployed. Merged
code, a published release and verification on a deployed instance are different
evidence, and this document says which one it has for each claim.

The original lab blockers #91, #93, #95, #92, #94 and #89 are closed. The Hermes
worker-path proof is recorded in #146. Neither fact alone establishes M1c.
#173, #203, #204 and the toolchain half of #184 are closed (PRs #224 and #223,
2026-09-28 to 2026-09-30). The remaining M1 gaps are #85 (test-service sidecars and
branch CI as a pre-PR gate) and #183 (truthful reports when required tools are
missing). Codex credential refresh (#42) is implemented by PR #327 and ships in
v0.7.1. In practice Codex has carried most of the self-development work (36 of the
38 Hades-authored PRs to 2026-10-01, 2 on Hermes, none on Claude Code); the M1c
Claude Code path is unproven and is recorded as such below.

The real Foundry ledger handoff happened on 2026-09-29 on the operator's words
(ADR 0029, #255, PR #245): the bootstrap bundle was imported, the SQLite ledger was
frozen read-only, and Hades has been the system of record for Foundry's tasks since.
[Readiness](readiness.md) row 18 records it; the import id lives in Foundry's private
state, not in this repository.

Persistent principal chat, independent work cards, curated agent definitions and
durable agent routines remain product work, not capabilities implied by today's
worker adapters or admin UI. The authentication `PrincipalRow` is not a
conversational principal, and the supervisor tick is not the M5 routine scheduler.

## Tracking and reconciliation

[#207](https://github.com/sentania-labs/hades/issues/207) was Foundry's reconciliation
task; it closed with PR #213 on 2026-09-28 and its checklist was never ticked. The
2026-10-01 reconciliation lives in the issue bodies it touched (#208, #275, #278,
#288, #289, #310, #319, #332, #337, #338) and in this document. New issues and scope comments are for triage;
they do not authorize implementation, change milestone order, or supersede the
operator's vision.

| Capability | Current position | Owning tracking |
|---|---|---|
| Bootstrap authority and self-development proof | Handoff done 2026-09-29 (ADR 0029, #255); M1 exits still need #85 and a Claude Code run | [#207](https://github.com/sentania-labs/hades/issues/207), [#85](https://github.com/sentania-labs/hades/issues/85), [#184](https://github.com/sentania-labs/hades/issues/184) |
| M2 persistent principal and harness continuity | Planned; dedicated conversation service and continuity spike not implemented | [#208](https://github.com/sentania-labs/hades/issues/208) |
| M3 curated identities and skills | Partial backlog coverage; applicability, permissions and agent tests need explicit acceptance | [#199](https://github.com/sentania-labs/hades/issues/199), [#197](https://github.com/sentania-labs/hades/issues/197), [#198](https://github.com/sentania-labs/hades/issues/198) |
| M4 brainstorm intake and independent cards | Planned; captured ideas must remain distinct from authorized execution | [#209](https://github.com/sentania-labs/hades/issues/209) |
| M4/M5 human attention and daily briefing | Planned; distinct from execution telemetry | [#210](https://github.com/sentania-labs/hades/issues/210) |
| M5 durable routines | Partial backlog coverage; persisted scheduling, restart and approval semantics need acceptance | [#199](https://github.com/sentania-labs/hades/issues/199) |
| Noncoding research and content work | Current task contract still requires a repository, including artifact deliverables; place by first real use after bootstrap | [#211](https://github.com/sentania-labs/hades/issues/211) |
| M6 optional tools and transports | Planned; select a concrete flow before implementation | [#212](https://github.com/sentania-labs/hades/issues/212) |
| Execution visibility and independent review | Board shipped (#188, PR #306); cross-family review proposed | [#188](https://github.com/sentania-labs/hades/issues/188), [#201](https://github.com/sentania-labs/hades/issues/201); [#206](https://github.com/sentania-labs/hades/issues/206) remains optional ideas |
| Browser and machine authentication | Token-based administration exists; server-side UI sessions shipped (#132, PR #270); OIDC tracked | [#180](https://github.com/sentania-labs/hades/issues/180) |

Foundry validates each finding as satisfied, partially covered, untracked,
deliberately deferred, or needing an operator decision. Accepted scope belongs in
the owning issue body, not only in superseding comments. README and CONTRIBUTING
still contain pre-implementation/C7a-era descriptions; their reconciliation and
readiness evidence updates belong to #207. This status update does not invent
deployment or test results.

## Standing decisions

All of these are the operator's decisions.

| Decision | Date |
|---|---|
| **Hades** is the product and repository name (vision). | 2026-09-23 |
| The "later decision" the vision leaves open on the subsystem's name is taken: it becomes **`execution`**. Its internal identifiers (namespaces, Secrets, `CRUCIBLE_*` settings, images, database, CLI) are renamed after M1, as one planned migration run through Hades, with an alias period and a coordinated redeploy. New components never embed the name `crucible`. | 2026-09-23 |
| Harness login is driven from the UI on every provider, which answers the vision's open question on #92: it is required now. Harness credentials are not hand-sealed as a substitute. | 2026-09-23 |
| M1 proves two paths: Claude Code (subscription) and Hermes to the local model gateway. AGY follows once the Kubernetes login exists. Codex follows #42. | 2026-09-23 |
| Workers never get a Docker socket. Heavy test tiers run in branch CI (#85). | 2026-09-22 |

## Bootstrap

### M0a: rollback point

**Status:** the historical v0.5.3 rollback release and #96 are complete; retain
the original exit criteria as the record of what had to be established.

- #96: service `latest` is copied on the registry from the version tag, never pushed
  from a fresh local build, the rule main already applies to the worker images since
  #90 (after v0.5.2).
- This roadmap and the vision are committed.
- v0.5.3 is tagged as the last Crucible-era release (the operator's go).

**Exit:** the release run is green, both images pull by the digests the release
names, and every `latest` tag resolves on the registry to the same digest as its
version tag.

### M0b: an honest, usable lab deployment

**Status:** done and proven on the lab. The original implementation issues and
#173 are closed (PR #224). The lab running v0.7.0 has published 38 pull requests as
the Hades GitHub App, and the UI login from an empty Secret is in the kind CI tier
(`tests/e2e/test_kind.py::test_login_from_an_empty_secret_to_a_probe_and_an_attempt_through_the_admin_api`).

1. #91 with #58: egress allows that work under Cilium with kube-proxy replacement
   (selector or CNI-aware rules, not `ipBlock` on service addresses), and a canary
   that proves DNS resolution and model-endpoint reachability rather than only
   API-server denial. The login Job's allows are narrowed to login endpoints.
2. In parallel: #93 (requests below limits, configurable, with a render-time total of
   requested resources), #95 (the PID gate reads the pod's real limit, or the provider
   sets one), #94 (`fsGroupChangePolicy: OnRootMismatch`), #89 (an empty Hermes Secret
   is no credential).
3. #92: the Kubernetes login Job, driven from `/ui`, and Hermes gateway key entry from
   the UI on Kubernetes. This changes a documented design: spec 26 and
   `docs/deployment.md` originally delivered harness Secrets through the GitOps repository.
   The credential-ownership ADR and amendments to specs 12, 25 and 26 make the service the single
   owner of the harness credential Secrets and takes them out of GitOps, because a
   Secret written by both the service (login, token sync-back) and GitOps drifts. The
   database and deployment TLS remain deployment-owned. The GitHub App Secret is
   now also service-owned under ADR 0017; it must not have a competing GitOps writer.
4. `docs/deployment.md` matches what ships.

**Exit (seen working):** on a disposable kind cluster running Cilium with kube-proxy
replacement and no harness credential Secret provided up front, Claude Code is logged
in and the Hermes key entered from the browser, then one real attempt on Claude Code
and one on Hermes each reach a terminal state with evidence Foundry has read. The
release carrying it is published. Deploying it to the lab is the deployer's, and what
a cluster must provide (kubelet PID limit, gateway selectors) is stated in
`docs/deployment.md`.

### M0c: authority handoff

**Status:** done. The handoff ran on 2026-09-29 on the operator's words ("discard
and import fresh", ADR 0029, #255, PR #245); Hades has been the system of record
for Foundry's tasks since then. The authorization requirement below stays as the
rule for any future import.

Register the repositories (spec 25), then import, verify and commit the Foundry
bootstrap ledger on the instance the deployer runs (spec 15). Commit and mark-migrated are irreversible
and wait for the operator's verbatim go. Foundry's start-of-session then reads tasks
and wakes from Hades only.

**Exit:** the bootstrap ledger is frozen and Hades is the system of record.

### M0d: a truthful product

- README and operator documentation describe Hades, with `execution` as the subsystem.
- Open issues are triaged as the vision sets out: bootstrap blocker, execution
  hardening, later. #85 with #184 is the known M1 testing dependency; classify newer
  operational findings by whether they actually prevent the M1 acceptance path.
- The repository has been renamed to Hades. GitHub redirects the old
  URL and image names are unchanged, but registrations store the repository URL and
  the push remote is built from `owner/name` (specs 03 and 23). So the rename lands
  before M0c registers repositories, or M0c re-registers after it.

**Exit:** the README a newcomer reads describes what is deployed today, every open
issue carries one triage label, and a clone, a push and a registered-repository
token check work under the repository's final name.

### M1a: Hades can test itself from a pod (#85)

**Status:** open. The toolchain half of #184 is in (ADR 0020, FDY-0131,
2026-09-28): the worker image carries uv, CPython 3.12 and gitleaks, and tasks against
this repository run under `hades-self-hosting`, whose required checks are `make lint`,
`make test-unit` and `make scan`, proven on kind with the real worker image and
NetworkPolicy (`make e2e-kind-self-hosting`). The integration and e2e tiers still need
Postgres and Docker; until the sidecars below land, branch CI is their only proof.

- Declared test services (PostgreSQL first) run as per-attempt sidecars, reachable on
  localhost only. The suite uses the service when it is present and falls back to
  testcontainers when it is not, so one suite runs on a laptop, in CI and in a pod.
- The supervisor's publisher, never the worker, pushes the candidate head. Workers
  keep no GitHub credential. Branch CI already runs every job on every push. What is new is that the service
  observes it: branch CI green becomes a required gate before review and the pull
  request, in the slot local tiers hold today. The heavy tiers stay on GitHub-hosted
  runners, as `ci.yml` places them for a public repository; moving any job to the
  self-hosted pool is a separate decision. Evidence must name the exact candidate
  SHA; corrections require fresh head-specific verification. Required CI failures
  escalate with evidence, not automatic correction/retry loops.
- CI uploads a diagnostics bundle on failure only.

**Exit:** a worker on the lab cluster runs the unit and integration tiers against its
PostgreSQL sidecar, and an attempt whose branch CI is red cannot reach review.

### M1b: a deterministic model for tests

**Status:** a deterministic model server already exists in
`tests/e2e/stub_model.py`, and `tests/e2e/test_kind_self_hosting.py` drives a Hermes
task through the `fast` alias with it. Two gaps remain: that test runs only through
`make e2e-kind-self-hosting` and is in no CI shard, and there is no assertion that
repeated runs are identical.

A scripted OpenAI-compatible stub behind a gateway model alias, so tests of the Hermes
path do not depend on a live model's output.

**Exit:** a Hermes-path integration test passes identically on repeated runs with no
live model reachable.

### M1c: acceptance

The operator hands Foundry a bounded change to Hades. Foundry scopes it and submits a
contract; Hades launches the worker on the lab cluster; evidence and gates are
captured; branch CI gates it; Foundry reviews before the pull request opens; the
external review round runs; Foundry accepts, corrects or escalates. Once on Claude
Code, once on Hermes.

**Exit:** the operator can truthfully say "Foundry uses Hades to develop Hades."

## Self-sustaining

From here, Hades work is dispatched only through Hades.

### M1.5: burn-in

- The `execution` rename.
- The hardening backlog as the burn-in queue. The original examples #40, #41, #55,
  #59, #60, #63, #65, #66 and #76 are now closed; #39 remains open. Select live,
  bounded work during triage rather than reopen completed work to fill the queue.
- AGY on Kubernetes through the UI login.

**Exit:** the rename and at least five further changes are merged through Hades,
with no fix to Hades made outside it in that period.

## Product milestones

Order after burn-in follows real use.

### M2: persistent principal agent

Tracking: [#208](https://github.com/sentania-labs/hades/issues/208). The first usable
path can start with one harness; the continuity spike establishes the limits of
additional harnesses. Operator-directed switching preserves recorded work and
approvals, not hidden model state, and must not replay completed actions.

- A dedicated service owns every conversation. Hades' own append-only log is the
  source of truth; a harness session is a cache that can be rebuilt from it.
- Reasoning runs in the official harness binaries in their long-lived structured
  modes (Claude Code with stream-JSON input and output, `codex app-server` over
  stdio), signed in with the operator's own subscription and a login dedicated to the
  principal so its token refreshes never collide with workers. Hermes serves cheap
  turns.
- One active turn per conversation; reconnect resumes the same conversation from any
  client.
- The principal's authority is the Hades API plus read-only context, not a shell.
  Anything that changes code is an `execution` task.
- Approvals become first-class records: pending, approved, denied, expired; resolved
  once, atomically; kept in a permanent decision log. "Expired" (nobody answered) is
  never shown as "denied" (someone decided).
- First step is a spike: one conversation through each harness, the pod killed mid
  turn, the session volume wiped, and a worker running concurrently on the same
  subscription.
  The room runner part ran on 2026-10-08 ([docs/spikes/room-runner-sdk.md](spikes/room-runner-sdk.md)):
  a room is a Claude Agent SDK client over the same stream-json process, scheduled runs
  stay on the per-turn CLI, and the two resume each other's sessions.

**Exit:** close the laptop, return from another client, continue the same
conversation, and delegate through Hades.

### M3: agent identity and curation

Tracking: [#199](https://github.com/sentania-labs/hades/issues/199),
[#197](https://github.com/sentania-labs/hades/issues/197), and
[#198](https://github.com/sentania-labs/hades/issues/198).

- An agent card: identity, responsibility, tools, skills, routing preference, sample
  requests, and an enabled flag kept separate from observed health.
- Skills state when to use them and when not to, name the sibling skill for the
  adjacent case, and declare the tools they may use. They are injected at dispatch,
  never committed to target repositories.
- "Test the agent": assertions on the tools an agent called and their order against
  mocked tools, routing cases (should route here, near miss, should route nowhere),
  and an optional model-judged pass where a judge outage is an error, not a skip.

**Exit:** the operator creates an agent in the UI, attaches a skill and a tool, runs
its tests there, and enables it.

### M4: work and focus

Tracking: [#209](https://github.com/sentania-labs/hades/issues/209) for intake/cards
and [#210](https://github.com/sentania-labs/hades/issues/210) for human attention.
Capturing an idea is not authorization to execute it. The worker dashboard in
[#188](https://github.com/sentania-labs/hades/issues/188) is related but distinct.

Lightweight cards that reference GitHub and other sources rather than copying them.
The first view answers "what needs me?" from pending approvals, agents waiting, and
real commitments, with lab and pet-project work visibly separate.

**Exit:** the operator uses that view, not GitHub or chat scrollback, to find what is
waiting on them.

### M5: routines and the daily view

Tracking: [#199](https://github.com/sentania-labs/hades/issues/199) for scheduling
and [#210](https://github.com/sentania-labs/hades/issues/210) for the daily view.

A durable scheduler with leases, a stored next run and an explicit missed-run policy.
Each run gets its own conversation. Each routine declares its pre-approved tools; a
run that needs a new approval parks in "what needs me".

**Exit:** a routine survives a service restart without a lost or doubled run, and the
daily view is the one the operator actually opens.

### M6: tools and transports

Tracking: [#212](https://github.com/sentania-labs/hades/issues/212). Coordinate
[#211](https://github.com/sentania-labs/hades/issues/211) when the selected workflow
needs repository-independent research or content artifacts. Neither service becomes
a mandatory Hades dependency.

Chronicle, Coppermind, GitHub and Discord, each when a real flow needs it. Inbound
triggers carry a per-trigger token stored hashed, a payload schema, and a delivery
log, behind the durable queue.

**Exit:** per tool, the real flow that justified it runs end to end.

## Explicitly deferred

As `docs/vision.md` lists: org charts, multi-company support, a Paperclip clone, a
universal knowledge store, vector search, replacing Vault or n8n ingestion, absorbing
Chronicle or Coppermind, a Discord bot before persistent chat works, a perfect Kanban,
and clearing all technical debt. The vision also defers renaming every `crucible`
identifier; that stays true through M1, and the rename is now scheduled for M1.5.
