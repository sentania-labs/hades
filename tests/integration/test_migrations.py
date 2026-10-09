from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, inspect, text

from crucible.adapters.harness.registry import default_registry
from crucible.adapters.persistence import migrate
from crucible.adapters.persistence.migrations.versions import (
    _0051_routing_model_references as m51,
)
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.application.routing import load_routing
from crucible.contracts.policy import RoutingPolicyV1
from crucible.ports.harness import HarnessGate, HarnessUnavailableError
from tests.fixtures import contract_document
from tests.integration.conftest import rebuild, reset, submit_and_start

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def at_clean_head(migrated: str) -> Iterator[None]:
    """Most tests here take the database straight, not through the `engine` fixture, and
    leave rows and revisions behind that break the next one's downgrade. Each starts and
    ends at head with every table reset, so none depends on the one before it (issue 195)."""

    def clean() -> None:
        migrate.upgrade(migrated)
        engine = make_engine(migrated)
        try:
            reset(engine)
        finally:
            engine.dispose()

    clean()
    yield
    try:
        clean()
    except Exception:
        # A test that failed halfway down can leave a revision `upgrade` cannot leave.
        # Rebuild, so one real failure is one failure and not every test after it.
        rebuild(migrated)
        clean()


def test_up_down_up_from_empty(database_url: str) -> None:
    migrate.downgrade(database_url, "base")
    engine = make_engine(database_url)
    assert (
        inspect(engine).get_table_names() == ["alembic_version"]
        or "tasks" not in inspect(engine).get_table_names()
    )
    migrate.upgrade(database_url)
    names = set(inspect(engine).get_table_names())
    assert {
        "principals",
        "repositories",
        "policies",
        "tasks",
        "task_contracts",
        "executions",
        "attempts",
        "events",
        "leases",
        "supervisor_status",
        "completion_claims",
        "heartbeats",
    } <= names
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    with engine.connect() as conn:
        # C10 adds immutable version 6 and intentionally does not rewrite older rows;
        # C11 adds the next version for Opus 5.5.
        assert conn.execute(text("SELECT count(*) FROM policies")).scalar() == 6
    migrate.downgrade(database_url, "base")
    assert "tasks" not in inspect(engine).get_table_names()
    migrate.upgrade(database_url)
    engine.dispose()


def test_events_and_contracts_are_append_only(migrated: str) -> None:
    engine = make_engine(migrated)
    marker = "append-only-probe"
    with engine.begin() as conn:
        before = conn.execute(text("SELECT count(*) FROM events")).scalar()
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'principal_created', :marker, true, '{}')"
            ),
            {"marker": marker},
        )
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("UPDATE events SET kind = 'task_submitted'"))
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("DELETE FROM events"))
    with engine.connect() as conn:
        # The refusals changed nothing, and the row this test wrote is still there. The
        # table is not asserted empty: a downgrade past C4 now preserves its events
        # rather than deleting them, so an earlier test can legitimately leave rows.
        assert conn.execute(text("SELECT count(*) FROM events")).scalar() == (before or 0) + 1
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE principal = :marker"), {"marker": marker}
            ).scalar()
            == 1
        )
    engine.dispose()


def test_unknown_event_kind_is_rejected(migrated: str) -> None:
    engine = make_engine(migrated)
    with engine.begin() as conn, pytest.raises(Exception, match="ck_events_kind"):
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'made_up_kind', 'tests', true, '{}')"
            )
        )
    engine.dispose()


def test_0058_creates_memory_and_the_ledger_on_a_populated_database(database_url: str) -> None:
    """hades #208, AC1: the revision applies over rows that are already there, both tables
    appear with no drift, the ledger refuses an edit and a delete, and the way down and
    back up keeps the new event kinds the way 0007 taught."""
    migrate.downgrade(database_url, "0056_pull_request_schema_overlap")
    engine = make_engine(database_url)
    marker = "hades-208-populated"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'principal_created', :marker, true, '{}')"
            ),
            {"marker": marker},
        )
        names = set(inspect(conn).get_table_names())
    assert "memory_items" not in names and "decision_ledger" not in names
    migrate.upgrade(database_url)
    with engine.begin() as conn:
        names = set(inspect(conn).get_table_names())
        assert {"memory_items", "decision_ledger"} <= names
        columns = {c["name"] for c in inspect(conn).get_columns("memory_items")}
        assert columns == {
            "id",
            "text",
            "source",
            "observed_at",
            "scope_tags",
            "promoted_by",
            "promoted_at",
            "superseded_by",
            "superseded_at",
        }
        columns = {c["name"] for c in inspect(conn).get_columns("decision_ledger")}
        assert columns == {
            "id",
            "principal",
            "channel",
            "said_at",
            "verbatim",
            "transcript_ref",
            "applies_to",
            "acted_by",
            "acted_at",
        }
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE principal = :marker"), {"marker": marker}
            ).scalar()
            == 1
        )
        conn.execute(
            text(
                "INSERT INTO memory_items (id, text, source, observed_at, scope_tags, "
                "promoted_by, promoted_at) VALUES ('01MEMORY0580000000000000A', 'The lab "
                "cluster has one node.', 'operator', now(), ARRAY['hades'], 'scott', now())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO decision_ledger (id, principal, channel, said_at, verbatim, "
                "applies_to) VALUES ('01LEDGER0580000000000000A', 'scott', 'telegram', now(), "
                "'Build the mvp', ARRAY['FDY-0587'])"
            )
        )
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'memory_promoted', :marker, true, '{}')"
            ),
            {"marker": marker},
        )
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("UPDATE decision_ledger SET verbatim = 'edited'"))
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("DELETE FROM decision_ledger"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM decision_ledger")).scalar() == 1
        # Memory is edited by superseding, which is an update of the old row's pointer.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE memory_items SET superseded_at = now() "
                "WHERE id = '01MEMORY0580000000000000A'"
            )
        )
    assert migrate.schema_drift(engine) is None
    migrate.downgrade(database_url, "0056_pull_request_schema_overlap")
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        assert "memory_items" not in names and "decision_ledger" not in names
        assert "events_0058_archive" in names
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE kind = 'memory_promoted'")
            ).scalar()
            == 0
        )
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        assert "events_0058_archive" not in set(inspect(conn).get_table_names())
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE kind = 'memory_promoted'")
            ).scalar()
            == 1
        )
    engine.dispose()


def test_fresh_schema_has_no_drift(migrated: str) -> None:
    engine = make_engine(migrated)
    assert migrate.schema_drift(engine) is None
    ok, detail = migrate.is_current(engine, migrated)
    assert ok and "schema matches" in detail
    engine.dispose()


def test_0004_creates_the_c2_tables_and_seeds_the_routing_policy(migrated: str) -> None:
    engine = make_engine(migrated)
    names = set(inspect(engine).get_table_names())
    assert {
        "routing_policies",
        "artifacts",
        "evidence",
        "review_reports",
        "gate_results",
        "acceptance_results",
        "escalations",
        "decisions",
        "review_dispositions",
        "wakes",
        "attempt_metrics",
    } <= names
    with engine.connect() as conn:
        # Older migrations always seed 1 through 4. The optional old endpoint seed may
        # add 5. C10 takes the next free number for the lab-local route, and C11 takes
        # the next free number again for Opus 5.5, each leaving every prior document
        # intact.
        versions = (
            conn.execute(
                text(
                    "SELECT version FROM routing_policies WHERE name = 'default-routing' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
        assert versions == list(range(1, max(versions) + 1))
        assert max(versions) in (6, 7)
        seeded = conn.execute(
            text(
                "SELECT count(*) FROM routing_policies WHERE name='default-routing' "
                "AND EXISTS (SELECT 1 FROM jsonb_array_elements(document -> 'models') AS model "
                "WHERE model ->> 'disabled_reason' LIKE '%0017_lab_local%')"
            )
        ).scalar_one()
        # Every later migration that copies the routing document forward without
        # touching this model keeps the marker, so this only checks it was seeded.
        assert seeded >= 1
        # Later revisions add immutable policy versions that name their matching
        # routing version (05b).
        policy_versions = conn.execute(
            text(
                "SELECT version, document -> 'routing' -> 'policy' ->> 'version' "
                "FROM policies WHERE name = 'default-software' ORDER BY 1"
            )
        ).all()
        assert [(v, int(r)) for v, r in policy_versions] == [
            (version, version) for version in versions
        ]
        routing = conn.execute(
            text(
                "SELECT document -> 'routing' FROM policies "
                "WHERE name = 'default-software' AND version = 1"
            )
        ).scalar()
    assert routing == {"policy": {"name": "default-routing", "version": 1}}
    engine.dispose()


def test_0013_records_an_unconfigured_spark_route_with_a_reason(migrated: str) -> None:
    engine = make_engine(migrated)
    with engine.connect() as conn:
        document = conn.execute(
            text("SELECT document FROM routing_policies WHERE name='default-routing' AND version=4")
        ).scalar_one()
        assert (
            conn.execute(text("SELECT count(*) FROM harnesses WHERE name='hermes'")).scalar() == 1
        )
    routing = RoutingPolicyV1.model_validate(document)
    hermes = routing.model("gpt-oss:120b", "hermes")
    assert hermes is not None and not hermes.enabled and hermes.endpoint_url is None
    assert hermes.disabled_reason == "CRUCIBLE_SPARK_ENDPOINT_URL is not configured"
    assert routing.pools["spark-local"].max_concurrency == 4
    engine.dispose()


def test_0013_materializes_the_configured_spark_url(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate.downgrade(database_url, "0012_heartbeats")
    monkeypatch.setenv("CRUCIBLE_SPARK_ENDPOINT_URL", "http://192.0.2.41:11434/v1")
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.connect() as conn:
        document = conn.execute(
            text("SELECT document FROM routing_policies WHERE name='default-routing' AND version=4")
        ).scalar_one()
    hermes = RoutingPolicyV1.model_validate(document).model("gpt-oss:120b", "hermes")
    assert hermes is not None and hermes.endpoint_url == "http://192.0.2.41:11434/v1"
    assert hermes.disabled_reason == "enablement gate has not passed"
    with engine.connect() as conn:
        enabled_document = conn.execute(
            text("SELECT document FROM routing_policies WHERE name='default-routing' AND version=5")
        ).scalar_one()
        policy_ref = conn.execute(
            text(
                "SELECT document -> 'routing' -> 'policy' ->> 'version' FROM policies "
                "WHERE name='default-software' AND version=5"
            )
        ).scalar_one()
    enabled = RoutingPolicyV1.model_validate(enabled_document).model("gpt-oss:120b", "hermes")
    assert enabled is not None and enabled.enabled and enabled.disabled_reason is None
    assert enabled.endpoint_url == "http://192.0.2.41:11434/v1"
    assert int(policy_ref) == 5
    engine.dispose()


def test_0017_replaces_the_spark_pin_with_the_disabled_coder_route(migrated: str) -> None:
    engine = make_engine(migrated)
    with engine.connect() as conn:
        seeded_policy = conn.execute(
            text(
                "SELECT document FROM policies WHERE name='default-software' "
                "AND document ->> 'description' = "
                "'Authenticated Hermes lab-local route seeded by 0017_lab_local.'"
            )
        ).scalar_one()
        routing_version = int(seeded_policy["routing"]["policy"]["version"])
        document = conn.execute(
            text(
                "SELECT document FROM routing_policies "
                "WHERE name='default-routing' AND version=:version"
            ),
            {"version": routing_version},
        ).scalar_one()
    routing = RoutingPolicyV1.model_validate(document)
    local = [model for model in routing.models if model.harness == "hermes"]
    assert [model.id for model in local] == ["coder"]
    assert not local[0].enabled
    assert local[0].chat_template_kwargs.enable_thinking is False
    assert routing.pools["lab-local"].max_concurrency == 4
    assert "spark-local" not in routing.pools
    assert routing.version == routing_version
    engine.dispose()


def test_0017_preserves_an_operator_created_version_six(database_url: str) -> None:
    migrate.downgrade(database_url, "base")
    migrate.upgrade(database_url, "0016_disposition_versions")
    engine = make_engine(database_url)
    with engine.begin() as conn:
        routing = conn.execute(
            text(
                "SELECT document FROM routing_policies WHERE name='default-routing' "
                "ORDER BY version DESC LIMIT 1"
            )
        ).scalar_one()
        policy = conn.execute(
            text(
                "SELECT document FROM policies WHERE name='default-software' "
                "ORDER BY version DESC LIMIT 1"
            )
        ).scalar_one()
        routing["version"] = 6
        policy["version"] = 6
        policy["description"] = "operator-created version six"
        policy["routing"] = {"policy": {"name": "default-routing", "version": 6}}
        conn.execute(
            text(
                "INSERT INTO routing_policies(name, version, document, created_at) "
                "VALUES ('default-routing', 6, CAST(:document AS jsonb), now())"
            ),
            {"document": json.dumps(routing)},
        )
        conn.execute(
            text(
                "INSERT INTO policies(name, version, document, created_at) "
                "VALUES ('default-software', 6, CAST(:document AS jsonb), now())"
            ),
            {"document": json.dumps(policy)},
        )
    engine.dispose()

    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT document ->> 'description' FROM policies "
                    "WHERE name='default-software' AND version=6"
                )
            ).scalar_one()
            == "operator-created version six"
        )
        assert (
            conn.execute(
                text(
                    "SELECT document -> 'routing' -> 'policy' ->> 'version' FROM policies "
                    "WHERE name='default-software' AND version=7"
                )
            ).scalar_one()
            == "7"
        )
    engine.dispose()

    migrate.downgrade(database_url, "0016_disposition_versions")
    engine = make_engine(database_url)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM policies WHERE name='default-software' AND version=6")
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM policies WHERE name='default-software' AND version=7")
            ).scalar_one()
            == 0
        )
    engine.dispose()
    migrate.upgrade(database_url)


def test_0017_seeds_from_the_routing_in_force_not_an_unreferenced_draft(
    database_url: str,
) -> None:
    """An uploaded routing draft with a higher version, not named by default-software,
    must not become what the migrated default points at."""
    migrate.downgrade(database_url, "base")
    migrate.upgrade(database_url, "0016_disposition_versions")
    engine = make_engine(database_url)
    with engine.begin() as conn:
        policy = conn.execute(
            text(
                "SELECT document FROM policies WHERE name='default-software' "
                "ORDER BY version DESC LIMIT 1"
            )
        ).scalar_one()
        in_force = int(policy["routing"]["policy"]["version"])
        routing = conn.execute(
            text(
                "SELECT document FROM routing_policies "
                "WHERE name='default-routing' AND version=:version"
            ),
            {"version": in_force},
        ).scalar_one()
        draft_version = (
            int(
                conn.execute(
                    text("SELECT max(version) FROM routing_policies WHERE name='default-routing'")
                ).scalar_one()
            )
            + 1
        )
        draft = json.loads(json.dumps(routing))
        draft["version"] = draft_version
        experiment = {
            **next(model for model in draft["models"] if model.get("harness") != "hermes"),
            "id": "draft-only-experiment",
            "enabled": True,
            "disabled_reason": None,
        }
        draft["models"].append(experiment)
        conn.execute(
            text(
                "INSERT INTO routing_policies(name, version, document, created_at) "
                "VALUES ('default-routing', :version, CAST(:document AS jsonb), now())"
            ),
            {"version": draft_version, "document": json.dumps(draft)},
        )
    engine.dispose()

    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.connect() as conn:
        seeded_policy = conn.execute(
            text(
                "SELECT document FROM policies WHERE name='default-software' "
                "ORDER BY version DESC LIMIT 1"
            )
        ).scalar_one()
        seeded_version = int(seeded_policy["routing"]["policy"]["version"])
        seeded = conn.execute(
            text(
                "SELECT document FROM routing_policies "
                "WHERE name='default-routing' AND version=:version"
            ),
            {"version": seeded_version},
        ).scalar_one()
        kept_draft = conn.execute(
            text(
                "SELECT document FROM routing_policies "
                "WHERE name='default-routing' AND version=:version"
            ),
            {"version": draft_version},
        ).scalar_one()
    engine.dispose()
    # 0017 mints one new version on top of the draft, then C11's 0019 mints another
    # beside it, so the in-force chain now runs two steps past the draft, not one.
    assert seeded_version == draft_version + 2
    # At head, 0051 has made each entry a (harness, model) reference.
    ids = [model["model"] for model in seeded["models"]]
    assert "draft-only-experiment" not in ids
    assert [
        model
        for model in seeded["models"]
        if model.get("harness") != "hermes" and model["model"] != "claude-opus-5-5"
    ] == [model for model in m51._current(routing)["models"] if model.get("harness") != "hermes"]
    assert kept_draft == m51._current(draft)

    migrate.downgrade(database_url, "0016_disposition_versions")
    # The draft this test inserted by hand is unreferenced, so no downgrade removes
    # it either: left alone it would occupy a version number forever and throw off
    # every later test's "next free version" math in the shared database.
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM routing_policies WHERE name='default-routing' AND version=:version"),
            {"version": draft_version},
        )
    engine.dispose()
    migrate.upgrade(database_url)


def test_0017_extends_a_routing_policy_the_operator_named_differently(
    database_url: str,
) -> None:
    """The policy in force may name a routing policy uploaded under another name. The
    migration extends that one, under its own name, and its downgrade removes it."""
    migrate.downgrade(database_url, "base")
    migrate.upgrade(database_url, "0016_disposition_versions")
    engine = make_engine(database_url)
    with engine.begin() as conn:
        policy = conn.execute(
            text(
                "SELECT document FROM policies WHERE name='default-software' "
                "ORDER BY version DESC LIMIT 1"
            )
        ).scalar_one()
        routing = conn.execute(
            text(
                "SELECT document FROM routing_policies "
                "WHERE name='default-routing' AND version=:version"
            ),
            {"version": int(policy["routing"]["policy"]["version"])},
        ).scalar_one()
        routing["version"] = 1
        conn.execute(
            text(
                "INSERT INTO routing_policies(name, version, document, created_at) "
                "VALUES ('lab-routing', 1, CAST(:document AS jsonb), now())"
            ),
            {"document": json.dumps(routing)},
        )
        policy["version"] = int(policy["version"]) + 1
        policy["routing"] = {"policy": {"name": "lab-routing", "version": 1}}
        conn.execute(
            text(
                "INSERT INTO policies(name, version, document, created_at) "
                "VALUES ('default-software', :version, CAST(:document AS jsonb), now())"
            ),
            {"version": policy["version"], "document": json.dumps(policy)},
        )
        default_routing_rows = conn.execute(
            text("SELECT count(*) FROM routing_policies WHERE name='default-routing'")
        ).scalar_one()
    engine.dispose()

    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.connect() as conn:
        seeded_policy = conn.execute(
            text(
                "SELECT document FROM policies WHERE name='default-software' "
                "ORDER BY version DESC LIMIT 1"
            )
        ).scalar_one()
        seeded = conn.execute(
            text("SELECT document FROM routing_policies WHERE name='lab-routing' AND version=2")
        ).scalar_one()
        assert (
            conn.execute(
                text("SELECT count(*) FROM routing_policies WHERE name='default-routing'")
            ).scalar_one()
            == default_routing_rows
        )
    engine.dispose()
    # 0017 extends lab-routing to version 2; C11's 0019 then extends it again to 3,
    # and that is what the in-force policy now names.
    assert seeded_policy["routing"] == {"policy": {"name": "lab-routing", "version": 3}}
    assert [model["model"] for model in seeded["models"] if model["harness"] == "hermes"] == [
        "coder"
    ]

    migrate.downgrade(database_url, "0016_disposition_versions")
    engine = make_engine(database_url)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT max(version) FROM routing_policies WHERE name='lab-routing'")
            ).scalar_one()
            == 1
        )
    # The version this test inserted by hand, not through a migration: no downgrade
    # knows to remove it, so it would otherwise stay in force for every test after
    # this one in the shared database.
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM policies WHERE name='default-software' "
                "AND document -> 'routing' -> 'policy' ->> 'name' = 'lab-routing'"
            )
        )
        conn.execute(text("DELETE FROM routing_policies WHERE name='lab-routing'"))
    engine.dispose()
    migrate.upgrade(database_url)


def _active(conn: Connection) -> tuple[int, dict[str, Any], int, dict[str, Any]]:
    """The delivery policy in force and the routing version it names."""
    policy = conn.execute(
        text(
            "SELECT version, document FROM policies WHERE name='default-software' "
            "AND retired_at IS NULL ORDER BY version DESC LIMIT 1"
        )
    ).one()
    reference = policy.document["routing"]["policy"]
    routing = conn.execute(
        text("SELECT document FROM routing_policies WHERE name=:name AND version=:version"),
        {"name": reference["name"], "version": reference["version"]},
    ).scalar_one()
    return policy.version, policy.document, int(reference["version"]), routing


def test_0019_adds_opus_5_5_disabled_beside_the_frontier_entry(database_url: str) -> None:
    """C11: the id from Claude Code's own model catalog, in the Claude Code pool at the
    frontier tier, disabled until the operator enables it, in new immutable versions
    at the next free numbers (the admin UI mints versions too), which the down
    migration removes again."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0018_combined_worker_image")
    with engine.connect() as conn:
        before_policy, _, before_routing, source = _active(conn)
        highest = conn.execute(text("SELECT max(version) FROM routing_policies")).scalar_one()
        highest_policy = conn.execute(text("SELECT max(version) FROM policies")).scalar_one()
    assert not any(model["id"] == "claude-opus-5-5" for model in source["models"])
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        policy_version, policy, routing_version, document = _active(conn)
    assert (policy_version, routing_version) == (highest_policy + 1, highest + 1)
    assert "Opus 5.5" in policy["description"]
    routing = RoutingPolicyV1.model_validate(document)
    opus = routing.model("claude-opus-5-5", "claude_code")
    assert opus is not None
    assert (opus.harness, opus.endpoint, opus.capability, opus.pool) == (
        "claude_code",
        "subscription",
        "frontier",
        "anthropic-sub",
    )
    assert not opus.enabled and opus.disabled_reason
    ids = [model.id for model in routing.models]
    assert ids.index("claude-opus-5-5") == ids.index("claude-fable-5-1") + 1
    # Everything else is the routing version it was copied from, unchanged.
    # At head, 0051 has made each entry a (harness, model) reference.
    assert [m for m in document["models"] if m["model"] != "claude-opus-5-5"] == m51._current(
        source
    )["models"]
    migrate.downgrade(database_url, "0018_combined_worker_image")
    with engine.connect() as conn:
        assert _active(conn)[0] == before_policy and _active(conn)[2] == before_routing
    migrate.upgrade(database_url)
    engine.dispose()


def test_0019_downgrade_keeps_the_version_a_task_was_submitted_against(
    client: TestClient, migrated: str
) -> None:
    """A rollback after a submission must not strand the task: the policy version 0019
    wrote is retired, not deleted, and its routing version stays, so the task still
    resolves both, while the version before 0019 is back in force. A second round
    removes only the version the re-upgrade wrote."""
    engine = make_engine(migrated)
    with engine.connect() as conn:
        version, _, routing_version, _ = _active(conn)
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-succeed",
        "MIG-0019",
        start=False,
        policy={"name": "default-software", "version": version},
    )
    try:
        migrate.downgrade(migrated, "0018_combined_worker_image")
        with engine.connect() as conn:
            assert _active(conn)[0] < version
            retired = conn.execute(
                text(
                    "SELECT retired_at FROM policies "
                    "WHERE name='default-software' AND version=:version"
                ),
                {"version": version},
            ).scalar_one()
            assert retired is not None
        with SqlUnitOfWorkFactory(engine)() as uow:
            task = uow.tasks.get(task_id)
            assert task is not None and task.policy_version == version
            policy = uow.policies.get(task.policy_name, task.policy_version)
            assert policy is not None
            routing = load_routing(uow, policy.document)
            assert routing is not None and routing.version == routing_version
            assert routing.model("claude-opus-5-5", "claude_code") is not None
        migrate.upgrade(migrated)
        # Through the API only once the schema matches the code again: this code reads
        # columns later revisions add (0025's `repositories.private`), and running it
        # against a downgraded schema is the drift readiness refuses, not a rollback.
        assert client.get(f"/v1/tasks/{task_id}").status_code == 200
        with engine.connect() as conn:
            again, _, routing_again, _ = _active(conn)
        assert again > version and routing_again > routing_version
        migrate.downgrade(migrated, "0018_combined_worker_image")
        with engine.connect() as conn:
            assert _active(conn)[0] < version
            versions = {
                row.version: row.retired_at
                for row in conn.execute(
                    text("SELECT version, retired_at FROM policies WHERE name='default-software'")
                )
            }
            routings = set(
                conn.execute(
                    text("SELECT version FROM routing_policies WHERE name=:name"),
                    {"name": policy.document["routing"]["policy"]["name"]},
                ).scalars()
            )
        assert again not in versions
        assert versions[version] is not None
        assert routing_again not in routings and routing_version in routings
    finally:
        migrate.upgrade(migrated)
        engine.dispose()


def test_0019_downgrade_leaves_an_operator_copy_that_enabled_opus_alone(
    database_url: str,
) -> None:
    """A copy the operator uploaded keeps 0019's description, marker included. Once an
    earlier downgrade has removed 0019's own version, the copy is the lowest marked one
    left; if it names a routing version where the operator enabled Opus 5.5, it is theirs
    and a second downgrade leaves it, and that routing version, in force."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.begin() as conn:
        version, policy, routing_version, routing = _active(conn)
        name = policy["routing"]["policy"]["name"]
        enabled = json.loads(json.dumps(routing))
        for model in enabled["models"]:
            if model["model"] == "claude-opus-5-5":
                model["enabled"] = True
                model.pop("disabled_reason", None)
        operator_routing = routing_version + 1
        enabled["version"] = operator_routing
        conn.execute(
            text(
                "INSERT INTO routing_policies(name, version, document, created_at) "
                "VALUES (:name, :version, CAST(:document AS jsonb), now())"
            ),
            {"name": name, "version": operator_routing, "document": json.dumps(enabled)},
        )
        copy = json.loads(json.dumps(policy))
        copy["version"] = version + 1
        copy["routing"] = {"policy": {"name": name, "version": operator_routing}}
        conn.execute(
            text(
                "INSERT INTO policies(name, version, document, created_at) "
                "VALUES ('default-software', :version, CAST(:document AS jsonb), now())"
            ),
            {"version": version + 1, "document": json.dumps(copy)},
        )
    try:
        migrate.downgrade(database_url, "0018_combined_worker_image")
        with engine.connect() as conn:
            assert _active(conn)[:3:2] == (version + 1, operator_routing)
        # The upgrade finds Opus 5.5 already in the routing in force and writes nothing.
        migrate.upgrade(database_url)
        migrate.downgrade(database_url, "0018_combined_worker_image")
        with engine.connect() as conn:
            assert _active(conn)[:3:2] == (version + 1, operator_routing)
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM policies WHERE name='default-software' AND version=:version"),
                {"version": version + 1},
            )
            conn.execute(
                text("DELETE FROM routing_policies WHERE name=:name AND version=:version"),
                {"name": name, "version": operator_routing},
            )
        migrate.downgrade(database_url, "0018_combined_worker_image")
        migrate.upgrade(database_url)
        engine.dispose()


def test_0018_keeps_a_promotion_across_down_and_up(database_url: str) -> None:
    """C11: a promotion records every harness the image carries. A row from before C11
    becomes a one-harness document; the downgrade keeps the row readable."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0017_lab_local")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM image_promotions"))
        conn.execute(
            text(
                "INSERT INTO image_promotions (digest, reference, harness, harness_version, "
                "state, reason, updated_at, updated_by) VALUES ('sha256:c5', "
                "'crucible-worker:codex-0.153.4-x', 'codex', '0.153.4', 'default', '', now(), "
                "'tests')"
            )
        )
    migrate.upgrade(database_url, "0020_provider_settings")
    with engine.connect() as conn:
        harnesses = conn.execute(
            text("SELECT harnesses FROM image_promotions WHERE digest='sha256:c5'")
        ).scalar_one()
    assert harnesses == {"codex": "0.153.4"}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO image_promotions (digest, reference, harnesses, state, reason, "
                "updated_at, updated_by) VALUES ('sha256:c11', 'crucible-worker:20260916-x', "
                "CAST(:harnesses AS jsonb), 'default', '', now(), 'tests')"
            ),
            {"harnesses": json.dumps({"codex": "0.156.0", "agy": "1.2.8"})},
        )
    migrate.downgrade(database_url, "0017_lab_local")
    with engine.connect() as conn:
        rows = {
            str(digest): str(value)
            for digest, value in conn.execute(
                text("SELECT digest, harness || ' ' || harness_version FROM image_promotions")
            ).all()
        }
    assert rows == {"sha256:c5": "codex 0.153.4", "sha256:c11": "agy 1.2.8"}
    migrate.upgrade(database_url, "0020_provider_settings")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM image_promotions"))
    migrate.upgrade(database_url)
    engine.dispose()


def test_0004_down_and_up(database_url: str) -> None:
    """Down migrations are required for every revision in v0.x (14)."""
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0003_idempotency_reservation")
    names = set(inspect(engine).get_table_names())
    assert "gate_results" not in names and "wakes" not in names
    assert "head_sha" not in {c["name"] for c in inspect(engine).get_columns("tasks")}
    migrate.upgrade(database_url)
    names = set(inspect(engine).get_table_names())
    assert {"gate_results", "wakes", "attempt_metrics"} <= names
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    engine.dispose()


def test_0002_down_and_up(database_url: str) -> None:
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0001_walking_skeleton")
    cols = {c["name"] for c in inspect(engine).get_columns("supervisor_status")}
    assert "last_success_at" not in cols
    migrate.upgrade(database_url)
    cols = {c["name"] for c in inspect(engine).get_columns("supervisor_status")}
    assert {"last_success_at", "last_error_at", "last_error"} <= cols
    engine.dispose()


def test_a_downgrade_past_c4_keeps_the_c4_events(database_url: str) -> None:
    """14: `events` is the audit log. The revisions below 0007 recreate the event-kind
    CHECK in its validating form, which C4 rows cannot satisfy, so the downgrade moves
    them aside rather than deleting them, and the upgrade moves them back."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'publish_completed', 'tests', true, "
                '\'{"marker": "downgrade-test"}\')'
            )
        )
    migrate.downgrade(database_url, "0006_log_occurrence")
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE kind = 'publish_completed'")
            ).scalar()
            == 0
        )
        # Not deleted: moved.
        kept = conn.execute(
            text(
                "SELECT count(*) FROM events_c4_archive WHERE payload->>'marker' = 'downgrade-test'"
            )
        ).scalar()
    assert kept == 1
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        restored = conn.execute(
            text("SELECT count(*) FROM events WHERE payload->>'marker' = 'downgrade-test'")
        ).scalar()
        assert conn.execute(text("SELECT to_regclass('public.events_c4_archive')")).scalar() is None
    assert restored == 1
    # And the sequence still hands out a usable value after the rows came back.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'publish_completed', 'tests', true, '{}')"
            )
        )
    engine.dispose()


def test_the_publish_pending_flag_becomes_the_publishing_state(database_url: str) -> None:
    """09: C4 replaces the flag with the state. A task accepted under C2 is waiting with
    the flag raised, and a bare drop would strand it in `awaiting_acceptance` for ever."""
    migrate.downgrade(database_url, "0006_log_occurrence")
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO principals (id, name, role, token_salt, token_hash, created_at) "
                "VALUES ('01MIGP00000000000000000001', 'migration-test', 'orchestrator', "
                "'\\x00', '\\x00', now()) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO repositories (id, name, url, default_branch, policy_name, "
                "registered_by, created_at) VALUES ('01MIGR00000000000000000001', "
                "'migration/test', 'https://github.com/migration/test', 'main', "
                "'default-software', 'tests', now()) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, external_id, principal_id, repository_id, project, "
                "title, state, contract_version, policy_name, policy_version, created_at, "
                "updated_at, head_sha, publish_pending) VALUES "
                "('01MIGT00000000000000000001', 'MIG-1', '01MIGP00000000000000000001', "
                "'01MIGR00000000000000000001', 'p', 't', 'awaiting_acceptance', 1, "
                "'default-software', 1, now(), now(), 'abc123', true)"
            )
        )
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM tasks WHERE id = '01MIGT00000000000000000001'")
        ).scalar_one()
        events = conn.execute(
            text(
                "SELECT count(*) FROM events WHERE task_id = '01MIGT00000000000000000001' "
                "AND kind = 'task_publishing'"
            )
        ).scalar_one()
    assert state == "publishing"
    assert events == 1
    engine.dispose()


def test_0010_down_and_up(database_url: str) -> None:
    """C6: the bootstrap_imports table and the attempts.unsupervised flag (15, 14)."""
    engine = make_engine(database_url)
    migrate.upgrade(database_url)
    names = set(inspect(engine).get_table_names())
    assert "bootstrap_imports" in names
    assert "unsupervised" in {c["name"] for c in inspect(engine).get_columns("attempts")}
    indexes = {i["name"] for i in inspect(engine).get_indexes("bootstrap_imports")}
    assert {"ix_bootstrap_imports_content", "uq_bootstrap_imports_authoritative"} <= indexes
    migrate.downgrade(database_url, "0009_administration")
    names = set(inspect(engine).get_table_names())
    assert "bootstrap_imports" not in names
    assert "unsupervised" not in {c["name"] for c in inspect(engine).get_columns("attempts")}
    with engine.begin() as conn, pytest.raises(Exception, match="ck_events_kind"):
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'bootstrap_handoff', 'tests', true, '{}')"
            )
        )
    migrate.upgrade(database_url)
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'bootstrap_handoff', 'tests', true, '{}')"
            )
        )
    engine.dispose()


def test_0011_down_and_up_preserves_class_routing_events(database_url: str) -> None:
    engine = make_engine(database_url)
    migrate.upgrade(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'pool_exhausted', 'tests', true, "
                '\'{"marker": "c6b-downgrade-test"}\')'
            )
        )
    migrate.downgrade(database_url, "0010_bootstrap_import")
    names = set(inspect(engine).get_table_names())
    assert "pool_exhaustions" not in names
    assert "resume_at" not in {c["name"] for c in inspect(engine).get_columns("tasks")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('public.events_c6b_archive')")).scalar()
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        restored = conn.execute(
            text("SELECT count(*) FROM events WHERE payload->>'marker' = 'c6b-downgrade-test'")
        ).scalar_one()
        assert (
            conn.execute(text("SELECT to_regclass('public.events_c6b_archive')")).scalar() is None
        )
    assert restored == 1
    engine.dispose()


def test_0012_down_and_up_preserves_stall_events(database_url: str) -> None:
    engine = make_engine(database_url)
    migrate.upgrade(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'worker_stalled', 'tests', true, "
                '\'{"marker": "c6c-downgrade-test"}\')'
            )
        )
    migrate.downgrade(database_url, "0011_class_routing")
    assert "heartbeats" not in set(inspect(engine).get_table_names())
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        restored = conn.execute(
            text("SELECT count(*) FROM events WHERE payload->>'marker' = 'c6c-downgrade-test'")
        ).scalar_one()
    assert restored == 1
    engine.dispose()


def test_0015_down_and_up_preserves_admin_ui_events(database_url: str) -> None:
    engine = make_engine(database_url)
    migrate.upgrade(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (ts, kind, principal, verified, payload) "
                "VALUES (now(), 'principal_revoked', 'tests', true, "
                '\'{"marker": "c7a-downgrade-test"}\')'
            )
        )
    migrate.downgrade(database_url, "0014_enable_hermes_local")
    with engine.connect() as conn:
        archived = conn.execute(
            text(
                "SELECT count(*) FROM events_c7a_archive "
                "WHERE payload->>'marker' = 'c7a-downgrade-test'"
            )
        ).scalar_one()
    assert archived == 1
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        restored = conn.execute(
            text("SELECT count(*) FROM events WHERE payload->>'marker' = 'c7a-downgrade-test'")
        ).scalar_one()
        assert (
            conn.execute(text("SELECT to_regclass('public.events_c7a_archive')")).scalar() is None
        )
    assert restored == 1
    engine.dispose()


def test_0011_refuses_an_incompatible_contract_on_a_non_terminal_task(
    database_url: str,
) -> None:
    engine = make_engine(database_url)
    if migrate.current_revision(engine) is None:
        migrate.upgrade(database_url, "0010_bootstrap_import")
    else:
        migrate.downgrade(database_url, "0010_bootstrap_import")
    document = contract_document(external_id="MIG-C6B-GUARD")
    document["execution_request"].update({"harness": "codex", "model": "gpt-5.6-luna"})
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO principals (id, name, role, token_salt, token_hash, created_at) "
                "VALUES ('01MIGC6BP0000000000000001', 'c6b-migration-principal', "
                "'orchestrator', '\\x00', '\\x00', now()) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO repositories (id, name, url, default_branch, policy_name, "
                "registered_by, created_at, external_review_attested) VALUES "
                "('01MIGC6BR0000000000000001', "
                "'migration/c6b', 'https://example.invalid/migration/c6b', 'main', "
                "'default-software', 'tests', now(), false) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, external_id, principal_id, repository_id, project, "
                "title, state, contract_version, policy_name, policy_version, created_at, "
                "updated_at, head_sha) VALUES ('01MIGC6BT0000000000000001', 'MIG-C6B-GUARD', "
                "'01MIGC6BP0000000000000001', '01MIGC6BR0000000000000001', 'p', 't', "
                "'submitted', 1, 'default-software', 1, now(), now(), NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO task_contracts "
                "(id, task_id, version, document, sha256, submitted_at) VALUES "
                "('01MIGC6BC0000000000000001', '01MIGC6BT0000000000000001', 1, "
                "CAST(:document AS jsonb), 'invalid-contract-for-migration-guard', now())"
            ),
            {"document": json.dumps(document)},
        )
    with pytest.raises(RuntimeError, match="01MIGC6BT0000000000000001"):
        migrate.upgrade(database_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET state='cancelled' WHERE id='01MIGC6BT0000000000000001'")
        )
    migrate.upgrade(database_url)
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    engine.dispose()


def test_0020_provider_settings_down_and_up_keeps_the_audit_trail(database_url: str) -> None:
    """crucible#91: the setting table goes with a rollback, but the audit events of an
    edit are archived and come back on the next upgrade."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO provider_settings (name, document, reason, updated_at, updated_by) "
                "VALUES ('kubernetes.egress', CAST(:doc AS jsonb), 'r', now(), 'tests')"
            ),
            {"doc": json.dumps({"dns": {"namespace": "kube-system"}})},
        )
        conn.execute(
            text(
                # An explicit seq: an earlier migration test's archive round trip
                # re-inserts events by seq and leaves the sequence behind them.
                "INSERT INTO events (seq, ts, kind, principal, verified, payload) "
                "SELECT coalesce(max(seq), 0) + 1, now(), 'kubernetes_egress_updated', "
                "'tests', true, '{}'::jsonb FROM events"
            )
        )
    migrate.downgrade(database_url, "0019_opus_5_5")
    assert "provider_settings" not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE kind='kubernetes_egress_updated'")
            ).scalar()
            == 0
        )
    migrate.upgrade(database_url)
    assert "provider_settings" in inspect(engine).get_table_names()
    with engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE kind='kubernetes_egress_updated'")
            ).scalar()
            == 1
        )
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    engine.dispose()


def test_0023_carries_each_harness_default_forward_and_back(database_url: str) -> None:
    """crucible#116: promotion is per harness. Each harness's current default (the most
    recent `default` row that carries it) becomes its own default, and the most recent
    `retained` row that carries it becomes the image a rollback returns to."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0022_first_run_setup")
    insert = text(
        "INSERT INTO image_promotions (digest, reference, harnesses, state, reason, "
        "updated_at, updated_by) VALUES (:digest, :reference, CAST(:harnesses AS jsonb), "
        ":state, '', now() - make_interval(mins => :age), 'tests')"
    )
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM image_promotions"))
        for digest, reference, harnesses, state, age in (
            ("sha256:old", "w:0.5.4", {"hermes": "0.19.0", "agy": "1.2.7"}, "retained", 30),
            ("sha256:new", "w:0.5.5", {"hermes": "0.19.0", "agy": "1.2.8"}, "default", 20),
            ("sha256:agy", "w:agy", {"agy": "1.2.9"}, "default", 10),
        ):
            conn.execute(
                insert,
                {
                    "digest": digest,
                    "reference": reference,
                    "harnesses": json.dumps(harnesses),
                    "state": state,
                    "age": age,
                },
            )
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        rows = {
            str(r.harness): (r.digest, r.version, r.previous_digest, r.previous_version)
            for r in conn.execute(text("SELECT * FROM harness_images"))
        }
    # agy's default replaced the combined image it was promoted over, which stayed the
    # default for hermes; hermes's replaced the retained one.
    assert rows == {
        "agy": ("sha256:agy", "1.2.9", "sha256:new", "1.2.8"),
        "hermes": ("sha256:new", "0.19.0", "sha256:old", "0.19.0"),
    }
    assert "image_promotions" not in inspect(engine).get_table_names()
    migrate.downgrade(database_url, "0022_first_run_setup")
    with engine.connect() as conn:
        back = {
            str(r.digest): (r.state, dict(r.harnesses))
            for r in conn.execute(text("SELECT * FROM image_promotions"))
        }
    assert back == {
        "sha256:agy": ("default", {"agy": "1.2.9"}),
        "sha256:new": ("default", {"hermes": "0.19.0"}),
        "sha256:old": ("retained", {"hermes": "0.19.0"}),
    }
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM image_promotions"))
    migrate.upgrade(database_url)


def test_0021_command_timeout_down_and_up_keeps_the_audit_trail(database_url: str) -> None:
    """crucible#128: a rollback archives the command timeout's audit events and the
    next upgrade restores them."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (seq, ts, kind, principal, verified, payload) "
                "SELECT coalesce(max(seq), 0) + 1, now(), 'command_timeout_updated', "
                "'tests', true, '{}'::jsonb FROM events"
            )
        )
    migrate.downgrade(database_url, "0020_provider_settings")

    def count() -> int:
        with engine.connect() as conn:
            value = conn.execute(
                text("SELECT count(*) FROM events WHERE kind='command_timeout_updated'")
            ).scalar()
        return int(value or 0)

    assert count() == 0
    migrate.upgrade(database_url)
    assert count() == 1
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    engine.dispose()


def test_0027_decides_only_an_administrators_disable_and_comes_back_off(
    database_url: str,
) -> None:
    """hades #174 (Codex round on PR 224): only an administrator's disable becomes a
    decision. A row a migration seeded off (Codex, 0008) starts undecided, so the
    configuration default governs it: off while the configuration keeps it off, on as
    soon as the configuration says so. A row an administrator disabled is decided and
    stays off whatever the configuration says. A rollback drops only the decision and
    puts the untouched seed back."""
    migrate.upgrade(database_url)
    engine = make_engine(database_url)
    migrate.downgrade(database_url, "0026_github_app_manifest")
    with engine.connect() as conn:
        seeded = conn.execute(
            text("SELECT enabled, reason, updated_by FROM harnesses WHERE name = 'codex'")
        ).one()
    assert (seeded.enabled, seeded.updated_by) == (False, "migration")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM harnesses WHERE name <> 'codex'"))
        conn.execute(
            text(
                "INSERT INTO harnesses (name, enabled, reason, session_compatibility, "
                "updated_at, updated_by) VALUES "
                "('claude_code', true, '', 'verified', now(), 'migration'), "
                "('agy', false, 'rotating', 'unverified', now(), 'admin-scott')"
            )
        )
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        rows = {
            str(r.name): (r.enabled, r.enabled_decided, r.reason)
            for r in conn.execute(
                text("SELECT name, enabled, enabled_decided, reason FROM harnesses")
            )
        }
    assert rows == {
        "codex": (True, False, ""),
        "claude_code": (True, False, ""),
        "agy": (False, True, "rotating"),
    }

    registry = default_registry()
    off = {"codex": HarnessGate(enabled=False, reason="unverified"), "agy": HarnessGate()}
    on = {"codex": HarnessGate(enabled=True), "agy": HarnessGate(enabled=True)}
    with SqlUnitOfWorkFactory(engine)() as uow:
        codex, agy = uow.harnesses.get("codex"), uow.harnesses.get("agy")
    with pytest.raises(HarnessUnavailableError, match="off by the configuration default"):
        registry.resolve("codex", gates=off, state=codex)
    assert registry.resolve("codex", gates=on, state=codex).name == "codex"
    with pytest.raises(HarnessUnavailableError, match="disabled by an administrator"):
        registry.resolve("agy", gates=on, state=agy)

    migrate.downgrade(database_url, "0026_github_app_manifest")
    assert "enabled_decided" not in {c["name"] for c in inspect(engine).get_columns("harnesses")}
    with engine.connect() as conn:
        back = {
            str(r.name): (r.enabled, r.reason)
            for r in conn.execute(text("SELECT name, enabled, reason FROM harnesses"))
        }
    assert back == {
        "codex": (False, seeded.reason),
        "claude_code": (True, ""),
        "agy": (False, "rotating"),
    }
    migrate.upgrade(database_url)
    ok, detail = migrate.is_current(engine, database_url)
    assert ok, detail
    engine.dispose()


# Placeholders for a NOT NULL column without a default that a seed row does not name,
# by Postgres type, so the seed follows the 0035 schema instead of restating it.
_PLACEHOLDERS = {
    "character varying": "seed",
    "text": "seed",
    "integer": 0,
    "bigint": 0,
    "boolean": False,
    "timestamp with time zone": "2026-01-01T00:00:00+00:00",
    "jsonb": "{}",
    "bytea": b"\x00",
}


def _seed(conn: Connection, table: str, row: dict[str, Any]) -> None:
    """Insert `row`, filling every other required column of `table` as it is now."""
    required = conn.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table "
            "AND is_nullable = 'NO' AND column_default IS NULL"
        ),
        {"table": table},
    ).all()
    values = dict(row)
    for name, data_type in required:
        if name not in values:
            values[name] = _PLACEHOLDERS[data_type]
    columns = ", ".join(f'"{name}"' for name in values)
    params = ", ".join(f":{name}" for name in values)
    conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({params})"), values)


def test_0035_to_head_upgrades_a_populated_database(database_url: str) -> None:
    """Issue 409: a migration runs with no supervisor lease and no fenced token, so a
    backfill of a fenced table has to stand its trigger down (0038 for `attempts`, 0039
    for `pull_requests`). The other migration tests upgrade empty tables, where an
    UPDATE matches no row and the trigger never fires. This one seeds a row in every
    fenced table at 0035, as a live 0.7.3 database has, and upgrades it to head: a
    migration that writes a fenced table without disabling its trigger fails here."""
    migrate.downgrade(database_url, "0035_credential_renewer")
    engine = make_engine(database_url)
    task, execution, attempt = (
        "01MIG4090000000000000TASK1",
        "01MIG4090000000000000EXEC1",
        "01MIG40900000000000ATTEMPT",
    )
    pending = "01MIG40900000000000PENDING"
    pull_request = "01MIG40900000000000000PR01"
    try:
        with engine.begin() as conn:
            # The seed writes as the supervisor would: holding the lease, presenting its
            # token for this transaction only. The migrations run on their own
            # connection afterwards, with neither.
            conn.execute(
                text(
                    "INSERT INTO leases (id, kind, key, holder, fenced_token, expires_at) "
                    "VALUES ('01MIG409000000000000LEASE1', 'supervisor', 'supervisor', "
                    "'migration-test', 409, now() + interval '1 hour')"
                )
            )
            conn.execute(text("SELECT set_config('crucible.fenced_token', '409', true)"))
            _seed(
                conn,
                "principals",
                {"id": "01MIG409000000000PRINCIPAL", "name": "mig-409", "role": "orchestrator"},
            )
            _seed(
                conn,
                "repositories",
                {
                    "id": "01MIG40900000000000000REPO",
                    "name": "migration/409",
                    "url": "https://github.com/migration/409",
                },
            )
            _seed(
                conn,
                "tasks",
                {
                    "id": task,
                    "external_id": "MIG-409",
                    "principal_id": "01MIG409000000000PRINCIPAL",
                    "repository_id": "01MIG40900000000000000REPO",
                    "state": "awaiting_external_review",
                    "contract_version": 1,
                    "policy_name": "default-software",
                    "policy_version": 1,
                },
            )
            _seed(
                conn,
                "executions",
                {
                    "id": execution,
                    "task_id": task,
                    "role": "implement",
                    "contract_version": 1,
                    "state": "succeeded",
                    "policy_snapshot": json.dumps(
                        {"routing": {"policy": {"name": "default-routing", "version": 3}}}
                    ),
                    "retry_on": "[]",
                },
            )
            _seed(
                conn,
                "attempts",
                {
                    "id": attempt,
                    "execution_id": execution,
                    "task_id": task,
                    "number": 1,
                    "state": "succeeded",
                    "selected_model": "claude-opus-5-5",
                    "ordered_candidates": "[]",
                    "routing_excluded_pools": "[]",
                },
            )
            _seed(
                conn,
                "attempts",
                {
                    "id": pending,
                    "execution_id": execution,
                    "task_id": task,
                    "number": 2,
                    "state": "pending",
                    "ordered_candidates": "[]",
                    "routing_excluded_pools": "[]",
                },
            )
            _seed(
                conn,
                "pull_requests",
                {
                    "id": pull_request,
                    "task_id": task,
                    "repository_id": "01MIG40900000000000000REPO",
                    "number": 409,
                    "url": "https://github.com/migration/409/pull/409",
                    "base_ref": "release/0.8",
                    "work_branch": "crucible/MIG-409",
                    "state": "open",
                    "head_sha": "4" * 40,
                },
            )
            _seed(
                conn,
                "evidence",
                {
                    "attempt_id": attempt,
                    "task_id": task,
                    "pull_request_id": pull_request,
                    "kind": "ci",
                    "source": "crucible",
                    "verified": True,
                },
            )
            _seed(
                conn,
                "wakes",
                {
                    "id": "01MIG40900000000000000WAKE",
                    "principal_id": "01MIG409000000000PRINCIPAL",
                    "task_id": task,
                    "reason": "task_state_changed",
                },
            )
            # `principal = 'crucible'` is the fenced form of an event (0001).
            _seed(
                conn,
                "events",
                {
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "task_submitted",
                    "task_id": task,
                    "execution_id": execution,
                    "attempt_id": attempt,
                    "principal": "crucible",
                    "verified": True,
                },
            )
            conn.execute(text("DELETE FROM leases WHERE kind = 'supervisor'"))

        migrate.upgrade(database_url)

        with engine.connect() as conn:
            observed = conn.execute(
                text(
                    "SELECT observed_head_sha, observed_base_ref, mergeable_state, "
                    "merge_refusal_count FROM pull_requests WHERE id = :id"
                ),
                {"id": pull_request},
            ).one()
            routed = {
                row.id: row.routing_version
                for row in conn.execute(
                    text("SELECT id, routing_version FROM attempts WHERE task_id = :task"),
                    {"task": task},
                )
            }
            assert (
                conn.execute(
                    text("SELECT count(*) FROM evidence WHERE task_id = :task"), {"task": task}
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM events WHERE task_id = :task"), {"task": task}
                ).scalar_one()
                == 1
            )
        assert tuple(observed) == ("4" * 40, "release/0.8", "", 0)
        # 0038: a routed attempt takes its snapshot's routing version; a pending one stays NULL.
        assert routed == {attempt: 3, pending: None}
        ok, detail = migrate.is_current(engine, database_url)
        assert ok, detail

        # And back down past 0039 with an event of the kind it added: `events` is
        # append-only, so its downgrade archives the row and its upgrade restores it.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO events (ts, kind, task_id, principal, verified, payload) "
                    "VALUES (now(), 'auto_merge_updated', :task, 'tests', true, "
                    '\'{"marker": "0039-downgrade-test"}\')'
                ),
                {"task": task},
            )
        migrate.downgrade(database_url, "0038_attempt_routing_version")
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM events WHERE kind = 'auto_merge_updated'")
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM events_0039_archive "
                        "WHERE payload->>'marker' = '0039-downgrade-test'"
                    )
                ).scalar_one()
                == 1
            )
        migrate.upgrade(database_url)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM events WHERE kind = 'auto_merge_updated' "
                        "AND payload->>'marker' = '0039-downgrade-test'"
                    )
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(text("SELECT to_regclass('public.events_0039_archive')")).scalar()
                is None
            )
    finally:
        engine.dispose()


def test_0051_renames_fenced_rows_both_directions(database_url: str) -> None:
    """Issue 567: the lab aliases must migrate with fenced rows and no migration lease."""
    migrate.downgrade(database_url, "0050_status_cache")
    engine = make_engine(database_url)
    task = "01MIG5670000000000000TASK1"
    routes = [("codex", "codex-local:fast", "fast"), ("qwen_code", "qwen-coder", "coder")]

    def assert_rows_and_fences(expected: dict[str, str]) -> None:
        with engine.connect() as conn:
            assert {
                row.harness: row.model
                for row in conn.execute(
                    text("SELECT harness, model FROM executions WHERE task_id=:task"),
                    {"task": task},
                )
            } == expected
            assert {
                row.selected_harness: row.selected_model
                for row in conn.execute(
                    text(
                        "SELECT selected_harness, selected_model FROM attempts WHERE task_id=:task"
                    ),
                    {"task": task},
                )
            } == expected
            assert {
                row.tgname: row.tgenabled
                for row in conn.execute(
                    text(
                        "SELECT tgname, tgenabled FROM pg_trigger WHERE tgname IN "
                        "('trg_executions_fenced', 'trg_attempts_fenced')"
                    )
                )
            } == {"trg_executions_fenced": "O", "trg_attempts_fenced": "O"}
            assert conn.execute(text("SELECT count(*) FROM leases")).scalar_one() == 0
        for table, column in (("executions", "model"), ("attempts", "selected_model")):
            with (
                engine.begin() as conn,
                pytest.raises(
                    Exception,
                    match=f"write to {table} requires a transaction-local crucible.fenced_token",
                ),
            ):
                conn.execute(
                    text(f"UPDATE {table} SET {column}={column} WHERE task_id=:task"),
                    {"task": task},
                )

    try:
        with engine.begin() as conn:
            # Reuse #409's schema-aware helper and seed with a transaction-local token.
            conn.execute(
                text(
                    "INSERT INTO leases (id, kind, key, holder, fenced_token, expires_at) "
                    "VALUES ('01MIG567000000000000LEASE1', 'supervisor', 'supervisor', "
                    "'migration-test', 567, now() + interval '1 hour')"
                )
            )
            conn.execute(text("SELECT set_config('crucible.fenced_token', '567', true)"))
            _seed(
                conn,
                "principals",
                {"id": "01MIG567000000000PRINCIPAL", "name": "mig-567", "role": "orchestrator"},
            )
            _seed(
                conn,
                "repositories",
                {
                    "id": "01MIG56700000000000000REPO",
                    "name": "migration/567",
                    "url": "https://github.com/migration/567",
                },
            )
            _seed(
                conn,
                "tasks",
                {
                    "id": task,
                    "external_id": "MIG-567",
                    "principal_id": "01MIG567000000000PRINCIPAL",
                    "repository_id": "01MIG56700000000000000REPO",
                    "state": "awaiting_external_review",
                    "contract_version": 1,
                    "policy_name": "default-software",
                    "policy_version": 1,
                },
            )
            _seed(
                conn,
                "routing_policies",
                {
                    "name": "migration-567",
                    "version": 1,
                    "document": json.dumps(
                        {
                            "models": [
                                {"harness": harness, "id": alias, "model_name": model}
                                for harness, alias, model in routes
                            ]
                        }
                    ),
                },
            )
            for number, (harness, alias, _) in enumerate(routes, start=1):
                execution = f"01MIG5670000000000000EXEC{number}"
                _seed(
                    conn,
                    "executions",
                    {
                        "id": execution,
                        "task_id": task,
                        "role": "implement",
                        "contract_version": 1,
                        "state": "succeeded",
                        "harness": harness,
                        "model": alias,
                        "policy_snapshot": "{}",
                        "retry_on": "[]",
                    },
                )
                _seed(
                    conn,
                    "attempts",
                    {
                        "id": f"01MIG56700000000000000ATT{number}",
                        "execution_id": execution,
                        "task_id": task,
                        "number": 1,
                        "state": "succeeded",
                        "selected_harness": harness,
                        "selected_model": alias,
                        "ordered_candidates": "[]",
                        "routing_excluded_pools": "[]",
                    },
                )
            conn.execute(text("DELETE FROM leases WHERE kind='supervisor'"))

        assert_rows_and_fences({harness: alias for harness, alias, _ in routes})
        migrate.upgrade(database_url, "0051_routing_model_references")
        assert migrate.current_revision(engine) == "0051_routing_model_references"
        assert_rows_and_fences({harness: model for harness, _, model in routes})

        migrate.downgrade(database_url, "0050_status_cache")
        assert migrate.current_revision(engine) == "0050_status_cache"
        # Downgrade chooses canonical legacy ids: codex fast stays fast, while
        # qwen_code coder becomes qwen-coder again.
        assert_rows_and_fences({"codex": "fast", "qwen_code": "qwen-coder"})
    finally:
        engine.dispose()


def test_0051_migrates_populated_qwen_route_without_rerouting_task(database_url: str) -> None:
    """#513: policy references and the attempt's routing version survive the document
    rewrite; only the endpoint model spelling changes from qwen-coder to coder."""
    migrate.downgrade(database_url, "0050_status_cache")
    engine = make_engine(database_url)
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT name, version, document FROM routing_policies "
                    "ORDER BY version DESC LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
        document = dict(row["document"])
        document["models"].append(
            {
                **document["models"][0],
                "id": "qwen-coder",
                "model_name": "coder",
                "harness": "qwen_code",
            }
        )
        conn.execute(
            text(
                "UPDATE routing_policies SET document=CAST(:document AS jsonb) "
                "WHERE name=:name AND version=:version"
            ),
            {**row, "document": json.dumps(document)},
        )
    migrate.upgrade(database_url)
    with engine.connect() as conn:
        migrated = conn.execute(
            text("SELECT document FROM routing_policies WHERE name=:name AND version=:version"),
            dict(row),
        ).scalar_one()
    qwen = next(entry for entry in migrated["models"] if entry["harness"] == "qwen_code")
    assert qwen["model"] == "coder"
    assert "id" not in qwen and "model_name" not in qwen
    assert migrated["version"] == row["version"]
    engine.dispose()


@pytest.mark.parametrize(
    ("head", "kind"),
    [
        ("0044_attempt_stall_shape", "task_proposed"),
        ("0044_editor_leftovers_policy", "task_proposed"),
        ("0044_merge_423_424", "credential_mount_mode_set"),
    ],
)
def test_each_0044_head_upgrades_through_the_0045_merge(
    database_url: str, head: str, kind: str
) -> None:
    """FDY-0385: PRs 426, 438 and 441 each added a 0044 head. A deployed database stands on
    exactly one of them and holds events of the kinds its own 0043 ancestor allowed. It
    reaches the current head through 0045 with those rows intact and no drift. From the two heads
    over 0043_proposed_tasks the path runs 0043_credential_mount_mode over the proposal
    rows, so that revision has to keep the kinds the live CHECK permits rather than
    rebuild it from the 0039 kinds alone."""
    # `downgrade <0044 head>` from 0045 only unapplies 0045 and leaves all three 0044 rows
    # in alembic_version, so the deployed state is reached from below: back to 0042, then
    # up to the one head.
    migrate.downgrade(database_url, "0042_pull_request_mergeable")
    migrate.upgrade(database_url, head)
    engine = make_engine(database_url)
    try:
        with engine.begin() as conn:
            versions = {
                row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            }
            assert versions == {head}
            conn.execute(
                text(
                    "INSERT INTO events (ts, kind, principal, verified, payload) "
                    "VALUES (now(), :kind, 'tests', true, "
                    '\'{"marker": "fdy-0385"}\')'
                ),
                {"kind": kind},
            )
        migrate.upgrade(database_url)
        # Later revisions (0046_blocked_reason, 0047_attempt_egress_probe, ...) sit above
        # the merge; the path still runs them all, up to the one head.
        assert migrate.current_revision(engine) == migrate.head_revision(database_url)
        ok, detail = migrate.is_current(engine, database_url)
        assert ok, detail
        with engine.begin() as conn:
            kept = (
                conn.execute(text("SELECT kind FROM events WHERE payload->>'marker' = 'fdy-0385'"))
                .scalars()
                .all()
            )
            assert kept == [kind]
            # At head the CHECK permits both 0043 siblings' kinds.
            for merged_kind in ("task_proposed", "credential_mount_mode_set"):
                conn.execute(
                    text(
                        "INSERT INTO events (ts, kind, principal, verified, payload) "
                        "VALUES (now(), :kind, 'tests', true, '{}')"
                    ),
                    {"kind": merged_kind},
                )
    finally:
        engine.dispose()
