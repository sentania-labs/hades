# ADR 0024: The review is the enforcement; paperwork gates are advisory

Status: accepted. The operator's decision of 2026-09-29: "We need to let the review be
our enforcement rather then dictating behavior" and "I want to stop fucking around ...
so do what it takes". Built by FDY-0138 on 2026-09-29. Amends spec 11's rule that every
required pre-PR gate must pass, and spec 05b's `gates` section.

## Context

Every pre-PR gate blocked. On the lab, Hermes did correct work and the task still went
to `pre_pr_gates_failed` because it touched a file outside a narrow `allowed_paths`
list, or because its report left out a judgement field. Neither is damage and neither is
a false claim; each is something a reviewer can weigh in seconds. Blocking on them made
Foundry write a correction for work that was already right.

## Decision

1. **Each pre-PR gate is blocking or advisory.** A failed blocking gate keeps today's
   behaviour: the task goes to `pre_pr_gates_failed`. A failed advisory gate still
   runs and records its result and detail, but the task goes on to
   `awaiting_internal_review`, and the failure is carried in front of the reviewer.
   When the policy requires no internal review for the head (a correction under
   `required_for_corrections: false` that does not ask for one), the task goes to
   `gates_passed` and `awaiting_acceptance` as before, and Foundry, whose acceptance
   is then the review, gets the same list in the `gates_passed` wake. `error` is treated as `fail` either way.
2. **The default.** Blocking, where the damage is real or a claim is false:
   `verification_ran`, `no_secrets`, `ci_unchanged`, `dependencies_unchanged`,
   `commits_present`, `no_injected_files`, `workspace_clean`, `exit_clean`, and
   `internal_review_recorded`. Advisory: `scope_contained`, `report_present`,
   `criteria_mapped`, `run_evidence_present`. A gate added later is blocking unless a
   policy lists it. `commit_policy` (FDY-0135, which no policy lists) is always
   advisory: the operator decided on 2026-09-29 that the commit trailer is not
   required and the task record is the paper trail (FDY-0143), so a commit authored by
   someone other than the policy's author is listed for the reviewer and the trailer is
   not checked. `report_present` is advisory for a report that is malformed or
   lacks a judgement field; no report at all still stops the task. A `report.yaml`
   that is there but is not YAML, or not a mapping, is malformed, not absent: it is
   recorded as a present report that did not parse, and the parser's problem and
   position (never the file's text) are listed for the reviewer.
3. **A prohibited path always blocks.** `scope_contained` is split by its outcome: a
   path matching the contract's `prohibited_paths` stops the task whatever the gate's
   class, and a path merely outside `allowed_paths` is advisory. The stored row for
   that evaluation says `blocking`.
4. **The policy decides.** `gates.advisory` lists the advisory pre-PR gates; every
   other pre-PR gate blocks. It is validated (pre-PR gates only, no duplicates, never
   `internal_review_recorded` or `no_secrets`, which always block, and never
   `commit_policy`, which is always advisory) and versioned with the rest of the
   policy. `no_secrets` is fixed because a secret, once pushed, cannot be taken back,
   and never committing a secret outranks any policy. The gate stays blocking; within
   it, a match whose value is a fixture the repository itself declares (in its
   `.gitleaksignore` or `.gitleaks.toml` allowlist, read from the merge base) is
   listed for the reviewer instead of failing it, since that value is already in the
   repository (FDY-0618, spec 11). A version
   without the field, including every version written before it existed, takes the
   default set when it is read. Nothing is rewritten on upgrade: the lab's
   `default-software` version 8 and `hades-self-hosting` version 1 carry no list and so
   take the default, with no new version and no change to a version a task references.
   The shipped examples write the default out. Listing a gate outside the default set
   (turning a safety gate advisory) is an operator-only setting, recorded as a
   decision, like `allow_no_ci` (from the local admin CLI, which has no principal row,
   on the upload event only).
5. **The reviewer sees it.** Each gate result records its class (`gate_results.blocking`,
   migration 0028). The task view's `gate_summary`, `GET /v1/attempts/{id}/gates` (the
   CLI's `--gates`) and the admin UI's Tasks page mark each gate blocking or advisory and
   list the failed advisory gates under "for the reviewer" with their detail. `failing`
   names only what stops the task. The `internal_review_needed`, `gates_passed` and
   `pre_pr_gates_failed` wakes carry the same list as `for_reviewer`, and their summary
   names the gates; a detail, which can carry a path the worker chose, is never put in
   the summary line. A Crucible review execution is launched with the contract and the
   head, as before, and does not receive the list.
6. **A contradicted claim is its own finding.** When the worker's report says a
   required check passed and Crucible's own re-run failed it, `verification_ran`
   records the advisory finding "the worker reported V3 passing; Crucible's re-run
   failed it" (`gate_results.findings`), listed for the reviewer as a trust problem.
   The check failing is still a blocking `verification_ran` failure. Only the
   contract's own check ids are echoed.
7. **Every tunable has a UI.** The advisory set is shown and edited on the Routing page
   (Advisory gates, in force and in its own form), from `GET` and
   `POST /v1/admin/gates/advisory`, and from `crucible admin gates advisory` and
   `crucible admin gates set-advisory --gate NAME ...`. Each save writes a new policy
   version with only that list changed.

## Consequences

A task can reach Foundry's acceptance with a changed file outside its allowed paths or
an incomplete report. That is the point: the reviewer decides, with the detail in front
of it. The reviewer's `request_changes` verdict still does not move the task by itself
(11); Foundry reads it and decides.

The local admin CLI has no principal row, so an operator-only setting it saves is
recorded on the upload event, not as a Decision row.

Rolling back to a release before 0028 drops the class and the findings from the stored
gate rows, and that release blocks on every gate again.

## Amendment: the gates judge the work, not the time sheet (hades #498, 2026-10-06)

The operator's rule: the pre-PR gates judge whether the worker returned work and
whether it passes, never whether the paperwork is complete. Twelve finished attempts
had been scored `completed_without_report` because the correction report lacked
`finding_dispositions`, a field the worker instructions never mention, and budget stops
with commits had been scored as losses.

1. `report_present` is always advisory, as `commit_policy` is: no report at all, a
   report that does not parse, a missing self-review and a correction finding without
   a disposition are each listed for the reviewer with a plain detail and never fail
   the attempt. Point 2's "no report at all still stops the task" no longer holds.
   The blocking gates for an attempt with commits are the ones about the work:
   `commits_present`, `verification_ran`, `scope_contained` (for a prohibited path),
   `no_injected_files`, `no_secrets`, `dependencies_unchanged`, `ci_unchanged`,
   `workspace_clean`, `editor_leftovers` and `exit_clean`.
2. Hades composes the completion record itself from the commits on the branch, the
   checks it re-ran and the diff against each review finding's path; the worker's
   report adds to it and never gates it (11, "The completion record").
3. A clean exit with commits and no report is `completed`, never
   `completed_without_report`. A stop on the time or turn budget with commits is
   `ended_by_budget`: a normal end, collected and gated like a completed run.

## Amendment: advisory findings annotate publication (hades #602, 2026-10-09)

The worker self-review is the internal review. A failure of `commit_policy`,
`criteria_mapped`, `report_present`, `run_evidence_present` or advisory
`scope_contained` is stored with its detail as a reviewer note on the task and rendered
in the pull request body. It does not move the task to `awaiting_internal_review` and
does not require an orchestrator review or acceptance call. Hades records acceptance
and proceeds to publication when all blocking gates pass. A blocking gate failure keeps
the existing stop and correction behavior. The automatic acceptance reasoning states
what it rests on: that every blocking gate passed, that the report carries the worker
self-review only when `report_present` passed, and the name of each advisory gate that
failed and was recorded as a reviewer note.
