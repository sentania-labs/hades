# 05b. Policy schema (PolicyV1)

A policy is a named, versioned document uploaded by an admin principal and
referenced from every task contract. Versions are immutable once referenced.
Every tunable the specification mentions lives here, so nothing is a magic
default in code. The values below are the operator's initial defaults
(decisions of 2026-09-16).

```yaml
schema_version: "1.0"
name: "default-software"
version: 2                             # version 1 is the same document naming routing version 1
description: "Software repositories consumed by something else: branch, PR, merge, tag."   # the seeded row's own description names the version and what changed in it

limits:
  timeout_seconds: { min: 300, max: 14400, default: 3600 }
  command_timeout_ms: { min: 1000, max: 14400000, default: 3600000 }   # per shell command, see below
  max_attempts: { max: 3, default: 2 }
  grace_seconds: 60                    # drain before kill
  stall_warn_seconds: 300
  stall_fail_seconds: 1800
  auth_retry_delay_seconds: 600
  escalation_stale_hours: 24
  wake_retry_hours: 24

retry:
  eligible_classes: ["environment", "lost", "auth_failure"]
  auth_failure_max: 1

concurrency:
  per_provider: 3
  per_harness: { claude_code: 1, codex: 1, agy: 1 }   # shipped defaults; all three support higher caps

resources:
  cpus: 2
  memory: "4GiB"
  pids: 512
  tmpfs_total: "20GiB"

network:
  mode: "egress-proxy"                 # egress-proxy | none
  egress_allowlist:                    # hostnames the egress proxy (13) or the worker's NetworkPolicy (26) permits for workers, as written (hades #425)
    - "github.com"                     # read-only in effect: workers hold no GitHub credential
    - "objects.githubusercontent.com"
    - "pypi.org"
    - "files.pythonhosted.org"
    - "registry.npmjs.org"
  harness_endpoints: "from-harness"    # each adapter contributes its model endpoints (S6)

routing:                               # see RoutingPolicyV1 below; this names which one applies
  policy: { name: "default-routing", version: 2 }   # default-software v2 names routing v2; v1 named routing v1

images:
  allowlist: ["crucible-worker:*", "ghcr.io/sentania-labs/crucible-worker:*"]
  require_default_or_retained: true    # candidates only with an explicit per-task override

git:
  author_name: "crucible-worker"
  author_email: "crucible-worker@users.noreply.github.com"
  commit_trailer: "Crucible-Attempt"   # trailer key; Crucible's commit hook adds `<key>: <external_id>` as a courtesy nothing checks (06)
  work_branch_pattern: "crucible/*"
  protected_branches: ["main", "release/*"]

repository:
  required_checks:                     # commands every contract's required_verification must include
    - "make lint"
    - "make test"
    - "make scan"                      # vulnerability, dependency, and secret scanning as the repo defines it
  required_programs: []               # optional (hades #184): programs those checks call beyond their first word;
                                       # never read at run time, `make images-policy-check` proves each is in the worker image

gates:
  pre_pr:                              # evaluated on the collected head before anything is pushed
    - report_present
    - exit_clean
    - commits_present
    - scope_contained
    - no_injected_files
    - no_secrets
    - verification_ran
    - run_evidence_present
    - criteria_mapped
    - dependencies_unchanged
    - ci_unchanged
    - workspace_clean
    - internal_review_recorded
  publication:                         # evaluated after Crucible pushes and opens the PR
    - branch_pushed_at_head
    - pr_exists_head_matches
  post_pr:                             # evaluated on the PR head as GitHub state arrives
    - external_review_rounds
    - feedback_dispositions_complete
    - ci_green_for_head
  skipped: []                          # release gates live on the release contract (24)
                                       # commit_policy is never listed: it always runs, always advisory (11)
  advisory:                            # ADR 0024: a failure of these goes to the internal reviewer instead of stopping the task
    - criteria_mapped                  # absent (every version written before 2026-09-29): this default set
    - report_present
    - run_evidence_present
    - scope_contained                  # a path matching prohibited_paths still stops the task

deliverables:
  allow_branch_only: false             # `branch` deliverables refused unless true
  on_out_of_band_head: "block"         # block: task to head_diverged and wake (09); the only option in v0.x

pull_request:
  require_pre_pr_verification: true
  open_only_after_pre_pr_gates_pass: true
  publish_requires_acceptance: true    # Foundry's AcceptanceResult precedes the push (09)
  title_from: "claim"                  # the worker's proposed title, validated by Crucible
  body_template: "default"             # Crucible renders the body from contract and verified evidence
  closing_refs: "contract_only"        # only deliverables[].closes may appear as closing keywords

internal_review:
  required: true
  required_for_corrections: false      # a correction contract may still request one
  reviewer_must_not_be_author: true
  executor: "orchestrator_or_crucible" # orchestrator uploads a ReviewReportV1, or requests a Crucible review execution

external_review:
  provider: "codex"
  request_on_publish: true             # post the provider trigger after opening the PR
  trigger_comment: "@codex review"     # provider-specific; Codex is the shipped mapping
  reviewer_logins: ["chatgpt-codex-connector[bot]"]   # allowlisted identities (confirmed on sentania-labs/crucible#1)
  required_rounds: 1
  retrigger_after_correction: false
  require_review_on_final_sha: false
  require_feedback_disposition: true
  accepted_signals: ["reaction:+1", "review"]  # from an allowlisted login only; +1 is the durable "no findings" signal (S12 rerun); "comment" is opt-in
  components: ["code"]                 # review components in one cycle; ["code", "security"] where the repo runs both
  round_counting: "completed_cycles"   # a round is one completed cycle (all components terminal) on a published head
  wait_timeout_hours: 24               # then wake Foundry with reason external_review_overdue

ci_certification:
  require_green_on_final_sha: true
  required_checks: []                  # default empty: count every observed non-skipped run on the SHA; non-empty explicitly narrows by name
  allow_no_ci: false                   # false: zero observed runs is pending forever (wake on timeout); true only for a repository that intentionally has no CI
  on_failure: "escalate"
  automatic_retry: false
  automatic_worker_correction: false
  wait_timeout_hours: 6                # then wake Foundry with reason ci_certification_overdue

release:
  require_operator_approval: true
  authorization_recorder: "orchestrator_relay"   # orchestrator_relay: Foundry records the operator's verbatim approval and identity; operator_token: the operator's own principal must record it
  trigger: "tag"
  tag_pattern: "v{major}.{minor}.{patch}"
  version_files: []                    # paths whose version string must agree with the tag
  changelog_required: true

cleanup:
  workspace_on_success: "keep_diff_only"
  workspace_on_failure: "keep"
  container_remove: "always"           # only after logs_drained
  credential_volume_remove: "immediately_after_validated_sync"

retention:
  logs_and_transcripts_days: 90
  bootstrap_archive_days: 180
  completed_workspaces_days: 14
  wakes_after_ack_days: 30
  indefinite: ["events", "completion_claims", "decisions", "gate_results", "review_reports",
               "external_reviews", "dispositions", "ci_certifications", "release_records",
               "diffs", "artifact_metadata"]
```

## Validation

- Every field present with the listed types; unknown fields rejected.
- `gates.pre_pr`, `publication`, `post_pr`, and `skipped` partition the gate
  set defined in 11 and 23; a gate in none of them is an error.
- `retry.eligible_classes` is a subset of the `ExitClass` enum (07).
- `concurrency.per_harness` may exceed 1 for read-only adapters or adapters declaring
  `parallel_attempts_safe`. Writable adapters without that declaration are refused
  with a reason. Claude Code's long-lived setup token is read-only and never syncs
  back. AGY's writable copies reuse Google's refresh token. Codex does not declare
  parallel safety: OpenAI's refresh-token rotation makes concurrent sync-backs unsafe,
  so `per_harness.codex` above 1 requires renewer mode. Hades alone refreshes the
  login; workers hold access tokens only. Rollback uses `per_harness.codex: 1` and
  `rw-narrow` mode. Its
  `auth_failure` retry-once behavior remains; supervisor warnings carry per-harness
  `auth_failure_count` (reset on restart). Shipped caps are unchanged; Foundry sets
  per-harness caps after deployment. Both uploads and launches use the shared adapter
  declarations (12). Executions keep their policy snapshot.
- The per-harness cap is checked **after** the checkout lease (10), not
  before. The lease is the older and more specific rule: an attempt whose
  checkout another attempt holds should be told that, not held back by a
  cap it never reached. Only a launch that could take the checkout is
  measured against the cap. A launch over the cap waits and records
  `harness_launch_deferred`; it is not a failure.
- An attempt counts against the cap until its credential copy has been
  synced back and removed, which is after `exited` (12), so `terminating`
  and `exited` attempts are still busy.
- `network.egress_allowlist` entries are hostnames, no wildcards in v0.x. The
  list is what the worker and the verifier may reach, on either provider and
  as written; the git roles reach GitHub whether or not it is listed (26).
  Before the harness starts, the launch wrapper probes every listed host and
  the result is recorded on the attempt (`egress_probe`, hades #425).
- `external_review.required_rounds: 0` makes the external review gates
  `skipped`; `reviewer_logins` must be non-empty when rounds are above 0.
- `external_review.accepted_signals` does not carry `comment` by default.
  The provider posts its summary comment within about ten seconds of the
  PR opening, minutes before any verdict, and edits it in place when the
  review lands (S12), so a comment accepted by default completes the cycle
  before there is anything to complete. A repository may add `comment`
  deliberately; even then a comment carrying the provider's summary marker
  is never a round (23).
- `external_review.components` lists the components one cycle expects and
  defaults to `["code"]`. The cycle logic depends on it being present, so
  a repository running code and security review sets both.
- `ci_certification.allow_no_ci: true` and `deliverables.allow_branch_only:
  true` may only be set by an `operator` or `admin` principal and are
  recorded as decisions.
- `release.require_operator_approval` may only be `false` under a policy
  the operator uploaded (principal role `operator` or `admin`), which is
  recorded as a decision.
- `gates.advisory` (ADR 0024) is optional. When present it lists pre-PR
  gates only, each once, and never `internal_review_recorded` (the review is
  the enforcement), `no_secrets` (a pushed secret cannot be taken back) or
  `commit_policy` (the publisher's own rule, never listed in a policy).
  Every pre-PR gate it does not list blocks. Absent, the
  default set applies, so a version written before the field existed
  behaves as the default without being rewritten. Listing a gate outside
  the default set (making a safety gate advisory) may only be done by an
  `operator` or `admin` principal and is recorded as a decision (from the
  local admin CLI, on the upload event). It is edited in place from the admin
  API, CLI and UI (25).

## The per-command timeout

`limits.command_timeout_ms` is the timeout every harness runs one shell command
under, in milliseconds (issue 128; the operator's decision of 2026-09-25: "in the
worker pods we should do this (or it's equivilant) for all harnesess, and the
timeout should be set by the task launch. i.e. configurable at a hades level or
dispatched at run time."). The attempt launches with the contract's
`execution_request.command_timeout_ms` when it sets one, else this default, and
never with more than its own `timeout_seconds`. A version uploaded before the
field existed takes the default bounds shown above (60 minutes). The admin API,
CLI and UI edit it in place (25), which writes a new policy version with only
this limit changed. How each harness is held to it is in 07.

A command in flight counts as activity (issue 152; the operator's decision of
2026-09-27: "a running command counts as activity. While a harness reports a
command in flight, the stall clock pauses, so the stall limit applies to a worker
doing nothing and the command timeout applies to a command running long."). While
the harness's own live output reports a command running, neither
`stall_warn_seconds` nor `stall_fail_seconds` advances (10); the command timeout
bounds each command, and `timeout_seconds` still bounds the whole attempt. A command
the harness still reports a minute past its command timeout no longer counts, since
some harnesses do not end every command at that timeout themselves (a Codex session,
a Hermes background process). When the command ends, both clocks run again from
within a minute of its end (half the shorter stall limit, when that is less), so a
worker that then does nothing stalls as before. What each harness reports while it runs is
in 07. Where it reports nothing, the stall limits count a silent command as they
count any silence: AGY gives no live evidence at all, and a Hermes foreground
command (its usual kind) is not in the registry the evidence comes from, so for
either a silent command longer than `stall_fail_seconds` (1800 seconds in the
seeded policy) ends as a stall before its command timeout.

## Precedence with the task contract

The contract may narrow but never widen: `timeout_seconds` within limits,
`command_timeout_ms` within its limits and never above its own
`timeout_seconds`,
`max_attempts` at or under the cap, `retry_on` a subset of
`eligible_classes`, `network` may select `none` under an `egress-proxy`
policy but not the reverse, `required_verification` a superset of
`repository.required_checks`, `deliverables[].closes` the only closing
references. Review round counts and CI rules come from policy only; a
contract cannot change them.

## Routing policy (RoutingPolicyV1)

Uploaded and versioned like a policy. Foundry names a tier; Crucible
selects the model within it by the rule below, at every attempt launch,
inside the fenced transaction that moves the attempt to `launching`. The
rule is mechanical and total over database state: two supervisors given
the same rows select the same entry. Crucible still refuses an operator pin
whose model is absent or disabled, whose harness does not match the entry,
or whose quota pool is exhausted, and reports per-pool usage and
exhaustion marks on `GET /routing/usage`. (Decided 2026-09-20 on the
operator's direction; before C6b the contract pinned a model and Crucible
only refused.)

**Selection rule.** Candidates are the policy's entries that are enabled,
whose harness is enabled and holds a credential (25), whose capability is
in the tier's `allowed_capability`, whose pool is under its soft limit,
and whose pool carries no live exhaustion mark. Order them by: quality
demotion as `rotation` states (a demoted candidate ranks after every one
that is not, unless its probe is due); then the position of its pool in the
tier's preferred pool order (unlisted last); then the position of its
capability in the tier's `prefer` list (unlisted last); then weighted
least-recent: a candidate never launched on this project ranks first,
otherwise rank by `(now - last_launched_at) * weight`, largest first; then
id, as the final tie break. Recency is read directly from the latest
AttemptMetrics per model on the project, never through a paged task
listing. The first candidate is selected. The launch event records the
selected entry, the derived image, and the ordered candidate list with each
exclusion's reason, whether the candidate's pool is preferred, and its
quality standing.

**Preferred pool order (ADR 0028).** A tier's `prefer_pools` lists pools in
the order routing tries them, ahead of the capability preference. A tier
without the field (every version written before ADR 0028) reads as the
default: the pools holding a `local` model (Hermes on the gateway) for
`trivial` and `standard`, and no pool preference for any other tier, so
`complex` goes to its preferred capability, frontier. An empty list is no
preference. The models outside the preferred pools are the fallbacks: they
are selected when every preferred one is excluded (disabled, no credential,
pool at its soft limit or marked exhausted). When a worker on a `local`
model exits `provider_error` (the gateway refused, was unreachable, or
answered 5xx) and the previous finished attempt on that pool did too, the
pool is marked for its `default_cooldown_seconds`, with that reason; the
mark is listed and cleared like a quota mark. One provider error marks
nothing, and a subscription model's provider error marks nothing. A pool
at its `max_concurrency` is not excluded: the launch waits for a slot. A
review attempt holds a slot of its model's pool like an implement attempt
(hades #359), and the cap that binds is the smaller of the pool's
`max_concurrency` in the routing version the attempt routes with and in the
newest version of the same routing policy that is not retired, so a cap an
operator lowers binds for tasks pinned to an older version. (Decided 2026-09-29 on the operator's direction: "Hermes is not
the anti-route. It should probably be close to our default doer with
frontier being hard structural problems for scoping of items for
hermes/qwen.")

**Quality demotion (ADR 0028).** Per model and project, over its last
`quality_window` attempts, the judged attempts are those that reached the
gates, and a failure is one with a failed blocking gate: the metric
`gates_failed` counts blocking gates only, so an advisory finding never
counts, and corrections do not count. Gate counts are folded into the
metrics once the pre-PR gates are evaluated, so a pass waiting for its
review is judged as early as a failure. The model is demoted when at least
`demote_min_sample` attempts were judged, at least two of them failed, and
failures are at least `demote_failure_percent` of the judged. One failure
never demotes. A demoted model whose last attempt is `probe_after_minutes`
old ranks as if it were not demoted, unless an attempt already routed to it
on the project has not launched yet: one probe at a time, and the probe's
launch makes its last attempt new again. Passing probes bring its rate down
until it is no longer demoted. `quality_feedback: false` turns demotion
off. Bounds: `quality_window` 1 to 1000, `demote_min_sample` 2 to the
window, `demote_failure_percent` 1 to 100, `probe_after_minutes` 1 to 10080.
A version without the three new fields reads them as 50, 5 and 60.

**Exhaustion marks.** A worker exit classified `quota_exhausted` (07, 16)
marks the attempt's pool exhausted until `reset_at`: the reset the harness
reported when the adapter can parse one, otherwise now plus the pool's
`default_cooldown_seconds`. A parsed reset is used as given, even when it
lies beyond any task's wait cap; the cap ends the task, it does not shorten
the pool's fact. Because a mark is shared by every task, it is written only
when the harness's own provider-error event (07) says the provider refused
for quota, never from quota-shaped text elsewhere in a transcript; text
alone may still classify that one attempt `quota_exhausted` and reroute it,
without a mark. A refusal about one model rather than the account (hades
#373; 07, Claude Code) marks no pool: it writes a mark for the model, keyed
`model:<harness>:<model>` in the same table, until the refusal's reset or the pool's
`default_cooldown_seconds`, and selection turns that model away ("model
excluded until ...") while its pool stays open. Marks are rows, survive a
restart, expire on their own, and can be cleared by the administrator with
a reason (25), a model's mark by its `model:<harness>:<model>` key. A
launch-time reservation that finds the pool over its soft limit does not
create a mark; the soft limit is Crucible's own count, the mark is the
provider's word. A harness that is
disabled, by configuration or by the administrator's flag (25), is likewise
a contract problem at submit on `execution_request.harness`, answered 422
with the reason, not a refusal the task discovers later. The operator's
direction (2026-09-16): rotate work across providers by capability, cost,
and speed; never spend frontier models on simple work; local models carry
routine work once they exist.

`default-routing` version 3, seeded by C6b, is the document below: version 2's
roster plus `default_cooldown_seconds` on every pool and the `reroute` block.
Versions 1 and 2 stay beside it because policy versions reference them and a
version is immutable; version 1's ids were placeholders, version 2's are the
ones the CLIs themselves list.

```yaml
schema_version: "1.0"
name: "default-routing"
version: 3
tiers:                                 # task tiers Foundry assigns in the contract's execution_request.tier
  trivial:   { allowed_capability: ["small", "mid"],  prefer: ["small"] }     # frontier is refused, not merely dispreferred
  standard:  { allowed_capability: ["mid", "small"],  prefer: ["mid"] }       # a task that truly needs frontier is marked complex
  complex:   { allowed_capability: ["frontier", "mid"], prefer: ["frontier"] }
  # ADR 0028: a tier may add prefer_pools: [pool, ...]; absent reads as the local pools
  # first for trivial and standard, and no pool preference otherwise.
models:                                # every entry weight 1: rotation is least-recent until the outcomes say otherwise
  - { model: "claude-haiku-4-5",      harness: claude_code, endpoint: subscription, capability: small,    cost: low,    speed: fast,   pool: anthropic-sub, weight: 1, enabled: true }
  - { model: "claude-sonnet-5",       harness: claude_code, endpoint: subscription, capability: mid,      cost: medium, speed: fast,   pool: anthropic-sub, weight: 1, enabled: true }
  - { model: "claude-fable-5-1",      harness: claude_code, endpoint: subscription, capability: frontier, cost: high,   speed: medium, pool: anthropic-sub, weight: 1, enabled: true }
  - { model: "gpt-5.6-luna",          harness: codex,       endpoint: subscription, capability: small,    cost: low,    speed: fast,   pool: openai-sub,    weight: 1, enabled: true }
  - { model: "gpt-5.6-terra",         harness: codex,       endpoint: subscription, capability: mid,      cost: medium, speed: medium, pool: openai-sub,    weight: 1, enabled: true }
  - { model: "gpt-5.6-sol",           harness: codex,       endpoint: subscription, capability: frontier, cost: high,   speed: medium, pool: openai-sub,    weight: 1, enabled: true }
  - { model: "gpt-6-astra",           harness: codex,       endpoint: subscription, capability: frontier, cost: high,   speed: slow,   pool: openai-sub,    weight: 1, enabled: true }
  - { model: "gemini-3.8-flash-low",  harness: agy,         endpoint: subscription, capability: small,    cost: low,    speed: fast,   pool: google-sub,    weight: 1, enabled: true }
  - { model: "gemini-3.8-flash-high", harness: agy,         endpoint: subscription, capability: mid,      cost: medium, speed: medium, pool: google-sub,    weight: 1, enabled: true }
  - { model: "gemini-3.1-pro-high",   harness: agy,         endpoint: subscription, capability: frontier, cost: high,   speed: slow,   pool: google-sub,    weight: 1, enabled: true }
pools:                                 # budget_units is one of attempts | tokens_out | cost_units, all recorded in AttemptMetrics
  anthropic-sub: { window: "5h", budget_units: "tokens_out", soft_limit: 0, default_cooldown_seconds: 18000 }   # 0 means observe only until measured; cooldown is the mark length when the harness reports no reset
  openai-sub:    { window: "5h", budget_units: "tokens_out", soft_limit: 0, default_cooldown_seconds: 18000 }
  google-sub:    { window: "5h", budget_units: "attempts",   soft_limit: 0, default_cooldown_seconds: 3600 }
rotation:
  strategy: "weighted-least-recent"    # among models allowed for the tier, prefer the preferred pools, then the preferred capability, then the least recently used, weighted
  quality_feedback: true               # demote a model whose blocking-gate failure rate on this project is high (ADR 0028)
  quality_window: 20
  # Added by ADR 0028; version 3 predates them and reads these defaults:
  # demote_failure_percent: 50         # failures, as a percentage of judged attempts, that demote
  # demote_min_sample: 5               # judged attempts needed before any demotion (2 or more)
  # probe_after_minutes: 60            # a demoted model is tried again once its last attempt is this old
reroute:                               # C6b: what happens when a worker dies of quota (16)
  reroute_max: 3                       # reroutes per task per contract version, counted apart from lifecycle.max_attempts
  resume_max_wait_seconds: 86400       # longest a task waits in awaiting_quota before it ends reported with a wake
```

Version 3 carries subscription entries only. Version 4 is immutable version 3
plus a `spark-local` pool with `max_concurrency: 4` and one disabled model entry:
`gpt-oss:120b`, harness `hermes`, endpoint `local`, capability `mid`, cost
`none`, and the configured Spark `/v1` URL. Without that configuration the URL
is null and `disabled_reason` records that it is not configured. With the URL,
the entry remains disabled with the reason that the enablement gate has not
passed. Version 5 differs only by enabling that entry and removing the reason,
after the single-task and four-way live gates pass. No Qwen model is in either
version.

Version 6 replaces every Hermes entry from those Spark-specific versions with one
disabled `coder` entry in pool `lab-local`. The endpoint comes from
`CRUCIBLE_LOCAL_ENDPOINT_URL` on first migration, with the older Spark variable accepted
only as a compatibility seed. The entry stores
`chat_template_kwargs.enable_thinking`, false by default, and the pool starts with
`max_concurrency: 4`. Hermes 0.19 has no safe non-interactive flag that can pass this
request option, so C10 retains the operator's choice in immutable routing state. Since
hades #388 the Hermes adapter passes it to the image wrapper, whose bootstrap sends it on
each request as `extra_body.chat_template_kwargs`, and the attempt records it with its
other effective settings. Runtime edits create later immutable versions through
the admin surface. The database value is authoritative over the environment after the
migration.

The `model` field is what routing, pools and evidence call the entry; by default it is
also exactly what the harness is sent. A routing entry is unique on `(harness, model)`,
so Hermes, Qwen Code and Codex may all use `coder`. Local models are references to the
gateway's authenticated `/models` listing at publish time; an unknown reference is
refused with 422 and the checked listing. Subscription models are references known by
their adapter. Which routing version a deployment uses is the policy's own choice.
`default-software` version 2, seeded by
migration 0009 (C5b), is version 1's document naming `default-routing`
version 2, and it is the version the shipped example policy, the example
contract, the compose smoke, the fixtures, and the tiers all reference, so
the verified roster is what a fresh deployment routes with. Version 1 stays
beside it naming `default-routing` version 1, because a policy version is
immutable once referenced. Where a caller names no version, the newest
version of the policy is the one that applies.

`routing.policy.pinned` (a strict boolean, default false) decides which routing
version an attempt routes with (hades #254). Unpinned, every attempt, including a
correction or retry inside an existing execution, routes with the newest version of
the named routing policy at the moment it is routed: the newest version above the
referenced one that is not retired and that some policy references, as publishing
leaves it. A version only uploaded and never published is not chosen, and a retired
reference never falls back to an older version. Pinned, the referenced version is
used. A quota reroute and a resumed quota wait follow the same rule, with the
exhausted pool excluded from the reroute. The task's policy snapshot is never
rewritten. The version used is recorded on the attempt as `routing_version`, and the
attempt's spec, pool reservation, harness count and exit read that version, not a
newer one. A routing version an attempt recorded is immutable: `PUT
/routing/{name}/{version}` refuses to rewrite it with 409, as it does for a version a
policy references. A review is not routed; it records the current version when it
starts preparing and is refused if its model is disabled there or the
entry pairs the model with a harness other than the one the review launches; a
refused review holds no pool slot. A pool is marked exhausted only for an attempt
whose recorded pool and harness match its model's entry in its routing version. The Codex pool in version 2 is exactly the
operator's roster decision: `gpt-5.6-luna` small, `gpt-5.6-terra` mid, `gpt-5.6-sol` and
`gpt-6-astra` frontier. Nothing older and no mini; `gpt-5.5` is a recorded
fallback outside the pool. The ids come from the CLI's own listing, the cost
and speed classes are Foundry's tiering. AGY carries its effort inside the
model id (07), which is why the Flash entries differ only by suffix. A `local`
entry must carry `endpoint_url`; Crucible passes it to the adapter's
launch context and adds its hostname to that attempt's egress allowlist.
A pool's `budget_units` must be a unit AttemptMetrics records; when a
harness reports no token counts, `tokens_out` is recorded as null and the
pool falls back to counting attempts, which `GET /routing/usage` states.
The quota check runs twice: advisory at submit (422 so Foundry can pick
again) and authoritative at attempt launch, where the supervisor reserves
the pool capacity in the same fenced transaction that moves the attempt
to `launching`; if the pool crossed its soft limit since submit, the
attempt is refused with class `quota_exhausted` and Foundry is woken. Foundry
records the tier with its rationale in the contract (05); Crucible records
the selected model and the outcome in AttemptMetrics (03, 14) and exposes
`GET /routing/history?model=&project=`, which returns both the model
Crucible selected and the model the transcript named (14), which is what
the least-recent and quality terms of the selection rule read.
Foundry's judgment is the tier; the policy and the rule do the rest. The
operator alone may pin (05).

`max_concurrency` is a pool-wide count of launching and running attempts. It is
enforced independently of `concurrency.per_harness`; the local Hermes pool uses its
configured pool limit because its credential is read-only, while subscription routes
retain their writable-credential caps. An attempt beyond the local pool limit stays scheduled and records
`harness_launch_deferred` until capacity is released.

### Local Codex preference (FDY-0149)

Local model entries may name `harness: codex`, `endpoint: local`, and `pool: lab-local`.
For trivial and standard tiers, within the same pool Codex ranks before Hermes.
Demotion and probe eligibility still rank first; pool preference still precedes
harness preference, followed by capability and weighted least-recent rotation.
A capacity refusal records both `excluded_harness` and `excluded_model` on the retry
scheduled event. Selection excludes only that pair: a refused Codex route leaves
Hermes and Qwen Code eligible even when they reference the same model. Route resolution
requires the harness for both the submit quota check and the launch reservation, so
each route uses its own pool and endpoint kind.

The Local gateway page has one row per gateway model and places every harness using it
under that row. A newly offered model has an unchecked Hermes control for adding its
first route. Saving these controls preserves each existing route's thinking preference;
the form does not edit that preference. It never presents an internal routing key as
a model. A scheduled
gateway listing disables every `(harness, model)` entry for a vanished model, records
when it vanished, and wakes the orchestrator with the model and stranded harnesses.

### Codex on a local lane is sent a model name it knows (hades #354)

Codex logged `Model metadata for 'fast' not found` on a local lane: Hades sent the lane
name, which Codex's own model catalog does not recognise, so it fell back to generic
tool, prompt, output-token and compaction defaults. A routing entry may now carry
`harness_model_name`, the name its harness is actually launched with, for example
`gpt-5.4`, the gateway alias Codex's catalog does recognise for the same backing
model, while `model` keeps the lane name (`fast`) that routing, pools and evidence
always read. Absent, the harness is sent `model` unchanged (every entry before #354,
and every entry that sets no override, behaves exactly as before). The launch event
(`attempt_launching`) records both: `model`, the lane, and `sent_model_name`, the name
actually passed to the harness.

A local Codex entry's own `context_length` and `max_output_tokens` (the same fields
#448 added for Qwen Code) feed Codex's per-attempt provider config,
`model_context_window` and `model_max_output_tokens`, ahead of the Hermes-administered
defaults on the Local gateway page; an entry that sets neither is unaffected, and Codex
keeps reading the Hermes defaults it reads today. Changing a gateway alias or a
routing entry's thinking setting is lab-admin's call, not this mechanism's; #354 does
not touch either.

`context_length` and `max_output_tokens` are independent overrides, so one entry can
set a value that, against the other figure (its own, or inherited from the Local
gateway page), leaves no input budget once the response reservation is taken out of
the window. The saved Hermes settings already reject that pair at save time
(`hermes_limit_problems`); a local Codex launch validates the same final pair and is
refused rather than sent to fail at request time.
