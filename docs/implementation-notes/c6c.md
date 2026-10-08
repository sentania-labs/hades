# C6c: readiness gaps that were code

C6c closes readiness rows 5, 7, 11, 12, 14, 21, and 23. Row 18 remains
unproven and unchanged because the real ledger handoff is a separate operator
act.

## Implementation

- `GET /v1/attempts/{attempt_id}/logs` returns offset-paged stdout, stderr, or
  combined text. A request with `Accept: text/event-stream` returns the same
  persisted log chunks as SSE records and ends only after the attempt is
  terminal and the stored tail is drained.
- Migration 0012 adds fenced heartbeat rows and extends the event constraint
  with `worker_quiet` and `worker_stalled`. Worker launch and log progress write
  activity signals. The supervisor warns once after `stall_warn_seconds`,
  creates the operator wake, then drains and kills after `stall_fail_seconds`.
  The attempt records exit class `timeout` and reason `stall`.
- A report present after a killed worker is stored as a `partial_report`
  artifact and is deliberately not parsed as a worker claim.
- Checkout lease contention is covered at integration level, including the
  single denial event and launch after the holder releases.
- The real GitHub target now has a required `crucible-readiness` check. The live
  test can force that check red and proves the task stops in
  `ci_certification_failed` without retry or correction.
- Live harness acceptance now promotes the selected image through the same
  image registry used by production routing. It compares each running
  container's `crucible.harness_version` label with the installed version
  returned by `/v1/harnesses`.

## Release evidence decision

The release workflow does not rerun test tiers. Readiness row 14 cites the `ci`
run on the tagged commit. "All tiers except live" means the five `ci` jobs:
`lint`, `scan`, `test`, `e2e`, and `compose-smoke`.

The most recent release tag, v0.2.1, points to commit
`a7a23679b85161982947abf49f5254cb6bf6d8eb`. Its green `ci` run is
https://github.com/sentania-labs/crucible/actions/runs/35169303005. That run
predates the addition of the `e2e` CI job, so it has four jobs. The current
five-job definition is proven green on `main` by
https://github.com/sentania-labs/crucible/actions/runs/35527704824. A future
release tag on a commit with the current workflow will carry all five jobs
without duplicating them in `release.yml`.

The C6c branch itself passed all five jobs at
https://github.com/sentania-labs/crucible/actions/runs/35533793026 after the
post-PR correctness fixes.

## External target evidence

The workflow was added to `sentania-labs/crucible-spike-target` through pull
request https://github.com/sentania-labs/crucible-spike-target/pull/93. Its
installation run is
https://github.com/sentania-labs/crucible-spike-target/actions/runs/35529498371.
The forced-red acceptance run is
https://github.com/sentania-labs/crucible-spike-target/actions/runs/35529796009.
The three version-comparison runs are:

- Claude Code:
  https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530104688
- Codex:
  https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530201700
- AGY:
  https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530268129

Each live test closed its temporary pull request and deleted its temporary
branch after recording the evidence.

## Local verification

Run on the reference workstation on 2026-09-20, America/Chicago.

| Tier | Result |
|---|---|
| `make lint` | clean: Ruff format, Ruff checks, mypy on 225 files, and 3 import contracts |
| `make test` | 579 unit tests and 301 integration tests passed |
| `make scan` | tree and branch history clean, no leaks found |
| `make e2e-image` | script harness built at digest `sha256:9b5b91e74522bf6e65d159d27fec2fd0815cc915f6b760f11ecc974c20fc6b73` |
| `make e2e` | 16 passed, 10 deselected, 100.43 s on the dedicated rootless daemon |
| `make up`, then `make smoke` | isolated host-daemon project healthy; full task, gates, review, and acceptance passed |
| `make e2e-github` | 3 passed, 1 skipped, 92.06 s; target PRs cleaned up |
| `make e2e-live HARNESS=all` | 3 passed, 23 deselected, 282.85 s; all three real harnesses reached `ready_for_merge` |
| `make e2e-admin` | 3 passed, 23 deselected, 50.31 s with the dedicated credential root |

The rootless service user's daemon cannot traverse the operator's home path for
Compose bind mounts. The compose smoke therefore used the normal host daemon,
a unique project name, no host PostgreSQL port, and a fresh volume. The default
Crucible volume was preserved. Temporary containers and networks were stopped
after the successful smoke.

## Review

The required non-author adversarial review ran before the Crucible pull request
opened. A fresh `codex exec` used model `gpt-5.6-terra`, reasoning effort none,
and the contract plus `origin/main...HEAD` diff. Its first read-only process
could not start because bubblewrap could not create a network namespace. That
process inspected nothing. The fresh unsandboxed process was instructed not to
modify files and completed the review.

It reported three blockers:

1. `container_running` and `progress_line` were excluded from the activity
   query. Accepted with the distinction required by spec 10: any signal now
   resets the quiet warning clock, while only substantive activity resets the
   final stall clock. `container_running` is substantive. Unverified progress
   prevents a warning but cannot keep a worker alive forever. A progress
   heartbeat is now stored, and unit plus integration coverage records this
   behavior.
2. Readiness contained a temporary CI run URL placeholder. Accepted as a
   delivery-sequencing blocker. The branch CI URL did not exist until this
   review was recorded and the pull request opened. The placeholder was
   replaced by the first green five-job run,
   https://github.com/sentania-labs/crucible/actions/runs/35532280495. The
   evidence-only replacement commit must also pass all five jobs.
3. This section still said the review was pending. Accepted and resolved by
   this findings and dispositions record.

The reviewer reported no non-blocking findings. Per the contract, there is no
second review round.

The repository's GitHub settings automatically started another review when the
pull request opened. It was not requested and is not a second contract review
round. Its three findings were direct correctness defects in the new C6c paths,
so they were fixed:

1. SSE log tails held the request-scoped authentication UoW until the stream
   ended. The route now authenticates and takes its initial snapshot in a short
   local UoW, then opens only short-lived polling UoWs.
2. Silent workers editing files could be killed because no live `fs_changed`
   heartbeat was emitted. The supervisor now fingerprints the writable checkout
   and report trees without following symlinks, and records substantive activity
   when either changes.
3. Cancellation could discard a parsed report when a provider supplied no raw
   YAML. The parsed mapping is now serialized and stored as the unparsed partial
   report artifact, with integration coverage.

The complete lint, scan, unit, integration, Docker e2e, Compose smoke, and CI
gates were rerun after these fixes. No additional review was initiated.

## Spec notes and follow-ups

No spec or ADR file was changed.

- Spec 04's log route is implemented beneath the API's existing `/v1` prefix.
- Spec 10's heartbeat record is now persistent and fenced. Progress signals do
  not replace the worker activity signals used for stall decisions.
- Spec 19 can mark the seven C6c rows proven once the branch CI URL is recorded.
- The v0.2.1 tagged commit predates the fifth CI job. The next release is the
  first tag that can carry the current five-job definition.
- Row 18 still requires the real ledger export, import, commit, and
  `mark-migrated` sequence with the operator's explicit authorization.
- The second-daemon Codex image reproducibility check and the rootless Compose
  location decision remain separate follow-ups already recorded by prior work.

## FDY-0341: workspace fingerprint budget guard (Issue 36)

The supervisor's `workspace_fingerprint` walks both the checkout and report trees
on every observation tick (the Docker path).  On a large worktree the walk can
exceed the observation interval and the stall clock is at risk.  FDY-0341 adds
the same kind of guard that the Kubernetes worker already uses:

- **Time budget** (`ACTIVITY_WALK_SECONDS`, value 10): the walk returns
  ``None`` once the elapsed monotonic time exceeds the budget.  ``None`` is
  treated by `_workspace_changed` / `_record_workspace_activity` as "could not
  tell" and does **not** reset the supervisor's stall clock.
- **Entry-count budget**: the walk prunes the entire ``.git`` subtree so that
  the heavy ``.git/objects`` directory is never traversed.
- **`_workspace_fingerprints` type** updated to
  `dict[str, tuple[int, int, int] | None]` so the dict can store the ``None``
  sentinel.

Cost measured on an NFS-hosted CI node (300 kB overlay, 512 MB /tmp tmpfs):

| Tree size | Walk time (with guard) |
|---|---|
| ~8 000 files (checkout + report) | 0.3 s (budget 10 s; guard not triggered) |
| 5 000 files, budget 1 ms (monkeypatch) | guard fires after a few hundred files; returns None |

The guard is a time check on every ``stat()`` call plus ``.git`` pruning at the
iterator level.  On a local SSD tree of tens of thousands of files the walk
finishes in well under a second so the guard rarely fires.  On NFS the guard
may fire, but the result is always ``None`` which the caller treats as "could
not tell" — a deliberate false-negative that preserves stall-clock correctness.

### P1 finding: directory symlink guard

A worker-controlled directory symlink (e.g. ``repo/peer -> ../../``) must not be
followed: descending it would scan ``<artifact_root>/workspaces`` and other
attempts, corrupting this attempt's fingerprint and resetting the stall clock.
The walker uses ``stat(follow_symlinks=False)`` followed by ``stat.S_ISDIR``
to distinguish real directories from symlinks, so only genuine directories
enter the traversal stack.

### P2 finding: iterative traversal

A checkout with roughly 1 000 one-character nested directories would exhaust
the Python call stack and raise ``RecursionError`` before the time budget
check.  The walker now uses an explicit ``list[Path]`` stack (``while stack``)
instead of recursive ``yield from _walk(child)`` calls, eliminating the
recursion depth limit entirely.

## FDY-0535: SSE log tail concurrency target (Issue 37)

Each live log tail polls its attempt and log chunks in a short-lived database
unit of work every 250 ms. The service now admits at most 20 concurrent tails
per process by default (`service.max_sse_log_tails`). The next tail receives
the `sse-tail-limit-exceeded` RFC 9457 problem with `429` and `Retry-After: 1`;
plain (non-streaming) log reads are not limited.

Measured with `uv run python tools/benchmarks/issue_37_sse_tail_concurrency.py
<tails> 2` against its SQLite test database on 2026-10-08:

| Tails | Elapsed | Queries | Queries/s | Connection checkouts | Peak connections in use |
|---:|---:|---:|---:|---:|---:|
| 1 | 2.016 s | 18 | 8.93 | 9 | 1 |
| 20 | 2.025 s | 360 | 177.74 | 180 | 1 |

Load is linear in tails: each poll is two statements (attempt, log chunks) on one
pool checkout, about 4.5 checkouts and 9 queries per second per tail, so the
20-tail cap bounds a process at roughly 180 queries per second from tails. The
benchmark counts SQL statements and pool checkout/checkin events only over the
timed interval, after every tail's initial snapshot. The poll's unit of work is
synchronous inside the async route, so tails never hold more than one connection
at a time in this measurement; that is a property of this local SQLite run, not
a production database capacity claim.

The limiter only gates admission. An admitted tail streams exactly as before
and returns its slot in the generator's `finally`, after the `event: end` it
sends when the attempt's logs are drained. A client that disconnects before the
generator first runs never reaches that `finally` (closing an unstarted async
generator runs none of its body), so the `StreamingResponse` also carries a
background task that releases the same permit; a permit releases only once.
An earlier revision of this branch released only from the background task and
wrapped the generator in handlers that swallowed cancellation, and the live tail
end-to-end test stopped seeing `event: end`; that revision was replaced.
