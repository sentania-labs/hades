# 05. Task contract schema (TaskContractV1)

The worker self-review is the internal review. The required `self_review` section
names where documentation was updated (or why no update was needed), maps every
acceptance criterion with evidence, and lists anything knowingly left out and why.

A missing or incomplete section fails `report_present`, naming `self_review`.
When every blocking gate passes and the report is complete, Hades records acceptance
and publishes without an orchestrator review or acceptance call, for first attempts
and corrections alike. Publication sends one informational `published, PR #N` wake.
An advisory gate failure still requires an orchestrator review before automatic acceptance.
The orchestrator can still cancel or attach a correction after publication. The
review-report endpoint records operator out-of-band adversarial findings against
the PR; a correction can be attached on the operator's word. It is not a gate.

The contract is the only way work enters Crucible. It is authored by the
orchestrator, validated on submit, stored verbatim with its SHA-256, and never
edited in place. Sanitized example: `examples/task-contracts/`.

## Fields

```yaml
schema_version: "1.0"
external_id: "FDY-0042"            # orchestrator's stable ID, unique per orchestrator
title: "Add retry policy to ledger import"
project: "example-service"         # free label for grouping
parent_external_id: null

repository:
  name: "example-service"          # a repository registered with Crucible (04); carries URL and auth
  base_ref: "main"                 # where the worker branches from
  work_branch: "crucible/FDY-0042" # created by Crucible; worker must not rename

scope:
  allowed_paths: ["src/ledger/**", "tests/ledger/**", "docs/ledger.md"]
  prohibited_paths: [".github/**", "**/secrets*"]
  may_add_dependencies: false
  may_modify_ci: false

objective: >
  One paragraph stating what must be true when the task is done.

context:                           # pointers, not prose dumps
  - { kind: "issue", ref: "https://github.com/example-org/example-service/issues/17" }
  - { kind: "doc", ref: "docs/ledger.md" }

project_instructions:              # what the worker must read first
  - { kind: "file", ref: "CONTRIBUTING.md" }
  - { kind: "skill", ref: "sdlc" }

acceptance_criteria:               # each becomes a row the worker must map to
  - id: "AC1"
    text: "Import of a bundle with a duplicate ID fails with a 409 and no partial write."
  - id: "AC2"
    text: "Existing import tests still pass."

required_verification:             # must include every check the repository policy requires
  - { id: "V1", command: "make lint", expect_exit: 0 }
  - { id: "V2", command: "make test", expect_exit: 0 }
  - { id: "V3", command: "make scan", expect_exit: 0 }
  - { id: "V4", kind: "artifact", path: "report/run-evidence.md" }

constraints:
  prohibited_actions:
    - "modify files outside allowed_paths"
    - "create or modify GitHub Actions workflows"
    - "delegate to other agents"
  network: "policy"                # policy | none

deliverables:
  - kind: "pull_request"           # Crucible pushes the branch and opens the PR (23)
    target: "main"
    draft: false
    closes: ["https://github.com/example-org/example-service/issues/17"]   # authorized closing refs; nothing else may be closed

reporting:
  report_schema: "CompletionClaimV1"
  report_dir: "/crucible/report"   # a separate rw mount, never inside the checkout
  progress_events: true

escalation:
  conditions:
    - "acceptance criteria conflict with existing behavior"
    - "a required verification command does not exist in the repository"
    - "any change outside allowed_paths appears necessary"
  action: "write report/blocked.md with the question and exit 75"

policy: { name: "default-software", version: 3 }

execution_request:                 # the class of work; Crucible selects the model within it (05b, C6b)
  tier: "standard"                 # trivial | standard | complex, from the routing policy (05b)
  provider: "docker"
  timeout_seconds: 5400
  command_timeout_ms: 1800000      # optional; per-command timeout within the policy's bounds (05b, issue 128)
  rationale: "mechanical change; standard tier"
  effort: "high"                   # optional; passed through where the selected harness has an effort flag (07)
  pin: null                        # operator pin only: { harness, model, pin_reason }; Foundry never sets it

lifecycle:
  max_attempts: 2                  # must not exceed the policy's cap
  retry_on: ["environment", "lost"]      # subset of the ExitClass enum (07); never on gate failure
  cleanup: "policy"

correction: null                   # present only on a correction version (below)
```

The submitting principal and receipt time are not contract fields: Crucible
records them on the task row and the `task_submitted` event from the
authenticated token, so a client cannot claim another principal.

Deliverable kinds: `pull_request` (the normal case), `branch` (push only,
no PR; requires `deliverables.allow_branch_only: true` in policy), and
`artifacts` (no repository output; used for spikes and reports). `branch`
and `pull_request` both pass through `publishing` (09); only
`pull_request` continues into review and CI certification (23).

## Correction versions

A correction is a new contract version attached through
`POST /tasks/{id}/corrections`. It carries the full contract (so the stored
contract always says what ran) plus:

```yaml
correction:
  of_version: 1
  reason: "external_review"        # external_review | ci_certification | needs_more_work | pre_pr_gates | internal_review
  addresses:                       # what this version responds to
    - { kind: "review_comment", id: "2101", disposition_id: "01J..." }
  instructions: >
    Concrete, bounded instructions for the correcting worker.
  resume_from: "remote_branch"     # the worker starts from the current remote work_branch head
  request_internal_review: false   # true when Foundry judges the correction substantial (09)
```

A correction attached in `ready_for_merge` gives reason `needs_more_work` or
`internal_review` (hades #379): the external round and CI are already done,
so what it answers is Foundry's own judgement of the full diff.

A correction version may narrow `scope` and `objective` and may not widen
them; validation rejects a correction whose `allowed_paths` is not a subset
of the previous version's. `required_verification` may not shrink.

For the single automatic Codex findings correction (23), Crucible derives this section
itself. `addresses` names every inline finding, `instructions` contains each finding's
path, line and body verbatim plus the standing fix-or-decline and report rules, and the
rest of the document is copied from the current task contract. The report must name
each addressed finding exactly once. Dispositions and declined-finding replies are
recorded only after the correction attempt succeeds.

## Validation rules (deterministic, on submit)

- Every field above present; unknown fields rejected.
- `schema_version` major supported.
- `external_id` unique within the submitting principal's namespace.
- `repository.name` is registered (04); `base_ref` exists on the remote at
  validation time; `work_branch` matches the repository policy's branch
  pattern and is not a protected branch; both refs contain only
  `[A-Za-z0-9._/-]` and do not start with `-`, because every ref reaches
  a command line eventually; submit-time validation also applies the
  rules of `git check-ref-format --branch` it can express without git
  (no `..`, no `@{`, no component starting with `.` or ending in `.lock`,
  no trailing `.` or `/`), and the preparer container runs
  `git check-ref-format --branch` as the final authority, failing the
  attempt with class `environment` and the ref named if it refuses.
- `allowed_paths` and `prohibited_paths` are valid globs where `*` stops at
  a path separator and only `**` crosses one (the permissive reading would
  silently widen every contract); the two must not fully overlap.
- Every `acceptance_criteria.id` and `required_verification.id` unique.
- `required_verification` includes every command the repository policy's
  `repository.required_checks` lists (05b); missing ones are a 422 naming
  the check.
- `policy` exists and is not retired; `lifecycle.max_attempts` within it.
- `execution_request` names no model, harness, or image. At submit the
  advisory check is that the routing policy has at least one selectable
  entry for `tier` (enabled, harness enabled with a credential, capability
  allowed, pool under its soft limit and not marked exhausted); otherwise
  422 naming the tier and why each candidate was excluded. Selection itself
  happens at attempt launch (05b) and is recorded on the attempt, so the
  contract stays valid across reroutes.
- `tier` is how the orchestrator asks for a kind of worker (ADR 0028).
  `trivial` and `standard` go to the local pool first (Hermes on the
  gateway), the default doer. `complex` goes to a frontier model: it is the
  tier for hard structural problems and for scoping, such as "scope this
  into Hermes-sized tasks", where the deliverable is a set of smaller
  contracts Foundry then submits as `trivial` or `standard`. No other field
  is needed to ask for frontier work.
- `execution_request.pin`, when present, is the operator's explicit choice
  (bootstrap contract: an explicit model or harness selection from the
  operator takes precedence). It carries `harness`, `model`, and a
  non-empty `pin_reason`, and is validated as the old `model` field was:
  an enabled entry whose harness matches, capability allowed for `tier`,
  pool under its soft limit. A pinned task never reroutes; exhaustion on a
  pinned pool waits for that pool's reset or ends `reported` at the cap
  (16). A `pin` naming a harness without a model is refused.
- `provider` is registered and supports every harness the tier could
  select. The image is derived at launch from the selected harness through
  the image manifest, must match the provider's allowlist pattern, resolve
  to a known `WorkerImage` whose harness version is inside the adapter's
  supported range, and not be `retired`; a candidate whose derived image
  fails any of these is excluded from selection with that reason and the
  next candidate is considered; a tier whose every candidate has no usable
  image is a 422 at submit. The `fake` execution provider alone may accept
  a supplied `image`, as a test affordance; a real provider refuses one.
- `deliverables[].closes` entries are issues in the same repository.
- No credential reference or value anywhere: the contract has no auth
  fields by design; repository auth is Crucible configuration.
- `command_timeout_ms`, when set, within the policy's
  `limits.command_timeout_ms` bounds and never above `timeout_seconds`
  (issue 128). Absent, the attempt launches with the policy default, capped at
  `timeout_seconds`.
- `timeout_seconds` within policy bounds; `retry_on` a subset of both the
  `ExitClass` enum and the policy's `retry.eligible_classes`.
- The contract is authoritative for tier, provider, pin, and policy; the
  attempt is authoritative for the harness, model, and image that actually
  ran, and `GET /tasks/{id}` shows both. `POST /tasks/{id}/start` may carry
  an `overrides` object; applying it creates a new contract version through
  the amendment path and records an `amend` event.
- No string field contains something that matches the secret-pattern
  scanner (bearer-like tokens, private key headers). Rejected with 422 and
  the offending path, not the value.

## What the contract is not

It is not a prompt. Crucible renders the worker identity (06) from it. It is
not editable: an amendment or correction is a new version linked by events,
and an in-flight attempt keeps the version it started with. It never carries
a credential or a reference that resolves to one inside a worker.
