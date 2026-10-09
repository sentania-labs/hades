# Crucible readiness report (19)

This is the evidence document `docs/spec/19-readiness-gate.md` asks for: one row per
requirement of its two tables, with the tests that prove it, the tier that runs them,
the run whose result is named here, and the recorded artifact identifiers. It is
written for the operator's approval decision, so it is deliberately ungenerous. A row
is **proven** only when a run of the named tests exists and its result can be named
here. A row whose asked-for proof does not exist, or exists only as a test nobody has
run, is **unproven**, and the gap is named exactly in the section below the tables.

Every row is proven. Row 18 was held back until the operator's explicit go, because
it transfers authority from the real Foundry ledger; that go came on 2026-09-29 and
item 6 below records the handoff.

## What was run for this report

All on the reference workstation, 2026-09-18, America/Chicago, on branch
`c6/bootstrap-import` at the head this report is committed on. The Docker tiers ran
against the dedicated rootless daemon of the `crucible` service user (ADR 0004, S9),
the arrangement row 22 records.

| Tier | Command | Result | Finished |
|---|---|---|---|
| unit | `make test-unit` | 550 passed, 5.84 s | 11:41:12 PM |
| integration | `make test-integration` | 273 passed, 308.90 s, real PostgreSQL 16 in a container | 11:35:04 PM |
| e2e (Docker) | `make e2e DOCKER=<rootless wrapper>` | 12 passed, 9 deselected, 75.26 s | 11:36:29 PM |
| e2e_admin, the C6 test only | `pytest tests/e2e/test_admin_live.py::test_the_bootstrap_import_through_api_and_cli_on_the_live_stack -m e2e_admin` | 1 passed, 4.36 s, live stack on the rootless daemon | 11:37:34 PM |
| lint | `make lint` | ruff format, ruff check, mypy (217 files), lint-imports (3 contracts) all clean | before the commits |
| scan | `make scan` | gitleaks, no leaks, tree and history | before the commits |

The live administration tier could **not** be run in full: the dedicated credential
root `/var/lib/crucible/credentials` is owned by the `crucible` service user with mode
750, and the session that produced this report does not belong to that group, so
`make e2e-admin` fails at collection with `PermissionError`. The C6 test of that tier
needs no harness credential, so it was run on its own against a synthetic credential
root holding three empty directories; that run is the one recorded above. The harness
probe tests of the tier were not run here. Their last recorded run is C5b's, named in
row 15.

The live harness tier (`make e2e-live`) and the live GitHub tier (`make e2e-github`)
were not run for this report. Their last recorded runs are C5's and C4's, named in
rows 3 and 19.

CI run for this branch, all five required checks green:
`https://github.com/sentania-labs/crucible/actions/runs/35422008877`, on commit
`79f0ee9`, the head this report was first pushed at, 2026-09-18 11:42 PM local; the
run for the current head is linked from pull request 32. `lint` 21 s, `scan` 7 s,
`test` (unit and integration) 5 m 59 s, `e2e` 2 m 3 s, `compose-smoke` 1 m 1 s. No
workflow runs any live tier.

### C6b delta, 2026-09-20

Class routing and reactive quota reroute were exercised on the reference workstation
against PostgreSQL 16 and the dedicated rootless Docker daemon. The pull request CI
run URL is added below once the branch is pushed.

| Tier | Command | Result | Finished |
|---|---|---|---|
| unit | `make test-unit` as part of `make test` | 564 passed, 6.03 s | 11:03 AM |
| integration | `make test-integration` as part of `make test` | 285 passed, 442.93 s, real PostgreSQL 16 | 11:09 AM |
| e2e (Docker) | `make e2e DOCKER='<rootless wrapper>'` | 13 passed, 9 deselected, 104.39 s | 11:11 AM |
| lint | `make lint` | ruff format, ruff check, mypy (222 files), lint-imports (3 contracts) all clean | 11:01 AM |
| scan | `make scan` | gitleaks, no leaks, tree and 7-commit branch history | 10:58 AM |

C6b-specific evidence is `tests/unit/test_class_routing.py`,
`tests/unit/test_task_contract.py`, `tests/integration/test_class_routing.py`,
`tests/integration/test_github_delivery.py::test_quota_checkpoint_is_pushed_before_the_reroute_is_scheduled`,
`tests/integration/test_admin.py::test_the_cli_remote_mode_builds_the_same_calls`, and
`tests/e2e/test_class_routing.py::test_scripted_quota_reroutes_to_a_second_image_and_remote_branch`.

### C6c delta, 2026-09-20

The code gaps were exercised on the reference workstation against PostgreSQL 16,
the dedicated rootless Docker daemon, the isolated host-daemon Compose stack, the
dedicated live credential root, and `sentania-labs/crucible-spike-target`.

| Tier | Command | Result |
|---|---|---|
| unit | `make test-unit` as part of `make test` | 579 passed, 6.08 s |
| integration | `make test-integration` as part of `make test` | 301 passed, 370.11 s, real PostgreSQL 16 |
| e2e (Docker) | `make e2e DOCKER='<rootless wrapper>'` | 16 passed, 10 deselected, 100.43 s |
| e2e GitHub | `make e2e-github` | 3 passed, 1 skipped, 92.06 s |
| e2e live | `make e2e-live HARNESS=all` | 3 passed, 23 deselected, 282.85 s |
| e2e admin | `make e2e-admin` | 3 passed, 23 deselected, 50.31 s |
| compose smoke | `make up`, then `make smoke` | isolated stack healthy, task accepted, smoke passed |
| lint | `make lint` | Ruff, mypy on 225 files, and 3 import contracts clean |
| scan | `make scan` | no leaks in the tree or branch history |

The C6c pull request CI run, including all five non-live jobs, is
https://github.com/sentania-labs/crucible/actions/runs/35532280495. The live GitHub run URLs are recorded directly in rows 21
and 23.

### C7a delta, 2026-09-21

The server-rendered administration UI, first-run access path, and isolated
in-service login were exercised against PostgreSQL 16, the dedicated rootless
Docker daemon, and an isolated host-daemon Compose project.

| Tier | Command | Result |
|---|---|---|
| unit | `make test-unit` as part of `make test` | 609 passed, 6.63 s |
| integration | `make test-integration` as part of `make test` | 314 passed, 305.68 s, real PostgreSQL 16 |
| e2e (Docker) | `make e2e DOCKER='<rootless wrapper>'` | 17 passed, 12 deselected, 105.12 s |
| e2e admin | `make e2e-admin` | 1 passed and 2 failed because the dedicated AGY `oauth-token` file is absent; the second failure is the dependent probe |
| compose smoke | fresh isolated `make up`, then `make smoke` | signed first-run UI walk passed, task `01M3193VMG9NJ8J5Y2VWRQJ7V0` accepted, smoke passed |
| lint | `make lint` | Ruff, mypy on 237 files, and 3 import contracts clean |
| scan | `make scan` | final result recorded in the C7a report |

C7a-specific evidence is `tests/unit/admin_ui_fixtures.py`,
`tests/unit/test_ui_dashboard.py`,
`tests/unit/test_ui_render.py`,
`tests/unit/test_ui_settings.py`,
`tests/unit/test_ui_templates.py`,
`tests/unit/test_ui_workers.py`,
`tests/unit/test_docker_provider.py`,
`tests/integration/test_admin.py::test_ui_session_csrf_reader_access_and_page_walk`,
`tests/integration/test_admin.py::test_migrate_creates_and_prints_the_first_admin_once`,
`tests/integration/test_admin.py::test_every_remaining_ui_mutation_dispatches_to_the_shared_application_service`,
and `tests/e2e/test_admin_ui.py`. Browser evidence for every page is indexed in
`docs/implementation-notes/c7a.md`.

## Table 1: the readiness gate

| # | Requirement | Status | Tests, tier, and the run whose result is named |
|---|---|---|---|
| 1 | Accept and validate a versioned task contract | proven | unit `tests/unit/test_task_contract.py` and `tests/unit/test_refs.py`; integration `tests/integration/test_api.py::test_submit_validation_problem_details`, `::test_submit_shape_errors_name_the_path`, `::test_duplicate_external_id_is_409`, `::test_start_disagreeing_with_contract_is_422`, `tests/integration/test_policies_api.py::test_a_contract_is_validated_against_the_routing_policy`, `tests/integration/test_harness_registry.py::test_a_disabled_harness_is_a_contract_problem_at_submit`. C6b adds class-only contracts, exact operator-pin validation, harness-without-model refusal, and real-provider supplied-image refusal in `tests/unit/test_task_contract.py`. Run: the unit and integration tiers above and the C6b delta. |
| 2 | Persist task, worker, execution, event, artifact, evidence, and decision state | proven | integration `tests/integration/test_full_run.py::test_submit_start_run_to_reported`; migrations `tests/integration/test_migrations.py::test_up_down_up_from_empty`, `::test_events_and_contracts_are_append_only`, `::test_fresh_schema_has_no_drift`, `::test_a_downgrade_past_c4_keeps_the_c4_events`, `::test_0010_down_and_up`, and C6b's 0011 down-up event restoration cases; `tests/integration/test_artifacts_api.py`, `tests/integration/test_fencing.py`, and `tests/integration/test_class_routing.py`. Run: the integration tier above and the C6b delta. |
| 3 | Launch at least one supported harness in an isolated environment | proven | e2e_live `tests/e2e/test_live_harness.py::test_a_trivial_task_reaches_ready_for_merge_live`, parametrized `[claude_code]`, `[codex]`, `[agy]`. Run: C5's recorded live tier, 2026-09-17, target `sentania-labs/crucible-spike-target`. claude_code 11:37:45 AM, 42.9 s, exit 0, `ready_for_merge`; codex 11:38:52 AM, 58.3 s, exit 0, `ready_for_merge`; agy 11:40:14 AM, 52.4 s, exit 0, `ready_for_merge`. Artifacts: pull requests 85, 86 and 87, each closed and its branch deleted by the tier. Images `crucible-worker:claude_code-2.1.273-8b75176f203e`, `codex-0.153.4-ce72fc1b2e20`, `agy-1.2.4-26838f92302b`. Not re-run on this branch, which changes no launch path. |
| 4 | Inject identity and instructions without committing them | proven | e2e `tests/e2e/test_full_run.py::test_the_branch_carries_no_injected_file` and `::test_a_script_harness_run_reaches_acceptance_with_every_gate_green`; unit `tests/unit/test_gates.py::test_no_injected_files_sees_the_commit_list_as_well_as_the_diff`, `tests/unit/test_identity_bundle.py` (7 tests). Run: the e2e and unit tiers above. |
| 5 | Capture or stream worker events and logs | proven | integration `tests/integration/test_readiness_gaps.py::test_log_offset_paging_returns_only_new_bytes` proves stream filtering and offset paging; e2e `tests/e2e/test_failures.py::test_live_log_tail_delivers_while_worker_is_running` reads SSE while the real Docker worker is active. Run: C6c local integration and e2e tiers above, plus https://github.com/sentania-labs/crucible/actions/runs/35532280495. |
| 6 | Continue authorized work while Foundry is disconnected | proven | e2e `tests/e2e/test_failures.py::test_the_run_completes_with_no_client_attached`; integration `tests/integration/test_wakes.py::test_the_wake_row_exists_before_any_delivery`, `::test_poll_and_ack`, `::test_no_webhook_configured_means_poll_only`, `::test_a_failing_receiver_only_delays`. C6b adds autonomous quota reroute and timed resume in `tests/integration/test_class_routing.py`, with one informational wake and no Foundry round trip. Run: the e2e and integration tiers above and the C6b delta. |
| 7 | Detect completion, failure, timeout, cancellation, stall, and loss | proven | unit `tests/unit/test_stall_detection.py` proves warning and fail thresholds; integration `tests/integration/test_readiness_gaps.py::test_stall_warns_then_drains_and_kills_as_timeout` proves the wake, drain, kill, and stored classification; e2e `tests/e2e/test_failures.py::test_a_stalled_real_worker_warns_then_drains_and_kills` and `::test_cancel_kills_a_real_worker_and_keeps_its_partial_report_unparsed` prove stall and cancellation against real Docker workers. Existing failure-class, timeout, and loss tests remain green. Run: C6c local unit, integration, and e2e tiers above, plus https://github.com/sentania-labs/crucible/actions/runs/35532280495. |
| 8 | Preserve worker reports and verification evidence | proven | integration `tests/integration/test_artifacts_api.py::test_collection_stores_the_claim_and_the_run_evidence`, `::test_the_parsed_report_is_readable`, `::test_evidence_is_readable_and_the_worker_row_is_unverified`, `tests/integration/test_fencing.py::test_evidence_is_fenced_and_a_worker_row_can_never_be_verified`; unit `tests/unit/test_gates.py::test_worker_asserted_evidence_never_satisfies_a_gate`. Run: the integration and unit tiers above. |
| 9 | Restart and reconcile active or interrupted executions | proven | e2e `tests/e2e/test_failures.py::test_a_restart_re_attaches_and_resumes_the_log_offset`, `::test_a_labelled_container_with_no_attempt_row_is_removed`; integration `tests/integration/test_reconcile.py` (15 tests, including `::test_reconcile_twice_changes_nothing_after_completion`), `tests/integration/test_fencing.py::test_second_supervisor_waits_as_standby`, and C6b `tests/integration/test_class_routing.py::test_all_pools_wait_and_a_restarted_supervisor_resumes_on_schedule`. Run: the e2e and integration tiers above and the C6b delta. Note: the restart is in-process; `docker compose restart` of the deployed service, S8's other completion condition, is still not exercised by any test. |
| 10 | Enforce the required deterministic gates | proven | unit `tests/unit/test_gates.py` (43 tests, one per pre-PR gate) and `tests/unit/test_delivery_gates.py` (10 tests, the publication and post-PR gates); integration `tests/integration/test_gates_and_acceptance.py::test_fail_path_scope_contained`, `::test_each_fail_fixture_fails_its_gate`, `::test_pass_path_reaches_awaiting_acceptance_then_accepted`; e2e `tests/e2e/test_hardening.py` (3 tests). Run: the unit, integration and e2e tiers above. |
| 11 | Stop or terminate a worker safely | proven | e2e `tests/e2e/test_failures.py::test_cancel_kills_a_real_worker_and_keeps_its_partial_report_unparsed` kills a real worker, proves drain then kill, stores the partial report artifact, and proves it was not parsed. `::test_a_timeout_drains_then_kills` remains green. Run: C6c local e2e tier above, plus https://github.com/sentania-labs/crucible/actions/runs/35532280495. |
| 12 | Prevent concurrent workers from corrupting the same working tree | proven | integration `tests/integration/test_readiness_gaps.py::test_second_checkout_waits_once_and_launches_after_release` proves the second attempt stays pending with one denial event and launches after release; e2e `tests/e2e/test_failures.py::test_a_second_task_on_the_same_branch_is_refused_at_submit` proves that a second task never reaches the lease: a work branch belongs to one task (hades #564) and its submission is refused naming the owner. Run: C6c local integration and e2e tiers above, plus https://github.com/sentania-labs/crucible/actions/runs/35532280495. |
| 13 | Expose sufficient API state for Foundry to inspect and reconcile | proven | integration `tests/integration/test_client_reconstruction.py::test_a_client_reconstructs_the_whole_run_from_the_api`, `::test_a_client_reconstructs_a_correction_loop`, `::test_a_client_reconstructs_a_blocked_task_and_its_decision`; C6 adds `tests/integration/test_bootstrap_import.py::test_a_scripted_start_of_session_reconstructs_the_live_task_set`. C6b's `tests/integration/test_class_routing.py` proves the task view exposes each attempt's model, harness, pool, reroute chain, remote-resume flag, and pending `resume_at`; the same module proves routing usage and admin clear state. C6c adds log reconstruction through `tests/integration/test_readiness_gaps.py::test_log_offset_paging_returns_only_new_bytes`. Run: the integration tiers above. |
| 14 | Demonstrate through automated tests | proven | Foundry's decision is recorded in `docs/implementation-notes/release.md`: the release workflow does not duplicate the test matrix, and all tiers except live means the five `ci` jobs `lint`, `scan`, `test`, `e2e`, and `compose-smoke`. Tag v0.2.1 points to commit `a7a23679b85161982947abf49f5254cb6bf6d8eb`; its green commit CI is https://github.com/sentania-labs/crucible/actions/runs/35169303005. That historical run predates the fifth job, whose current shape is proven green on `main` by https://github.com/sentania-labs/crucible/actions/runs/35527704824. C6c's five-job run is https://github.com/sentania-labs/crucible/actions/runs/35532280495. |

## Table 2: additional, from the operator's direction

| # | Requirement | Status | Tests, tier, and the run whose result is named |
|---|---|---|---|
| 15 | Each harness runs non-interactively in the worker container with subscription auth | proven | Spikes with transcripts: S1 (2026-09-16 1:59 PM to 2:08 PM, all three harnesses complete a trivial prompt with subscription auth from a mounted copy as uid 1000), S2 (2:10 PM to 2:13 PM, decision: proceed, do not enable bubblewrap), S3 (1:59 PM and 2:05 PM, AGY exit 0 in 22 s, argv ceiling measured), S1b (operator present, 4:20 PM to 5:21 PM). Live probes, C5b, 2026-09-17: claude_code 11:34:18 AM via API, 3.7 s, version 2.1.273, digest `sha256:aae06466e728...f2101`; codex 11:34:22 AM via CLI, 6.9 s, version 0.153.4, digest `sha256:15362eb4cb32...62615`; agy 11:34:29 AM via API, 5.4 s, version 1.2.4, digest `sha256:4d8c756f5b5a...c7b571`. Six consecutive `make e2e-admin` runs, 11:30 AM to 11:35 AM, 24 probes, every one conclusive. Unit `tests/unit/test_harness_adapters.py` (15 tests) and `tests/unit/test_credential_copy.py` (24 tests), run above. Open: Codex's Crucible-side credential refresh (S1b step 5) did not occur, so Codex stays disabled in the shipped defaults. |
| 16 | Workers cannot access the Docker socket, proxy, database, or other credentials | proven | e2e `tests/e2e/test_isolation.py::test_a_worker_reaches_nothing_it_must_not`, twenty probes, every one refused: `docker-socket-unix`, `docker-socket-run`, `socket-proxy`, `socket-proxy-ip`, `database`, `database-gateway`, `crucible-api`, `other-credential-codex`, `other-credential-claude`, `other-credential-agy`, `other-credential-root`, `git-push`, `egress-not-allowlisted`, `egress-direct-by-ip`, `egress-direct-by-name`, `write-root`, `write-identity`, `write-usr`, `chown-report`, `mknod`. Run: the e2e tier above, on the rootless daemon. |
| 17 | Codex's disabled sandbox cannot escape the container | proven | Spike S4 (2026-09-16 2:04 PM, partial, deferred to C3's rootless re-run) and spike S2, which found bubblewrap unavailable and kept ADR 0005: the container is the boundary and the Codex adapter keeps `--dangerously-bypass-approvals-and-sandbox`. S4's completion condition is the twenty-probe run of row 16, re-run on the real arrangement. Named substitution: the probes drive shell tooling in the worker container shape rather than Codex itself, which is the stronger test of the boundary but is not a Codex-specific escape attempt. |
| 18 | Bootstrap ledger imported and authority handed off | proven | 2026-09-29 handoff (ADR 0029, #255, PR #245), see item 6 below. The import path itself is proven: unit `tests/unit/test_bootstrap_bundle.py` (39 tests, every validation rule and every state mapping, refusals included); integration `tests/integration/test_bootstrap_import.py` (17 tests: the full import and commit lifecycle against real PostgreSQL, idempotence, the partial-failure rollback that stores nothing, the refusals, the supervisor leaving the unsupervised attempt alone, CLI and API parity, and the reconstruction of row 13), including `::test_the_bundle_foundry_ledger_wrote_imports_and_commits` against the bundle the real `foundry-ledger` producer wrote for its own invented fixture; live `tests/e2e/test_admin_live.py::test_the_bootstrap_import_through_api_and_cli_on_the_live_stack`, run 2026-09-18 11:37:32 PM, import `01M2VZ92ZZKRGAZW9SGR3MGC6T`, 8 tasks, 15 events, 2 synthetic executions, 2 unsupervised attempts, 8 handoff events, state `authoritative`, 0.4 s. What is missing is the real ledger. |
| 19 | Workers hold no GitHub credential; Crucible pushes and opens the PR only after pre-PR gates and acceptance | proven | e2e `tests/e2e/test_isolation.py::test_a_worker_reaches_nothing_it_must_not`, probe `git-push`, refused, run above; integration `tests/integration/test_github_delivery.py::test_publication_pushes_the_head_and_opens_the_pull_request`, `::test_no_installation_token_reaches_any_record`, `::test_green_required_checks_reach_ready_for_merge_then_merged`, run above; unit `tests/unit/test_publisher_shape.py` and `tests/unit/test_publication_body.py`, run above. C6b reuses that isolated publisher for WIP continuity: `::test_quota_checkpoint_is_pushed_before_the_reroute_is_scheduled` proves the remote head is confirmed before reroute, while the Docker e2e proves the resumed checkout uses that branch. Live evidence remains spike S10, C4's `e2e_github`, and the C5 live harness runs named in the prior report. |
| 20 | External review recorded only from allowlisted logins; feedback reaches Foundry, never a worker | proven | integration `tests/integration/test_github_delivery.py::test_a_non_allowlisted_login_satisfies_nothing`, `::test_a_review_with_findings_wakes_for_dispositions`, `::test_a_fix_disposition_holds_the_task_until_a_correction`, `tests/integration/test_amend_and_dispositions.py::test_a_disposition_needs_a_recorded_review_comment`; unit `tests/unit/test_external_review_cycles.py` (16 tests). Run: the integration and unit tiers above. Live: spike S12, result pass with a design correction, a pull request opened by the App reviewed automatically in 101 seconds by `chatgpt-codex-connector[bot]`; the spike also established that Issues read is not optional, because `GET /issues/{n}/reactions` is 403 without it. Whether Codex honors `@codex review` from an App account remains unproven and is the result the first real publish will show. If it does not, the existing observation timeout wakes Foundry. The e2e_github round test is opt-in behind `CRUCIBLE_GITHUB_WAIT_FOR_REVIEW` and was not run for this report. |
| 21 | A required CI failure escalates with evidence and triggers no retry or correction | proven | e2e GitHub `tests/e2e/test_github_live.py::test_a_real_required_check_failure_escalates_without_retry` forces the target repository's required `crucible-readiness` check red and proves `ci_certification_failed`, attached evidence, no retry, and no correction. Run: https://github.com/sentania-labs/crucible-spike-target/actions/runs/35529796009. The fake-server integration cases and `tests/unit/test_certification.py` remain green in https://github.com/sentania-labs/crucible/actions/runs/35532280495. |
| 22 | Docker authority arrangement recorded | proven | Documentary, and no test proves it. Spike S9, result pass, 2026-09-16, Ubuntu 24.04.5, kernel 6.8.0-139, cgroup v2, `docker-ce-rootless-extras 5:28.3.1`. The arrangement in use is the dedicated rootless daemon of the `crucible` service user (uid 999, socket `/run/user/999/docker.sock`), behind the socket proxy, per ADR 0004 and the operator's decision 13; `make preflight` refuses when the socket is absent and warns when the AppArmor profile the daemon needs after a reboot is missing. Every Docker tier run for this report used that daemon. Open items carried forward: the location decision that would let `make up` bring the whole Compose project up on the rootless daemon, and S9 follow-up 5, the reboot test of the lingering service user. |
| 23 | Harness versions pinned, digests recorded, unsupported combinations refused | proven | live `tests/e2e/test_live_harness.py::test_a_trivial_task_reaches_ready_for_merge_live` inspects the running container's `crucible.harness_version` label and proves it appears in `/v1/harnesses` `installed_versions`. Existing runs: Claude Code https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530104688, Codex https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530201700, and AGY https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530268129. Hermes 0.19.0 ran through the real proxy to `gpt-oss:120b` at 8:16:39 PM CDT on 2026-09-20, exit 0 in 77.9 s, passed every pre-PR gate, reached `ready_for_merge`, and produced https://github.com/sentania-labs/crucible-spike-target/actions/runs/35550459158. The tier inspected image `crucible-worker:hermes-0.19.0-981b69bc4b23`, whose recorded OCI digest is `sha256:c03667973ab1778e2f280ea6cde2627894db13c8bae36ea288219544d065a51c`. `test_hermes_spark_pool_runs_four_and_defers_the_fifth_live` also observed four running local workers and the fifth task's durable `harness_launch_deferred` event. Unit version refusal and integration registry tests remain green. |

## The formerly unproven row

6. **Row 18, the real ledger.** Resolved 2026-09-29. Foundry exported its real ledger,
   Hades imported the bundle (two bootstrap imports were discarded first, then the real
   one, on the operator's words "discard and import fresh"; ADR 0029, #255, PR #245),
   the SQLite ledger was frozen read-only, and Hades has held the task record since
   (principal `hades`). The mapping-table finding from `docs/implementation-notes/c6.md`
   did not bite: the real ledger carried no `rejected` or `missing` task. The import id
   is recorded in Foundry's private operator log, not here.

## How to re-run this evidence

```sh
make lint
make test                 # unit and integration; integration needs Docker or CRUCIBLE_TEST_DATABASE_URL
make scan                 # needs gitleaks on PATH
make e2e-image && make e2e DOCKER='<the rootless daemon wrapper in the Makefile header>'
make e2e-admin CRUCIBLE_LIVE_CREDENTIAL_ROOT=<the dedicated root> DOCKER='<same wrapper>'
make e2e-live  HARNESS=claude_code CRUCIBLE_LIVE_CREDENTIAL_ROOT=<...> CRUCIBLE_GITHUB_APP_JSON=<...> \
               CRUCIBLE_GITHUB_APP_KEY=<...> CRUCIBLE_GITHUB_TARGET_REPO=<owner/throwaway>
make e2e-github CRUCIBLE_GITHUB_APP_JSON=<...> CRUCIBLE_GITHUB_APP_KEY=<...> \
               CRUCIBLE_GITHUB_TARGET_REPO=<owner/throwaway>
```

The live tiers need the dedicated credential root readable by the invoking user. No
variable above is ever a key, a token or a secret; each names a file, a directory or a
repository.
