# 23. GitHub delivery: publication, PR observation, external review, CI certification

Crucible, not the worker and not Foundry, performs every routine GitHub
mutation and watches the PR afterward. Workers edit, run checks, commit
locally, and write a claim. Foundry interprets and decides. This file is the
delivery half of the task lifecycle (09).

## Authority model

- Crucible holds a GitHub App private key (the credential it owns, 12, ADR
  0017) and mints
  short-lived installation tokens scoped to one repository, on demand, for
  one publisher job at a time. Tokens live in memory and in the tmpfs of
  the publisher container (on Kubernetes, the publisher Pod's per-push
  Secret volume, ADR 0022), never elsewhere. A private repository's
  preparation step also gets one, read-only (`contents: read`), for the
  length of that step only (ADR 0019, 12).
- App permissions: Metadata read, Contents read/write, Pull requests
  read/write, Checks read, Actions read, Issues read (for
  `GET /issues/{n}/reactions` only, S12 rerun). Nothing else, and no
  Issues write. Repository
  registration (`PUT /repositories/{name}`) records the installation ID
  the App has for that repository; the key never appears in the record.
  The GitHub page's repository picker fills it (25): it lists what each
  installation covers, grouped by account, using a token scoped to
  `metadata: read` that is discarded before the listing returns, and a pick
  registers the repository with that installation ID, the default branch
  GitHub reports, and whether GitHub says it is private. A private
  repository (ADR 0019) is registered only after the App has minted it a
  read-only checkout token, which is revoked at once; an archived one is
  listed and refused.
- **The push remote is derived from the repository's `owner/name`, not from
  its registered url.** The registered url is the fetch source: it is what
  the preparer and the collector clone. The collector never holds a GitHub
  credential; the preparer holds one only for a repository registered as
  private, read-only and for that one step (ADR 0019). Keeping the two
  separate still lets a deployment point the fetch at a local mirror, while
  the publisher pushes to GitHub with the App token. In an ordinary
  deployment the two resolve to the same GitHub repository.
- Workers receive no GitHub credential. The worker's checkout has its
  `origin` URL replaced with a placeholder; a `git push` inside a worker
  fails with no credential and is a recorded prohibition, not a gate.
- Any future mode that gives a worker direct GitHub access is a policy
  exception with its own ADR.

## Publication (task state `publishing`)

Runs only after: pre-PR gates passed for the collected head, the internal
review is recorded, and Foundry's `AcceptanceResult` for that head is
`accepted` (policy `publish_requires_acceptance`).

1. Event `publish_started` with the head SHA and the bundle artifact ID.
2. Mint an installation token for the repository (expires in one hour; the
   job is bounded well below that).
3. Launch a **publisher container**: the same hardened shape as the
   collector (08), uid 1000, on **its own egress network** with an
   allowlist of `github.com` and `api.github.com` only. That allowlist is
   narrower than the workers' proxy permits, which is the point: the one
   container holding a GitHub credential must not sit on a network that
   reaches every model endpoint a harness needs, so the publisher gets its
   own network, its own proxy, and its own allowlist, generated beside the
   workers' one. Mounts: an empty working directory (rw, tmpfs), the branch
   bundle artifact alone (ro), an output dir (rw). The bundle is copied out
   of the collector's output into a directory of its own first, so the
   container holding a credential sees one file and not the diff, the
   report copy, and the collected tree beside it.

   **The token is handed over on stdin, never copied in.** The container is
   created with stdin open, started, and attached to through the Docker API
   (`POST /containers/{id}/attach`, which is `docker run -i` by another
   name); the value is written to that stream and the write side is closed.
   The socket proxy must permit that endpoint. `docker cp` is not an
   option: it cannot reach a tmpfs inside a `--read-only` container, and
   without `--read-only` the value lands on the writable layer, which is
   disk (S10). The token is never in `Env`, in `Cmd`, in a bind source, or
   in Crucible's own argv. Inside the container it is a file on tmpfs read
   by a git credential helper that answers only for the configured protocol
   and host.

   **On Kubernetes the publisher is a Job, `publish-<attempt>` (26, ADR
   0022).** A Pod has no stdin to write to and no host directory to stage
   in, so the carriers differ and nothing else does: the same script, the
   same checks, the same outcome. The token is a Secret,
   `publish-token-<attempt>`, created for this push, mounted read-only
   (mode 0400) at the same path as a Secret volume, which the kubelet keeps
   in memory, and deleted as soon as the Job's Pod is gone, on every path.
   The bundle is mounted as one file, read-only, straight off the attempt's
   workspace claim where the collector left it
   (`output/work_branch.bundle`); a bundle path that is not that leaf of
   that attempt's claim is refused before anything is created. The Pod
   writes its outcome to the claim's `publish/` leaf, which Crucible reads
   back through the reader Pod over exec, never through a log. Its egress
   is its own NetworkPolicy (`github.com`, `api.github.com`, and
   `github.credential_host` when that is another host), resolved and pinned
   into the Pod's `hostAliases` (hades #191). A workers namespace whose
   egress enforcement is not proven gets no publisher Pod and no Secret.
4. Inside, the repository is **built from the bundle**, not cloned from a
   cache: `git init`, fetch `base_ref` from the remote the publisher is
   about to push to, `git bundle verify`, then fetch `work_branch` out of
   the bundle. Before any of that, the bundle is hashed inside the
   container and compared with the seal the collector recorded; a bundle
   that is missing or no longer matches is refused (exit 7) before any
   remote is contacted, and the credential helper is asked for the token
   (`git credential fill`), refusing (exit 3) unless a password comes back,
   so an unreadable token fails here and not as an authentication error at
   the remote. The bundle is `base_ref..work_branch`, so it names
   prerequisite commits and neither verify nor fetch will look at it until
   the repository holds them; the base fetch is what supplies them. Nothing
   else enters this container: not the worker's tree, not the worker's
   `.git`, not a cache a worker could have influenced. Then verify the
   fetched head equals the collected head SHA Crucible recorded; push
   `work_branch` to the derived push remote without force. The publisher
   does not check commit authors or trailers: on 2026-09-29 the operator
   decided the trailer is not required and the task record is the paper
   trail (hades FDY-0143). What it guarantees is that it pushes exactly the
   sealed, verified bundle at the reviewed and accepted head. Who authored
   the commits is shown to the reviewer before review, in the
   `commit_policy` gate's detail (11). A remote head that is not an
   ancestor of the bundle head (someone
   pushed out of band) fails the push, records `publish_failed` with the
   remote head, and wakes Foundry; Crucible never force-pushes. The one
   leased push is merge-main (below): it replaces only the head Crucible
   pushed or adopted, and git refuses it if the branch moved since.
5. From Crucible (API calls, same token): `ls-remote` to confirm
   `branch_pushed_at_head`; then open the PR if none exists for this task,
   or reuse the existing PR whose head just changed. **A reused PR is
   reconciled against the contract, not merely assumed to carry the new
   head**: its base is sent on the update, and a PR whose base or draft
   flag does not match what this publication expects fails publication with
   the mismatch recorded rather than advancing. GitHub does not accept
   `draft` on that endpoint, so a reused PR that is a draft is reported and
   refused, never silently corrected. PR title is the
   claim's proposed title after validation (length, no secret patterns, no
   closing keywords). PR body is rendered by Crucible from the contract
   and **verified** evidence only:
   - objective and acceptance criteria with their verified mapping,
   - each required verification command with the verifier's exit code and
     log artifact ID (never the worker's own logs as proof),
   - the internal review reference (reviewer kind, report artifact ID),
   - the correction history if any,
   - the attempt ID, image digest, harness and version,
   - the authorized closing references from `deliverables[].closes` as
     `Closes <ref>` lines; no other closing keyword survives rendering,
   - limitations and risks from the claim, labeled as worker-asserted.
   Nothing worker-asserted appears in the body as a verified fact, and
   nothing worker-asserted acts. Worker text is **defanged** before it is
   rendered: every closing keyword that precedes a reference is escaped, so
   a fix-and-reference line inside a limitation cannot close an issue
   nobody authorized, and every at-mention is escaped for the same reason,
   because a mention notifies a person, subscribes a team, and, for a
   provider whose reviewer answers its own name, triggers a review under
   whatever identity opened the PR. Escaping leaves the sentence readable
   and stops the provider acting on it. Backticks are not protection: the
   provider reads the raw body, not the rendered HTML. A proposed title
   carrying a secret pattern, a closing keyword, or an at-mention is
   refused rather than rewritten, because a silently rewritten title is a
   claim Crucible did not make; the secret scan runs before the length
   check, so a long title containing a token is refused for the reason that
   matters.
6. Event `publish_completed` with PR number, URL, head SHA, body hash.
   Then `pr_exists_head_matches` evaluates and the task moves to
   `awaiting_external_review` or `awaiting_ci_certification`.
7. Publisher container removed; token discarded. Any failure between 2
   and 6 is `publish_failed`, with the step and the API response class
   (never the token) recorded.

**A publication that cannot start is never silent.** A task in `publishing`
waits when the deployment has no GitHub App client, no publisher (no Docker
or Kubernetes provider wired to push), or an App credential that is not in
place yet. On each tick the supervisor records the reason on the task as a
`task_publish_pending` event, once per entry into `publishing` and again only
when the reason changes, and logs it at warning the same once. `GET
/supervisor` lists every such task under `github.publishing_waiting`, and the
admin UI's task list shows it under "Needs attention" as "Waiting to
publish". Once the task has waited `github.publisher_timeout_seconds` (the
publisher's own time limit, default 600) an escalation opens on the 09 path,
once, with a `publish_failed` wake. The wait resolves itself: the first tick
on which a publisher and a ready App exist publishes the task with no manual
step, and closes the escalation the wait opened.

## Observation

After publication Crucible watches the PR until the task is terminal.
Polling is the complete observation path; webhooks only shorten latency.

Every poll records GitHub's `mergeable` result and `mergeable_state` on the pull
request row. A conflicting pull request (`mergeable` false or `mergeable_state` `dirty`)
does not wait in CI certification. When its head is one Crucible pushed or adopted and
the task is in `awaiting_external_review`, `external_feedback_received`,
`awaiting_ci_certification`, `ci_certification_failed` or `ready_for_merge`, Crucible
raises one `pull_request_conflicting` wake per head, whose summary names the next
action, and asks the publisher to merge main itself: `merge_main` on the publisher port
runs the publisher's container (Docker) or Job (Kubernetes) with the same token handling
and egress, fetches the remote work branch, refuses unless it is still at the known tip,
and runs `git merge origin/<base_ref>` with no conflict resolution. A clean merge is
committed as Crucible and pushed with `--force-with-lease` against the known tip; the
coordinator records `branch_pushed` (reason `merge_main`) and the new head as pushed by
Crucible, and the task waits for that head's own checks (`awaiting_ci_certification`).
When git stops on conflicts, the publisher reports the conflicting paths and leaves the
branch untouched; the coordinator then attaches a correction under the task's policy
whose instruction is to merge `origin/<base_ref>`, resolve every conflict keeping both
behaviours, run the required checks, commit, and report, naming the conflicting files.
That correction starts from the remote branch tip (`resume_from_work_branch`), never
from the previous attempt's bundle, so the head it publishes fast-forwards the one on the
pull request. Only a failed correction reaches the orchestrator.

A dirty head someone else pushed is not acted on: the same poll moves the task to
`head_diverged`, and nothing is merged into that head or launched against it until the
head decision. `adopt` (legacy name `recollect`) re-runs the task from the remote branch
tip; once that run publishes, the head is Crucible's and is certified and merged as any.

A `ready_for_merge` task merges as soon as GitHub says it is mergeable and every check
run on its accepted head passed. It does not re-test that head against current main: a
head that is only behind main is merged. After Crucible merges it, the merge commit is
added to the watch list of the `release.main_ci_hold` setting and the supervisor judges
its checks on main once they complete. Red main opens one fix-main task, on a branch of
its own, carrying the failed jobs, the failing job's log tail, and the pull request
numbers merged since the last green main, and sets the release hold on that commit. A
green result clears the hold only when it is for the held commit itself (a re-run) or
for main's current tip as read from GitHub. A commit merged before the newest judged one
is superseded and is not polled again, so an older green commit can never lift a hold a
newer red one set.

- **Polling** (always on): every `github.poll_interval_seconds` (default
  120) the supervisor fetches, for every PR in an observed state: the PR
  (state, head SHA, mergeability, merged flag and merger), its reviews,
  its review comments, its issue comments, the reactions on the PR
  itself, on each review, and on each comment, the check runs and check
  suites for the current head, the workflow runs for the current head,
  and the base branch's required checks. Reactions are polled because
  GitHub delivers no webhook event for them; the configured reviewer
  signals "reviewed, no findings" with a thumbs-up, so the reaction poll
  is part of every cycle, not an extra. A comment's reactions are fetched
  only when the comment's own `reactions.total_count` says it has some.
  Every list is read past its first page, the check-run, check-suite, and
  workflow-run lists included. A rate-limit refusal is never waited out
  inside the supervisor's tick: it is recorded (`github_rate_limited`)
  and delivery resumes on the first tick after GitHub's `retry-after` (or
  rate-limit reset); a publication or a quota checkpoint push it
  interrupts stays where it was rather than failing. A CI decision or an operator waiver recorded since
  the last poll makes the pull request due at once.
- **Webhooks** (optional accelerator, off by default on a workstation;
  Q13): `POST /v1/github/webhook` accepts `pull_request`,
  `pull_request_review`, `pull_request_review_comment`, `issue_comment`,
  `check_run`, `check_suite`, `workflow_run`, and `push`. A review or
  comment delivery triggers an immediate reaction poll for its subject.
  Handling of each delivery: read the raw body under a **1 MiB cap**,
  enforced both on the declared `Content-Length` and on the read itself,
  because the HMAC is computed over the raw body and the body therefore has
  to be read before it can be verified; verify the HMAC against that body
  in memory; reject and count on mismatch, storing nothing. **A rejected
  delivery stores nothing an unauthenticated caller chose**, its claimed
  delivery id included: an unsigned request is an unauthenticated claim
  about everything in it. The rejection is counted as an event carrying the
  reason and, for the claimed event, one name from the handled set above or
  `unrecognized`. On a verified delivery: parse; extract
  only the fields Crucible uses; run every user-controlled text field
  (review bodies, comment bodies, titles) through the secret scanner and
  redaction; store the delivery ID, event and action, normalized fields,
  and a SHA-256 of the original body; discard the raw body. The same
  normalization and scanning applies to text fetched by polling before it
  is written to `review_comments` or `external_reviews`.
- Every observed change is an event: head changed (and by whom), review
  received, comment received, reaction received, check concluded, PR
  closed or merged.
- **Head changed out of band**: a head Crucible did not push moves the task
  to `head_diverged` (09). The previous head's acceptance, review report,
  and gate results are marked superseded and kept as history. Nothing
  about the new SHA is trusted: CI on it is observed and recorded but
  cannot move the task. Foundry decides `recollect` (the task re-enters
  `scheduled` with a `correct` execution against the remote `work_branch`,
  which is where the divergent head is, so the new head produces a
  completion claim and a bundle of its own; then the full pre-PR path,
  internal review as the policy and Foundry decide, acceptance, and
  `publishing`, which verifies the remote already matches) or `reject`.
  Re-entering at `reported` would put the new head in front of gates with
  no claim behind it (09).

Foundry is not required to remain connected for any of this.

## Triggering the external reviewer

S12's rerun (docs/history/spikes/S12.md, 2026-09-16) settled it: with the
repository's Codex setting "review all pull requests" enabled, an
App-authored PR is reviewed automatically (pickup 11 s after open,
completion 101 s) with no human comment. **Repository onboarding
prerequisite**: the Codex GitHub App must be installed on the repository
with review of all pull requests enabled (code and security review as the
operator chooses). GitHub exposes neither setting, so registration
(`PUT /repositories/{name}`) requires an operator **attestation**
(`external_review.attested_all_prs: true`, with the attesting principal
and time recorded as an event) whenever the repository's policy requires
external review; a registration without it is accepted only with
`external_review.required_rounds: 0`. Optionally `POST
/admin/repositories/{name}/probe-review` opens a throwaway App-authored
PR, waits for a reviewer signal, closes it, and records the observed
result; the first real task's reviewer signal also updates the record,
and a repository whose PRs get none is reported by the admin status. The
provider's configured trigger comment is posted by Crucible under the App
identity after the pull request is recorded when required rounds remain and
`external_review.request_on_publish` is true. The returned comment id is
recorded in `external_review_requested`. Republish checks the pull request's
issue comments and the event before posting, so the request is once per pull
request. A correction never posts another trigger.

**Crucible authors the trigger phrase only as the configured issue comment.** The
provider's trigger is an at-mention of its own name, and the provider acts
on the raw text: a PR body, a commit message, or a comment Crucible
authored that contained it would perform the trigger under the App's
identity. So the phrase in its at-sign form never appears in a
body Crucible renders or a commit message it writes,
and the renderer defangs every at-mention in worker-asserted text for the
same reason (a limitation or risk quoting the phrase would otherwise
trigger a review). Quoting it in backticks is not protection; the provider
reads the body, not the rendered HTML. Outside the policy field and the
single trigger comment, the phrase is described rather than repeated.

What the reviewer emits, as observed: a clean result is **reactions on
the PR only**: `eyes` on pickup (deleted on completion, so it lives about
90 to 150 seconds) and a durable `+1`; no comment, no review object, no
check run. A result with findings is a review object with inline
comments plus the summary comment. Reactions carry no commit id, so a
reaction's head binding is inferred from the PR head at its `created_at`
against the head history, and `require_review_on_final_sha` cannot be
satisfied by a reaction-only result; a policy that needs a per-head
result must use the trigger comment after each head. Code review and
security review are not separable in the GitHub record and are treated as
one round. A new head does not re-trigger the reviewer by itself.

Reading reactions needs the App permission **Issues: read** (the
endpoint is `GET /issues/{n}/reactions`; comments are readable with Pull
requests read alone). It is added to the App's permission set for that
one call (ADR 0007). Because the pickup reaction is transient, the
supervisor polls reactions every `github.reactions_poll_interval_seconds`
(default 60) while a PR is `awaiting_external_review`, and treats the
durable `+1` as the completion signal; a missed `eyes` is informational
only. The provider's summary comment
is posted within about ten seconds of the PR opening, minutes before any
verdict, and is then edited in place when the review lands; it never counts
as a round. It is recognized by the marker the provider puts at the top of
it, and a comment carrying that marker is not a round even under a policy
that has deliberately added `comment` to `accepted_signals`. A comment is
not an accepted signal by default at all (05b). The review object, its
comments, and the provider's no-findings result do count. Because the
summary is an issue comment, its edit is recorded and is not feedback: it
never steps a task back out of certification or `ready_for_merge`. Only an
edit to an inline review comment is feedback that needs a disposition.
Installation tokens are
about 390 characters with dots, not the short `ghs_` form, and the
redaction patterns cover both.

A round means one configured review cycle after publication. When the
repository's Codex configuration runs code review and security review in
that cycle, both are components of the same round; the round completes
only when every configured component has completed or reached a terminal
result. Crucible persists a review cycle row per PR head it published
(or per trigger it recorded) with the components the policy expects
(`external_review.components`, default `["code"]`; `["code",
"security"]` where the repository runs both), and attaches each received
signal to the open cycle by head SHA and, where the signal names one,
component. GitHub does not label the reviewer's signals by component, so
the rule is: a review object or review comment attaches to the component
its body names if any, else to `code`; a reaction-only clean result
(`+1` with no review object) completes **every** configured component of
the cycle at once, because the provider emits one combined verdict. The
`external_review_rounds` gate counts completed cycles, never individual
signals; a cycle with two components and one review object with findings
stays open until the second component's result or the cycle timeout. When `retrigger_after_correction` is true the same
trigger path applies to every corrected head: Crucible wakes the
orchestrator with reason `external_review_trigger_needed`, the
orchestrator posts the trigger under the operator's account, and a new
cycle opens on that head.

## External review (bounded input, not a loop)

- A round is one completed cycle. **A signal never opens a cycle**: the
  cycle row is created at publication of a head, or when a retrigger is
  recorded, with the components the policy expects. Signals complete the
  components of a cycle that is already open. A signal counts only from a
  login in `external_review.reviewer_logins` and only when its kind is in
  `external_review.accepted_signals`: by default a submitted review or a
  `+1` reaction on the PR (the configured reviewer's "no findings"
  signal), with a comment accepted only where a repository opts in (05b).
  Any other user's activity, including reactions, is recorded but satisfies
  nothing. Rounds are counted per PR across heads.
- A round with no findings (a `+1` reaction, or a review with no comments)
  moves the task through `external_feedback_received` with nothing to
  disposition; Crucible records it and, when `required_rounds` is
  satisfied, advances to CI certification without a wake for judgment.
- Received: Crucible stores the review, its comments (each with ID, path,
  line, body, reviewed SHA), and reactions. On the first Codex round with findings it
  builds one correction from those verbatim findings and the task's objective, scope,
  required verification and report rules, then launches it under the task's policy.
  The instruction is to fix each finding or decline it in the report with the reason.
  Foundry receives one informational wake and may still attach a correction or cancel.
  Findings from a later round on the corrected head wake Foundry but cannot start a
  second automatic loop. Automatic scheduling requires `external_review.provider: codex`;
  other providers retain the wake-and-wait path.
- The correcting worker's report records one disposition per finding: fixed with its
  commit, or declined with reasoning. Duplicate IDs invalidate the report and the
  validation error names them. Only after the attempt succeeds does Crucible store
  these as `ReviewDisposition` rows and reply to each declined inline finding with
  the reported reason. Summary and security-summary comments without inline findings
  are recorded as noted and need
  no disposition.
- If any disposition is `fix`, Foundry attaches a correction contract (05)
  and the task re-enters supervision against the existing branch. The
  correcting worker reruns every required check; Crucible re-verifies,
  Foundry accepts, Crucible pushes the corrected head.
- Advancement out of `external_feedback_received` is the dispositions
  gate's decision: every received comment dispositioned **and none of them
  `fix`**. A `fix` is Foundry saying the work is not done, so a
  dispositioned-but-fix set is not advancement, it is a correction. Only
  once that gate passes does the round count choose the branch: at or above
  `required_rounds` the task goes to `awaiting_ci_certification`, and with
  rounds outstanding it returns to `awaiting_external_review` to wait for
  the next cycle. Conditioning the advance on both gates at once would make
  the second branch unreachable and park a task with `required_rounds: 2`
  forever. With the
  default policy (one round, no retrigger after correction, no
  requirement on the final SHA) a correction never causes a second round.
  Other repositories may set `required_rounds`,
  `retrigger_after_correction` (on each corrected head Crucible opens a new
  cycle and wakes the orchestrator with reason
  `external_review_trigger_needed`; publication does not post again), and
  `require_review_on_final_sha` (the last accepted signal must name the
  accepted head) differently.
- Nothing received is overdue silently: `wait_timeout_hours` produces a
  repeat wake. **The clock starts when the task entered the state it is
  waiting in**, read from that transition's own event, not when the PR was
  opened. The same rule governs `ci_certification_overdue`. Measuring from
  the PR would make a correction on a three-day-old PR overdue on its first
  poll.

The connector reply beginning "To use Codex here, create a Codex account" is a terminal
failed round, not a review result. Crucible raises an informational wake immediately so
the task cannot wait silently (hades #343).

## CI certification

- Count every observed non-skipped check run and workflow job on the accepted
  head SHA. Check suites are containers and do not count. Branch protection
  and ruleset names do not participate. The policy's
  `ci_certification.required_checks` defaults to `[]`; a non-empty list is
  an explicit narrowing by name of observed runs, never a source of missing
  checks to wait for. Runs sharing a name each count.
- Green: the counted set is non-empty and every member concluded `success`.
  Queued or in-progress runs keep certification pending. A `neutral` result
  also stays pending until `wait_timeout_hours` wakes Foundry; skipped runs
  are excluded. Details report counts, such as "9 of 9 jobs succeeded on
  <sha>" or "2 of 9 jobs still running".
  **An empty set is `pending`, never green**: before GitHub
  has created any run, or on a repository with no CI, the task waits and
  `wait_timeout_hours` wakes Foundry with `ci_certification_overdue`. A
  repository that intentionally has no CI needs
  `ci_certification.allow_no_ci: true`, an operator-recorded policy
  decision, which makes the gate `skipped` rather than passed. For one
  task, the operator's `accept_no_ci` decision (ADR 0025) does the same
  when the policy narrowing is empty and nothing has run on the accepted
  head (a run a path filter skipped counts as nothing). A non-empty narrowing
  with no observed runs stays pending even with this waiver; a check that
  does run is still certified. On green
  the task moves to `ready_for_merge` and wakes Foundry.
- Failed: any counted run concluded `failure`, `cancelled`,
  `timed_out`, `action_required`, `stale`, or `startup_failure`. Crucible captures the check name,
  workflow, job, head SHA, and the available log excerpt (through the
  Actions read permission), writes a `CICertification` row with state
  `failed`, moves the task to `ci_certification_failed`, and wakes Foundry.
  No automatic retry. No automatic worker correction. The excerpt is the
  tail of the failed job's own log: a check run from Actions is a job, so
  its log is `GET /actions/jobs/{id}/logs`, and a failed workflow run is
  resolved to its first failed job. GitHub answers with a redirect to a
  signed URL on another host, which Crucible follows without sending the
  token. It is fetched once per failed run, not on every poll, and a log
  that cannot be read leaves the excerpt empty rather than failing the
  poll.
- Foundry's `POST /tasks/{id}/ci-decision` records the cause from the enum
  `false_pre_pr_evidence`, `wrong_sha_checked`, `correction_without_checks`,
  `environment_drift`, `flaky_test`, `crucible_verification_defect`,
  `implementation_defect`, `missing_worker_tooling`, `ci_infrastructure`,
  `other`, and the action: `rerun` (Crucible records the intent and wakes
  the operator to re-run it on GitHub, because re-running needs Actions
  write, which the App does not hold; 22), `correct` (a correction
  follows), `reject`, `cancel`. A `correct` action requires a cause other
  than `ci_infrastructure` and `flaky_test`; a `rerun` requires one of
  those two; any other combination is refused with 422 (hades #356). After
  `rerun` the failure the decision was about is stale: the decision's
  event lists the failed runs (GitHub's id and when each concluded), those
  runs are not counted again, the certification reads `pending` with a
  detail that says so, and the task waits for a fresh result. Any other
  failure, including a re-run of a workflow that fails again under the
  same id, is a new failure.
- A task in `ci_certification_failed` that observes a green (or skipped)
  certification on its accepted head goes back to
  `awaiting_ci_certification` and on to `ready_for_merge`: someone re-ran
  the check, with or without a decision, and the failure no longer
  describes the head.
- A head that changes while awaiting certification is a divergence
  (above), not a new certification: the task leaves the certification
  path until Foundry decides.
- `wait_timeout_hours` without a conclusion produces a wake with reason
  `ci_certification_overdue`.

## Merge

With `delivery.auto_merge` enabled (the default), Hades squash-merges the certified
and accepted head at `ready_for_merge` through the GitHub App installation token.
There is no hold window: the merge runs on the same observation tick that passes the
post-PR gates, including the external review round and finding dispositions. Automatic
merge requires a green certification with every observed eligible job successful, even
if policy narrows the CI gate. Check suites and completed skipped jobs are excluded as
in certification. A skipped no-CI certification does not authorize automatic merge.

Immediately before merging, Hades reads the live PR and compares its head and base with
the accepted head and persisted contract target, and requires the PR to be open. Polling
never adopts a retargeted base. It then re-reads the task state, accepted head,
certification, policy opt-out and live global switch before the call. The merge request
specifies squash and the certified head as GitHub's SHA precondition. GitHub has no base
precondition on this endpoint, so the fresh base comparison cannot atomically prevent
a retarget between the read and the merge call.

The merge result records the SHA from the merge response and the merger and time from
an immediate PR read. An ambiguous failed response is checked for a completed merge
before a refusal is recorded. A refusal wakes Foundry naming its cause. Persisted
refusals retry after 60 seconds, doubling to a maximum of 30 minutes. A changed observed
head, base or mergeability state allows an earlier retry; only a changed refusal cause
produces another wake. These are failure retries, not a hold on newly ready heads.

`delivery.auto_merge: false` leaves the task ready for an operator. Administrators can
also disable automatic merges globally through `GET` or `POST
/v1/admin/delivery/auto-merge` with `{"enabled": false}` or the switch on `/ui/settings`.
The deployment switch defaults to enabled, is persisted as `delivery.auto_merge` in
provider settings, and is read for each merge without a restart. Enabling it again does
not override a policy opt-out. A switch change cannot cancel a merge request already
sent to GitHub. Observation of a person's merge continues with either switch disabled.

Crucible also observes `pull_request.closed` with `merged: true`, records the merge SHA,
the merger login, and time, moves the task to `merged`, and wakes Foundry
(informational). It does so from any delivery state, not only
`ready_for_merge`: a person can merge before review or CI is done, and a
merged pull request is never polled again, so the task would otherwise wait
for ever. The wake says which state the task was in. A correction against
an open PR (from `ready_for_merge` or any other correctable delivery state)
takes the task back through `scheduled`, `running`, and the pre-PR gates
while the PR stays open, so the PR is still polled then, for its merged flag
alone: a merge observed while the correction is scheduled, running, or
gated moves the task to `merged` too, and the supervisor ends the
correction's attempt as it ends a cancelled one (hades #360). The same holds
while the corrected head is publishing or its publication failed (hades #379).
A task that already has a pull request never gets a second one, and its pull
request is looked up before the corrected head is pushed and again after.
The task's own pull request is read by its number, every pull request open
on the work branch is listed (a reopened older one included), and none of
the others is ever adopted. When the task's own pull request is open, the
publication goes on to it; the others open on the work branch are recorded
on the `publish_completed` event (`other_pull_request` and
`other_pull_request_state` for the first, `other_pull_requests` for all)
and named in an `other_pull_request_open` wake, or, when the publication
fails, in its `publish_failed` event and wake. When the task's own pull
request is merged or closed, its merge or close is recorded and nothing more
is pushed: a merge settles the task as `merged`, and the others are recorded
on its `task_publish_failed` event and named in an `other_pull_request_open`
wake; a close fails the publication with a `publish_failed` wake naming the
pull request (and every other one on the branch) that says to reopen the
pull request and then republish, or to cancel, as the poll's close wake in
`publish_failed` does; a republish
while the pull request is still closed fails the same way. A publication
failure that finds the task already moved on (a poll settled it as
`merged`) is not reported. A head pushed after the task left `publishing`
(a poll settled it as `merged` while the push ran) is not recorded as a
pushed head; it is escalated, and the publication stops there without
editing the merged pull request or counting itself published. The merge
wake on this path says whether the head GitHub merged is among the heads
Crucible pushed, and is an escalation when it is not, when it was only a
quota checkpoint, or when Crucible pushed a later head after it (a push
that landed after the merge, however the merge was first recorded). A PR closed without
merge moves the task to `rejected` from any delivery state, `head_diverged`
included, with the closer recorded, and wakes Foundry with
`pull_request_closed`. The closer is not on the PR itself: `GET
/pulls/{n}` carries `merged_by` and no closer, so Crucible reads the actor
from the issue events timeline (`GET /issues/{n}/events`, the last `closed`
entry). That second call is **best effort**: a repository whose timeline
the App cannot read records the close with no actor rather than failing the
observation.

## Ready-for-merge report

When the task reaches `ready_for_merge`, the wake carries: PR URL, final
head SHA, external review summary with every disposition, CI certification
summary with check names and run URLs, the internal review reference, and
the correction history. Foundry reports the PR as ready to the operator
from this record; Crucible produces the facts, Foundry the sentence.
