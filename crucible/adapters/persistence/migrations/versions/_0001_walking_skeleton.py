"""C1 walking skeleton: principals, repositories, policies, tasks, task_contracts,
executions, attempts, events, leases, completion_claims, supervisor_status,
idempotency_keys; append-only and fenced-token triggers; the default policy.

Revision ID: 0001_walking_skeleton
Revises: None
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_walking_skeleton"
down_revision = None
branch_labels = None
depends_on = None

ID = sa.String(26)
TZ = sa.DateTime(timezone=True)

# Adding a kind is a migration (10). Keep this list in step with crucible.domain.events.
EVENT_KINDS = (
    "task_submitted",
    "task_scheduled",
    "task_running",
    "task_reported",
    "task_blocked",
    "task_cancel_requested",
    "task_cancelling",
    "task_cancelled",
    "task_retry_scheduled",
    "transition_rejected",
    "contract_rejected",
    "execution_created",
    "execution_active",
    "execution_succeeded",
    "execution_failed",
    "execution_cancelled",
    "attempt_created",
    "attempt_preparing",
    "attempt_launching",
    "attempt_running",
    "attempt_timeout_drain",
    "attempt_timeout_kill",
    "attempt_cancel_kill",
    "attempt_terminating",
    "attempt_exited",
    "attempt_lost",
    "attempt_collected",
    "attempt_succeeded",
    "attempt_blocked",
    "attempt_failed",
    "attempt_adopted",
    "report_parsed",
    "report_parse_failed",
    "supervisor_lease_acquired",
    "supervisor_lease_released",
    "orphan_removed",
    "principal_created",
    "repository_registered",
)

# Rows written on behalf of the supervisor carry this principal and are fenced.
FENCED_PRINCIPAL = "crucible"

# Operator defaults from 05b, the initial policy every C1 contract references.
DEFAULT_POLICY = {
    "schema_version": "1.0",
    "name": "default-software",
    "version": 1,
    "description": "Software repositories consumed by something else: branch, PR, merge, tag.",
    "limits": {
        "timeout_seconds": {"min": 300, "max": 14400, "default": 3600},
        "max_attempts": {"max": 3, "default": 2},
        "grace_seconds": 60,
        "stall_warn_seconds": 300,
        "stall_fail_seconds": 1800,
        "auth_retry_delay_seconds": 600,
        "escalation_stale_hours": 24,
        "wake_retry_hours": 24,
    },
    "retry": {"eligible_classes": ["environment", "lost", "auth_failure"], "auth_failure_max": 1},
    "concurrency": {"per_provider": 3, "per_harness": {"claude_code": 1, "codex": 1, "agy": 1}},
    "resources": {"cpus": 2, "memory": "4GiB", "pids": 512, "tmpfs_total": "20GiB"},
    "network": {
        "mode": "egress-proxy",
        "egress_allowlist": [
            "github.com",
            "objects.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "registry.npmjs.org",
        ],
        "harness_endpoints": "from-harness",
    },
    "images": {
        "allowlist": ["crucible-worker:*", "ghcr.io/sentania-labs/crucible-worker:*"],
        "require_default_or_retained": True,
    },
    "git": {
        "author_name": "crucible-worker",
        "author_email": "crucible-worker@users.noreply.github.com",
        "commit_trailer": "Crucible-Attempt",
        "work_branch_pattern": "crucible/*",
        "protected_branches": ["main", "release/*"],
    },
    "repository": {"required_checks": ["make lint", "make test", "make scan"]},
    "gates": {
        "pre_pr": [
            "report_present",
            "exit_clean",
            "commits_present",
            "scope_contained",
            "no_injected_files",
            "no_secrets",
            "editor_leftovers",
            "verification_ran",
            "run_evidence_present",
            "criteria_mapped",
            "dependencies_unchanged",
            "ci_unchanged",
            "workspace_clean",
            "internal_review_recorded",
        ],
        "publication": ["branch_pushed_at_head", "pr_exists_head_matches"],
        "post_pr": [
            "external_review_rounds",
            "feedback_dispositions_complete",
            "ci_green_for_head",
        ],
        "skipped": [],
    },
    "deliverables": {"allow_branch_only": False, "on_out_of_band_head": "block"},
    "pull_request": {
        "require_pre_pr_verification": True,
        "open_only_after_pre_pr_gates_pass": True,
        "publish_requires_acceptance": True,
        "title_from": "claim",
        "body_template": "default",
        "closing_refs": "contract_only",
    },
    "internal_review": {
        "required": True,
        "required_for_corrections": False,
        "reviewer_must_not_be_author": True,
        "executor": "orchestrator_or_crucible",
    },
    "external_review": {
        "provider": "codex",
        "reviewer_logins": ["chatgpt-codex-connector[bot]"],
        "required_rounds": 1,
        "retrigger_after_correction": False,
        "require_review_on_final_sha": False,
        "require_feedback_disposition": True,
        "accepted_signals": ["review", "comment", "reaction:+1"],
        "round_counting": "per_pull_request",
        "wait_timeout_hours": 24,
    },
    "ci_certification": {
        "require_green_on_final_sha": True,
        "required_checks": [],
        "allow_no_ci": False,
        "on_failure": "escalate",
        "automatic_retry": False,
        "automatic_worker_correction": False,
        "wait_timeout_hours": 6,
    },
    "release": {
        "require_operator_approval": True,
        "authorization_recorder": "orchestrator_relay",
        "trigger": "tag",
        "tag_pattern": "v{major}.{minor}.{patch}",
        "version_files": [],
        "changelog_required": True,
    },
    "cleanup": {
        "workspace_on_success": "keep_diff_only",
        "workspace_on_failure": "keep",
        "container_remove": "always",
        "credential_volume_remove": "immediately_after_validated_sync",
    },
    "retention": {
        "logs_and_transcripts_days": 90,
        "bootstrap_archive_days": 180,
        "completed_workspaces_days": 14,
        "wakes_after_ack_days": 30,
        "indefinite": [
            "events",
            "completion_claims",
            "decisions",
            "gate_results",
            "review_reports",
            "external_reviews",
            "dispositions",
            "ci_certifications",
            "release_records",
            "diffs",
            "artifact_metadata",
        ],
    },
}

REJECT_MUTATION_FN = """
CREATE OR REPLACE FUNCTION crucible_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = 'CRU02';
END
$$ LANGUAGE plpgsql;
"""

FENCED_TOKEN_FN = """
CREATE OR REPLACE FUNCTION crucible_check_fenced_token() RETURNS trigger AS $$
DECLARE
    setting text;
    presented bigint;
    current_token bigint;
BEGIN
    IF TG_TABLE_NAME = 'events' THEN
        IF NEW.principal <> 'crucible' THEN
            RETURN NEW;
        END IF;
    END IF;
    setting := current_setting('crucible.fenced_token', true);
    IF setting IS NULL OR setting = '' THEN
        RAISE EXCEPTION 'write to % requires a transaction-local crucible.fenced_token',
            TG_TABLE_NAME USING ERRCODE = 'CRU01';
    END IF;
    presented := setting::bigint;
    -- FOR SHARE waits for an in-flight takeover to commit, then sees its token.
    SELECT fenced_token INTO current_token FROM leases
        WHERE kind = 'supervisor' AND key = 'supervisor' FOR SHARE;
    IF current_token IS NULL OR current_token <> presented THEN
        RAISE EXCEPTION 'stale fenced token % for % (current %)',
            presented, TG_TABLE_NAME, current_token USING ERRCODE = 'CRU01';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_TABLES = ("events", "task_contracts")
FENCED_TABLES = ("executions", "attempts", "completion_claims", "supervisor_status", "events")


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("id", ID, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("token_salt", sa.LargeBinary, nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("disabled_at", TZ, nullable=True),
        sa.CheckConstraint(
            "role IN ('orchestrator', 'operator', 'observer', 'admin')", name="ck_principals_role"
        ),
    )
    op.create_table(
        "repositories",
        sa.Column("id", ID, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=False),
        sa.Column("installation_id", sa.BigInteger, nullable=True),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("registered_by", sa.String(128), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
    )
    op.create_table(
        "policies",
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("retired_at", TZ, nullable=True),
    )
    op.create_table(
        "tasks",
        sa.Column("id", ID, primary_key=True),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("repository_id", ID, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("project", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("state", sa.String(48), nullable=False),
        sa.Column("contract_version", sa.Integer, nullable=False),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.Integer, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("closed_at", TZ, nullable=True),
        sa.UniqueConstraint("principal_id", "external_id", name="uq_tasks_principal_external"),
    )
    op.create_index("ix_tasks_state", "tasks", ["state"])
    op.create_index("ix_tasks_principal_updated", "tasks", ["principal_id", "updated_at"])
    op.create_table(
        "task_contracts",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("submitted_at", TZ, nullable=False),
        sa.UniqueConstraint("task_id", "version", name="uq_task_contracts_version"),
    )
    op.create_table(
        "executions",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("contract_version", sa.Integer, nullable=False),
        sa.Column("harness", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("effort", sa.String(32), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("image", sa.Text, nullable=False),
        sa.Column("policy_snapshot", JSONB, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("retry_on", JSONB, nullable=False),
        sa.Column("timeout_seconds", sa.Integer, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("ended_at", TZ, nullable=True),
        sa.CheckConstraint("role IN ('implement', 'correct', 'review')", name="ck_executions_role"),
    )
    op.create_index("ix_executions_task", "executions", ["task_id"])
    op.create_table(
        "attempts",
        sa.Column("id", ID, primary_key=True),
        sa.Column("execution_id", ID, sa.ForeignKey("executions.id"), nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("workspace_path", sa.Text, nullable=True),
        sa.Column("handle", sa.Text, nullable=True),
        sa.Column("identity_sha256", sa.String(64), nullable=True),
        sa.Column("image_digest", sa.String(128), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("started_at", TZ, nullable=True),
        sa.Column("ended_at", TZ, nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("exit_class", sa.String(32), nullable=True),
        sa.Column("timeout_at", TZ, nullable=True),
        sa.Column("drain_deadline", TZ, nullable=True),
        sa.Column("killed_at", TZ, nullable=True),
        sa.Column("termination_reason", sa.String(32), nullable=True),
        sa.UniqueConstraint("execution_id", "number", name="uq_attempts_execution_number"),
    )
    op.create_index("ix_attempts_state", "attempts", ["state"])
    op.create_index("ix_attempts_task", "attempts", ["task_id"])
    op.create_table(
        "events",
        sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("execution_id", ID, sa.ForeignKey("executions.id"), nullable=True),
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=True),
        sa.Column("principal", sa.String(160), nullable=False),
        sa.Column("verified", sa.Boolean, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in EVENT_KINDS) + ")", name="ck_events_kind"
        ),
    )
    op.create_index("ix_events_task_seq", "events", ["task_id", "seq"])
    op.create_table(
        "leases",
        sa.Column("id", ID, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("holder", sa.String(128), nullable=False),
        sa.Column("fenced_token", sa.BigInteger, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.UniqueConstraint("kind", "key", name="uq_leases_kind_key"),
    )
    op.create_index("ix_leases_expires", "leases", ["expires_at"])
    op.create_table(
        "completion_claims",
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), primary_key=True),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("parsed_ok", sa.Boolean, nullable=False),
        sa.Column("parse_errors", JSONB, nullable=False),
    )
    op.create_table(
        "supervisor_status",
        sa.Column("singleton", sa.Boolean, primary_key=True),
        sa.Column("holder", sa.String(128), nullable=True),
        sa.Column("last_tick_at", TZ, nullable=True),
        sa.Column("tick_ms", sa.Integer, nullable=True),
        sa.Column("counts", JSONB, nullable=False),
        sa.CheckConstraint("singleton", name="ck_supervisor_status_singleton"),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("id", ID, primary_key=True),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer, nullable=False),
        sa.Column("response_body", JSONB, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.UniqueConstraint("principal_id", "key", name="uq_idempotency_principal_key"),
    )

    op.execute(REJECT_MUTATION_FN)
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION crucible_reject_mutation();"
        )
    op.execute(FENCED_TOKEN_FN)
    for table in FENCED_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_fenced BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION crucible_check_fenced_token();"
        )

    op.execute(
        sa.text(
            "INSERT INTO policies (name, version, document, created_at) "
            "VALUES (:name, :version, CAST(:document AS jsonb), now())"
        ).bindparams(name="default-software", version=1, document=json.dumps(DEFAULT_POLICY))
    )


def downgrade() -> None:
    for table in FENCED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_fenced ON {table};")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    op.execute("DROP FUNCTION IF EXISTS crucible_check_fenced_token();")
    op.execute("DROP FUNCTION IF EXISTS crucible_reject_mutation();")
    for table in (
        "idempotency_keys",
        "supervisor_status",
        "completion_claims",
        "leases",
        "events",
        "attempts",
        "executions",
        "task_contracts",
        "tasks",
        "policies",
        "repositories",
        "principals",
    ):
        op.drop_table(table)
