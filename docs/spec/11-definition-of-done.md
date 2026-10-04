# 11. Definition of done, gates, and evidence

## Four levels

1. **Worker completion.** The worker wrote a `CompletionClaimV1`, made local
   commits on `work_branch`, ran every required check, and exited 0. A
   claim.
2. **Crucible completion.** No blocking pre-PR gate the policy requires has
   failed for the collected head, the internal review is recorded, and the
   evidence rows the gates used exist. A failed advisory gate is listed for the
   reviewer instead (ADR 0024). Recorded as task state `gates_passed`. After
   publication, Crucible completion extends to the post-PR gates: external
   review rounds received and dispositioned, CI certification green on the
   final head. Recorded as `ready_for_merge`.
3. **Foundry acceptance.** Foundry read the claim, the diff, the verified
   evidence, and the review, and recorded an `AcceptanceResult` for that
   head. Semantic. Crucible never infers it. It precedes any push.
4. **User approval.** Consequential decisions (merge, release, accepted
   risk, scope change) recorded as `Decision` rows with verbatim words.
   Merge is the operator's act on GitHub, observed by Crucible. Release
   tagging requires a recorded operator authorization (24).

## CompletionClaimV1 (the worker report)

```yaml
schema_version: "1.0"
task_external_id: "FDY-0042"       # fact: optional, Crucible fills it
summary: "..."
self_review:
  documentation: ["docs/retries.md"]
  acceptance_criteria:
    - { id: "AC1", status: "met", evidence: "tests/ledger/test_import.py covers it" }
  omissions: []
changed_files: ["src/ledger/import.py", "tests/ledger/test_import.py"]   # fact
refs:                              # fact
  branch: "crucible/FDY-0042"
  head_sha: "abc123..."            # the worker's local HEAD; Crucible collects, never trusts
  commits: 3
checks:                            # fact
  - { id: "V1", command: "make lint", exit: 0, log: "report/V1.log" }
  - { id: "V2", command: "make test", exit: 0, log: "report/V2.log" }
  - { id: "V3", command: "make scan", exit: 0, log: "report/V3.log" }
acceptance_mapping:                # a list, or a mapping keyed by criterion id
  - { id: "AC1", status: "met", evidence: "tests/ledger/test_import.py::test_duplicate_id_409" }
  - { id: "AC2", status: "met", evidence: "report/V2.log" }
run_evidence: ["report/run-evidence.md", "report/screenshot-1.png"]   # fact
proposed_pull_request:
  title: "Return 409 on duplicate import ID"
  body: "..."                      # the worker's draft; Crucible renders the real body (23)
  closes: ["https://github.com/example-org/example-service/issues/17"]  # optional; must match the contract
limitations: ["..."]
risks: ["..."]
blockers: []
follow_ups: ["..."]
```

The worker writes judgement and Crucible derives facts (hades #215,
2026-09-28). The judgement fields are required, and empty lists are explicit:
`summary`, `self_review`, `acceptance_mapping`, `proposed_pull_request` (its `title` and
`body`), `limitations`, `risks`, `blockers` and `follow_ups`. The fact fields
are optional: at collection Crucible fills `task_external_id` from the task,
`changed_files` from the collected diff, `refs` from the collected branch
bundle, `checks` from its own verifier re-run and `run_evidence` from the run
evidence it copied out. A fact the worker did write is compared with
Crucible's and replaced by it; each difference is recorded as information in
the `report_present` detail, in words that repeat nothing of the worker's but
a hash or a number, and never fails the gate. Run evidence is compared one
way: Crucible collects every file in the report directory, so only a path the
worker listed that Crucible did not collect is a difference, and `V1.log`,
`report/V1.log` and `/crucible/report/V1.log` name the same file. The stored report is the
completed document; the worker's own document is kept as the worker's claim.
`acceptance_mapping` may be a list of entries or a mapping keyed by criterion
id (`AC1: {status: met, evidence: "..."}`); Crucible stores the list form.
Unknown fields are still refused.

`self_review` is required. It names where documentation was updated, maps every
acceptance criterion with evidence, and lists anything knowingly left out and why. This
worker self-review is the internal review. A missing section makes `report_present`
fail and names `self_review`. Once every blocking gate passes, Hades records acceptance
and publishes without an orchestrator review or acceptance call, on first attempts and
corrections alike. The review-report endpoint remains available for operator
out-of-band adversarial findings and is not a gate.

The worker image carries `crucible-report check <report.yaml>`, a
standard-library mirror of this schema that prints each problem in plain
words and reads the contract from the identity bundle to check that every
acceptance criterion has an entry. IDENTITY.md tells the worker to run it and
fix every problem before exiting 0. A unit test holds the checker and the
schema in agreement.

All paths are relative to `/crucible/report`. Missing or unparsable report is
a report-gate failure. An unparsable report is advisory by default (ADR 0024):
the reviewer sees it and decides; a file that is not YAML is unparsable, not
missing, and its parse error names the problem and position. No report at all
stops the task.
The claim has no `pushed`, `pull_request`, or `ci` fields: workers cannot
push and never see CI. Those facts are Crucible's to observe.

## ReviewReportV1 (operator out-of-band adversarial review)

```yaml
schema_version: "1.0"
task_external_id: "FDY-0042"
reviewed_head_sha: "abc123..."
reviewer: { kind: "crucible_review_execution", attempt_id: "01J..." }   # or { kind: "orchestrator", principal: "foundry" }
verdict: "approve"                 # approve | request_changes
findings:
  - { severity: "major", path: "src/ledger/import.py", line: 42, text: "..." }
summary: "..."
```

`reviewer_must_not_be_author` is enforced mechanically: the reviewer is a
different attempt (a `review` execution never shares an attempt with an
`implement` or `correct` one) or a different principal. A verdict of
`request_changes` does not move the task; Foundry reads it and decides.

## Derivation from the delivery pipeline

The operator's delivery skills define the pipeline: write, run every check
the repository defines, see the use case work, one non-author review before
the PR, PR opened only when the work is already proven, one external review
round, dispositions recorded, CI green on the final head, operator merge,
release by annotated tag through the repository's own release workflow.
Pre-PR verification is the proof; PR CI is the independent certification
that the submitted commit matches that proof. The PR is never the place to
find out whether the work builds.

What Crucible enforces is the mechanically checkable residue of that. What
it cannot check stays with Foundry or the user.

## Pre-PR gates (evaluated on the collected head, before any push)

Each pre-PR gate is **blocking** or **advisory** (ADR 0024, the operator's
decision of 2026-09-29). A failed blocking gate sends the task to
`pre_pr_gates_failed`. A failed advisory gate is recorded with its detail and
listed "for the reviewer" in the task view, the gate list, the admin UI's
Tasks page and the wake, and the task goes on to its internal review, or to
acceptance when the policy requires no review for that head. The policy's
`gates.advisory` decides (05b); the default is below, and
`internal_review_recorded` and `no_secrets` always block, and `commit_policy`
is always advisory.
`error` counts as `fail` in both classes.

| Gate | Default | Passes when | Evidence consumed |
|---|---|---|---|
| `report_present` | advisory, except no report at all | report parsed once Crucible filled its facts, every judgement field present; the detail names the facts Crucible filled and any the worker wrote differently (hades #215) | CompletionClaim artifact |
| `exit_clean` | blocking | exit code 0 and exit class `completed` or `completed_without_report` (an `incomplete` attempt exits 0 too, issue 128) | attempt exit info |
| `commits_present` | blocking | the collected `work_branch` has at least one commit beyond `base_ref`, the bundle verifies, and the bundle names its head. The head is the bundle's; a reported `head_sha` that differs is noted in the gate's detail and does not fail it (hades #187, 2026-09-28) | branch bundle from `collect` |
| `scope_contained` | advisory, except a prohibited path | every changed path matches `allowed_paths` and none matches `prohibited_paths`. A path matching `prohibited_paths` stops the task even when the gate is advisory; a path merely outside `allowed_paths` is for the reviewer | diff path list from `collect` |
| `no_injected_files` | blocking | no instruction additions, harness paths or normalized shim content in the diff or any commit on `work_branch`; unclassifiable evidence fails closed (details below) | normalized path records, base paths and blob classifications from the collector |
| `no_secrets` | always blocking | secret scanner over the diff, every commit message, and the report finds nothing | scanner output artifact |
| `verification_ran` | blocking | for each `required_verification` command: Crucible itself re-ran the command after exit, in a fresh verifier container from the collected tree (same image, `network` per policy), and its exit matches `expect_exit`. The worker's own check logs are stored as a claim and shown to Foundry, never consumed by the gate. A check the worker's report says passed and the re-run failed is also recorded as the advisory finding "the worker reported V3 passing; Crucible's re-run failed it" (ADR 0024) | verifier exit and log (verified) |
| `run_evidence_present` | advisory | each `kind: artifact` verification path exists and is non-empty | artifacts |
| `criteria_mapped` | advisory | every `acceptance_criteria.id` appears in `acceptance_mapping` with a status | report |
| `dependencies_unchanged` | blocking | when `may_add_dependencies` is false: lockfiles and manifests unchanged | diff |
| `ci_unchanged` | blocking | when `may_modify_ci` is false: no change under workflow paths | diff |
| `workspace_clean` | blocking | no leftover ephemeral clusters or containers labeled for this attempt | provider reconcile |
| `internal_review_recorded` | always blocking | a `ReviewReportV1` for this exact head SHA exists from a reviewer that is not the implementing attempt; `pending` until then (the task waits in `awaiting_internal_review`) | review report with reviewer identity |
| `commit_policy` | always advisory | every new commit is authored with the policy's `author_email`. The collector checks each commit's author, and a commit authored by someone else fails the gate, named by hash and address in the detail, so it is listed for the reviewer; it never stops the task. The attempt trailer is not checked. `skipped` only for an attempt collected before the check existed; `fail` (advisory) when the collector could not read the commits. The operator decided on 2026-09-29 that the trailer is not required and the task record is the paper trail (hades FDY-0143); before that the gate blocked (FDY-0135) | collector's author check over the collected checkout's commits, the ones the bundle carries |

`no_injected_files` matches names after Unicode NFC, casefold and removal of
zero-width and format characters, with common Cyrillic and Greek lookalikes folded
to Latin (hades #400, option 1). Any `AGENTS*.md`, `CLAUDE*.md` or `GEMINI*.md`
at any depth is an instruction name. Any entry or descendant of `.codex/`,
`.claude/`, `.hermes/`, `.gemini/`, `.crucible/`, `.crucible-shims/` or
`crucible/identity/` fails, including directory symlinks with those names. Symlink
targets are not traversed outside the collected tree. Existing identity/shim names
remain protected.

As decided in #369, ordinary edits and deletions of the repository's own instruction
files already present on the base are exempt. Ownership uses the original Git path,
so a newly added spelling variant cannot borrow the exemption. Replacing such a file
with a symlink still fails. Shim content is compared after normalizing line endings,
trailing whitespace and final newlines, including every historical blob on the branch.
The shell collector exports NUL-delimited raw records and bounded blobs by object id
using Git and the base image tools, without requiring Python in the worker image.
The service streams and classifies all raw names before filtering records; a Git
pathspec cannot perform this normalization. Empty lists are valid, and ordinary
paths do not require instruction blob classification. A truncated record fails
with a reason, without exhausting an iterator or suppressing commit records,
report artifacts or the verifier tree. Undecodable names, unreadable or undecodable instruction
blobs, malformed records and incomplete lists fail closed with a reason. Instruction
blobs above the 8 MiB classification limit also fail closed. Older evidence without
content classifications retains its conservative path/status and exact-blob checks.

`commit_policy` is evaluated whatever `gates.pre_pr` lists, and a policy may
not name it, so the reviewer always sees who authored the commits and a
policy stored before the gate existed still gets it. It is always advisory,
whatever `gates.advisory` lists, and `gates.advisory` may not name it
either.

## Publication and post-PR gates (23)

| Gate | Passes when | Evidence consumed |
|---|---|---|
| `branch_pushed_at_head` | remote `work_branch` head equals the collected head Crucible pushed, which equals the head the AcceptanceResult names | ls-remote after push |
| `pr_exists_head_matches` | PR exists, targets `deliverables.target`, head equals the pushed head, draft flag as the contract says, body is the one Crucible rendered | GitHub API |
| `external_review_rounds` | the count of accepted signals (review, comment, or `+1` reaction) from allowlisted logins on this PR is at least `required_rounds`; `pending` until then; `skipped` at 0 rounds. Advancement out of `external_feedback_received` requires this gate to pass, so a policy with more than one round waits for each | ExternalReview rows |
| `feedback_dispositions_complete` | every received review comment has a ReviewDisposition; `skipped` when `require_feedback_disposition` is false | dispositions |
| `ci_green_for_head` | the required-check set (23) is non-empty and every member concluded success on the accepted head; `pending` while any is queued or running **or while the set is empty**, so a head with no observed runs never passes; `fail` on any failure. Only `allow_no_ci: true` turns the empty set into `skipped` | CICertification |

Three evaluation rules. A pre-PR gate whose evidence is produced by
collection (`report_present`, `exit_clean`, `commits_present`,
`scope_contained`, `no_injected_files`, `no_secrets`,
`run_evidence_present`, `criteria_mapped`, `dependencies_unchanged`,
`ci_unchanged`, `commit_policy`) and is absent after collection is `fail`,
not `pending`, so a failed attempt reaches `pre_pr_gates_failed`
unambiguously when that gate blocks, and is listed for the reviewer when it
is advisory.
`internal_review_recorded` is the one pre-PR gate whose evidence arrives
after collection; it stays `pending` and the task waits in
`awaiting_internal_review`. A gate whose evaluator belongs to a later
phase (`verification_ran` and `workspace_clean` until C3's verifier
container exists) reports `deferred` (09): it is non-blocking for
`gates_passed`, is shown to Foundry with the phase that will implement
it, and can never report `pass`; once the evaluator ships, the gate
evaluates normally and `deferred` is no longer a possible result. The reviewer identity used
by `internal_review_recorded` and `reviewer_must_not_be_author` is the
authenticated principal that uploaded the report or the review attempt
that produced it, never the identity the document claims.

## Judgment (never a gate)

Whether the use case is actually seen working, whether review findings were
truly addressed, whether external feedback is correct or in scope, whether
"not exercised" is acceptable, why CI failed, whether the repository is
consumed by something when unclear, whether merged changes form a release,
whether a risk or limitation is acceptable, whether scope grew,
architectural soundness. Foundry evaluates these from the same evidence and
records an `AcceptanceResult`, a `ReviewDisposition`, or a `Decision`;
consequential ones go to the user as escalations.

## Evidence model (EvidenceV1)

`evidence`: `attempt_id` or `pull_request_id`, `kind` (exit_info,
diff_paths, diff_content, bundle_head, remote_head, pr_state,
review_received, check_run, scanner_result, artifact_present,
claim_parsed, review_report, verification_run, workspace_state,
transcript_match),
`observed_at`, `source` (`crucible`, `github`, or `worker`), `verified`
(true for `crucible` and for `github` deliveries that passed signature
verification or came from a poll), `payload`, `artifact_id`. Gates may
consume only `verified: true` evidence. Worker-asserted facts are shown to
Foundry but never satisfy a gate.
