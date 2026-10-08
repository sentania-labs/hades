"""The migration graph has exactly one head (FDY-0385).

Parallel branches that each number a revision the same way merge cleanly in git and
leave alembic with several heads, which only surfaces as MultipleHeads when a stack
starts. This test reads the graph without a database, so the next collision fails the
unit tier instead. The fix is a merge revision whose down_revision is every head."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.migrations.versions import _0039_auto_merge_refusals
from crucible.adapters.persistence.migrations.versions import (
    _0043_credential_mount_mode as credential_mount_mode,
)
from crucible.adapters.persistence.migrations.versions import _0043_proposed_tasks as proposed_tasks
from crucible.adapters.persistence.migrations.versions import (
    _0044_merge_runtime_settings_proposals as merge_423_424,
)

REPO = Path(__file__).resolve().parents[2]
MERGE = "0045_merge_0044_heads"
MERGED = {"0044_attempt_stall_shape", "0044_editor_leftovers_policy", "0044_merge_423_424"}
# The single head after the merge. hades #393 added 0046 above it and hades #425 added
# 0047 above that; hades #389's migration was renumbered to 0047 on top and chains from
# 0047_attempt_egress_probe so the graph stays linear. hades #176 adds 0048 on top,
# hades #265 adds 0049 for persisted batch outcomes, then #485 adds 0050 for the cache TTL,
# #437 adds 0051 for routing model references, and #343 adds 0052 for the repository's
# own Codex connector refusal. #476's correction adds 0053 for the certification's
# change class, and #489 adds 0054_task_notes for operator notes on a task.
ABOVE = "0046_blocked_reason"
PROBE = "0047_attempt_egress_probe"
LAUNCH = "0047_successful_launch_time"
REBOUND = "0048_repository_rebound"
BATCH = "0049_repository_batch"
CACHE = "0050_status_cache"
ROUTING_REFS = "0051_routing_model_references"
REFUSED = "0052_codex_review_refused"
CERT = "0053_cert_change_class"
HEAD = "0054_task_notes"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(migrate.alembic_config("postgresql://unused/unused"))


def test_the_migration_graph_has_a_single_head() -> None:
    heads = _script().get_heads()
    assert len(heads) == 1, (
        f"alembic has {len(heads)} heads {sorted(heads)}; add a merge revision whose "
        "down_revision is the tuple of them"
    )


def test_the_0045_merge_joins_the_three_0044_heads() -> None:
    script = _script()
    merge = script.get_revision(MERGE)
    assert merge is not None
    assert set(merge.down_revision or ()) == MERGED
    assert script.get_current_head() == HEAD
    above = script.get_revision(ABOVE)
    assert above is not None and above.down_revision == MERGE
    probe = script.get_revision(PROBE)
    assert probe is not None and probe.down_revision == ABOVE
    launch = script.get_revision(LAUNCH)
    assert launch is not None and launch.down_revision == PROBE
    rebound = script.get_revision(REBOUND)
    assert rebound is not None and rebound.down_revision == LAUNCH
    batch = script.get_revision(BATCH)
    assert batch is not None and batch.down_revision == REBOUND
    cache = script.get_revision(CACHE)
    assert cache is not None and cache.down_revision == BATCH
    routing_refs = script.get_revision(ROUTING_REFS)
    assert routing_refs is not None and routing_refs.down_revision == CACHE
    refused = script.get_revision(REFUSED)
    assert refused is not None and refused.down_revision == ROUTING_REFS
    cert = script.get_revision(CERT)
    assert cert is not None and cert.down_revision == REFUSED
    head = script.get_revision(HEAD)
    assert head is not None and head.down_revision == CERT


def test_the_cli_config_sees_the_same_single_head() -> None:
    """`uv run alembic heads` reads alembic.ini at the repository root."""
    cfg = Config(str(REPO / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    assert Path(script.dir).resolve() == migrate.MIGRATIONS_DIR.resolve()
    assert script.get_heads() == [HEAD]


def _postgres_renders(kinds: list[str]) -> str:
    """`pg_get_constraintdef` for a `kind IN (...)` CHECK on a varchar column."""
    literals = ", ".join(f"'{kind}'::character varying" for kind in kinds)
    return f"CHECK (((kind)::text = ANY ((ARRAY[{literals}])::text[])))"


def test_the_path_from_each_proposal_head_runs_0043_credential_mount_mode() -> None:
    """A database on 0044_attempt_stall_shape or 0044_editor_leftovers_policy applied
    0043_proposed_tasks and not its sibling, so its way to 0045 runs
    0043_credential_mount_mode before 0044_merge_423_424 restores the union of kinds."""
    script = _script()
    for head in ("0044_attempt_stall_shape", "0044_editor_leftovers_policy"):
        # The steps `alembic upgrade head` runs from `head`, in order; iterate_revisions
        # would leave out the sibling branch, which is the whole point here.
        plan = [step.revision.revision for step in script._upgrade_revs("head", head)]
        assert plan.index("0043_credential_mount_mode") < plan.index("0044_merge_423_424")
        assert plan.index("0044_merge_423_424") < plan.index(MERGE)
        assert plan[-14:] == [
            "0043_credential_mount_mode",
            "0044_merge_423_424",
            ("0044_editor_leftovers_policy", "0044_attempt_stall_shape")[
                head == "0044_editor_leftovers_policy"
            ],
            MERGE,
            ABOVE,
            PROBE,
            LAUNCH,
            REBOUND,
            BATCH,
            CACHE,
            ROUTING_REFS,
            REFUSED,
            CERT,
            HEAD,
        ]


def test_0043_credential_mount_mode_keeps_the_kinds_the_live_check_permits() -> None:
    """On that path the events table may hold proposal rows. The rebuilt CHECK keeps the
    proposal kinds the live constraint permits, so PostgreSQL accepts it over those rows,
    and the result is the union 0044_merge_423_424 settles."""
    live = credential_mount_mode._kinds_in(_postgres_renders(proposed_tasks._event_kinds()))
    assert live == proposed_tasks._event_kinds()
    kinds = credential_mount_mode._kinds_to_permit(live)
    assert set(proposed_tasks.EVENT_KINDS) <= set(kinds)
    assert set(credential_mount_mode.EVENT_KINDS) <= set(kinds)
    assert set(kinds) == set(merge_423_424._event_kinds())
    assert len(kinds) == len(set(kinds))


def test_0043_credential_mount_mode_is_unchanged_where_only_0042_was_applied() -> None:
    live = credential_mount_mode._kinds_in(
        _postgres_renders(_0039_auto_merge_refusals._event_kinds())
    )
    assert credential_mount_mode._kinds_to_permit(live) == credential_mount_mode._event_kinds()
    assert credential_mount_mode._kinds_in(None) == []
