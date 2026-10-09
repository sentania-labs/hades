"""Regression coverage for main at 876bbf5e, after #626 (device tokens, hades #576 U9, and
usage per attempt, hades #604).

The test and e2e jobs went red on that commit while the unit, integration and shell tiers
pass on the same tree. These cases pin, without a database or Docker, the seams of #626
that only the integration and e2e tiers otherwise reach: the migration is the one head,
its CHECK list carries every event kind the domain can append, the ORM and the migration
agree on the new table and column, and both test tiers clear the new table between
cases."""

from __future__ import annotations

import re
from pathlib import Path

from alembic.script import ScriptDirectory

from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.models import AttemptMetricsRow, Base, DeviceRow
from crucible.domain.events import EventKind
from crucible.ports.harness import ReportMetrics
from tests.fixtures import migration_by_slug

REPO = Path(__file__).resolve().parents[2]
DEVICE_KINDS = {"device_token_minted", "device_token_used", "device_token_revoked"}


def _truncated_tables(conftest: Path) -> set[str]:
    source = conftest.read_text(encoding="utf-8")
    match = re.search(r"^TRUNCATE = \((.*?)^\)", source, re.DOTALL | re.MULTILINE)
    assert match, f"no TRUNCATE in {conftest}"
    statement = "".join(re.findall(r'"([^"]*)"', match.group(1)))
    body = statement.removeprefix("TRUNCATE ").split(" RESTART")[0]
    return {name.strip() for name in body.split(",") if name.strip()}


def test_the_device_tokens_migration_is_the_single_head_above_comment_delivery() -> None:
    script = ScriptDirectory.from_config(migrate.alembic_config("postgresql://unused/unused"))
    device = migration_by_slug("device_tokens_usage")
    assert script.get_heads() == [device.revision]
    assert device.down_revision == migration_by_slug("comment_delivery").revision


def test_the_check_constraint_permits_every_event_kind_the_domain_appends() -> None:
    """An event kind the CHECK refuses fails only against Postgres, as an IntegrityError
    on the first append of it; the head's list is the whole domain's."""
    kinds = set(migration_by_slug("device_tokens_usage").module._event_kinds())
    assert kinds >= DEVICE_KINDS
    assert {kind.value for kind in EventKind} <= kinds


def test_the_orm_matches_the_migration_for_devices_and_cache_reads() -> None:
    devices = Base.metadata.tables["devices"]
    assert DeviceRow.__table__ is devices
    assert {column.name for column in devices.columns} == {
        "principal_id",
        "name",
        "created_by",
        "created_at",
        "last_used_at",
        "last_user_agent",
        "exchanged_at",
        "revoked_at",
        "revoked_by",
    }
    (key,) = devices.c.principal_id.foreign_keys
    assert key.target_fullname == "principals.id"
    assert "tokens_cache_read" in AttemptMetricsRow.__table__.columns
    assert ReportMetrics().as_dict()["tokens_cache_read"] is None


def test_both_database_tiers_clear_devices_between_cases() -> None:
    """devices references principals: a tier truncating principals must take devices too,
    by name or by CASCADE, or its next case fails on the foreign key."""
    integration = _truncated_tables(REPO / "tests/integration/conftest.py")
    assert "devices" in integration
    e2e_source = (REPO / "tests/e2e/conftest.py").read_text(encoding="utf-8")
    e2e = _truncated_tables(REPO / "tests/e2e/conftest.py")
    assert "devices" in e2e or ("principals" in e2e and "CASCADE" in e2e_source)
