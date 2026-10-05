"""The envelope, `next`, the role the token proves, and `crucible schema` (docs/client.md)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from crucible.application.corrections import CORRECTABLE_STATES
from crucible.cli import admin
from crucible.cli.main import build_parser, run
from crucible.client import next as nx
from crucible.client.envelope import UsageError
from crucible.client.schema import envelope_schema, kind_schemas
from crucible.domain.lifecycle import _CANCELLABLE_AT_ONCE, TASK_TRANSITIONS, TaskState
from tests.fake_crucible import FakeCrucible, fake
from tests.fixtures_data.legacy_cli.capture import walk

__all__ = ["fake"]


def _task(state: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "id": "T1",
        "state": state,
        "principal": "foundry",
        "policy": {"name": "default-software", "version": 3},
        "open_escalations": [],
        **extra,
    }


def _out(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    document = json.loads(capsys.readouterr().out)
    assert isinstance(document, dict)
    return document


def _actions(document: dict[str, Any]) -> list[str]:
    return [entry["action"] for entry in document["next"]]


# ----- the rules follow the API's own checks -------------------------------------------


def test_the_state_sets_are_the_domain_and_service_sets() -> None:
    assert {s.value for s in _CANCELLABLE_AT_ONCE} | {"running"} == nx.CANCELLABLE
    assert {s.value for s in CORRECTABLE_STATES} == nx.CORRECTABLE
    assert {a.value for a, b in TASK_TRANSITIONS if b is TaskState.CLOSED} == nx.CLOSABLE


ALL_ROLES = [*nx.ROLES, None]


@pytest.mark.parametrize("state", [s.value for s in TaskState])
@pytest.mark.parametrize("role", ALL_ROLES)
def test_every_offered_action_is_complete_and_admitted(state: str, role: str | None) -> None:
    """Every `{token}` in a command is described in `needs`; every action's roles admit
    the role in use; nothing is offered to an observer or an unknown role."""
    escalation = {"id": "E1", "state": "open", "question": "which way?"}
    actions = nx.task_actions(_task(state, open_escalations=[escalation]), role, ["crucible"])
    if role in (None, nx.OBSERVER):
        assert actions == []
    for entry in actions:
        assert role in entry["requires"]["roles"]
        if entry["action"] != "start":
            assert entry["requires"]["owner"] == "foundry"
        tokens = {m for arg in entry["command"] for m in re.findall(r"\{(\w+)\}", arg)}
        assert tokens <= set(entry["needs"]), (entry["action"], tokens)
        assert entry["command"][0] == "crucible"
        # Filled from `needs`, the command parses: the argv is one `crucible` accepts.
        fill = {
            name: (need["choices"][0] if isinstance(need, dict) else "x")
            for name, need in entry["needs"].items()
        }
        argv = [
            re.sub(r"\{(\w+)\}", lambda m, f=fill: f[m.group(1)], arg)  # type: ignore[misc]
            for arg in entry["command"][1:]
        ]
        parsed = build_parser().parse_args(argv)
        assert parsed.group == entry["command"][1]


@pytest.mark.parametrize(
    ("state", "role", "expected"),
    [
        ("submitted", nx.ORCHESTRATOR, {"start", "cancel"}),
        ("submitted", nx.ADMIN, {"start", "cancel"}),
        ("awaiting_acceptance", nx.ORCHESTRATOR, {"accept", "corrections", "cancel"}),
        # accept is the orchestrator route's; an admin token would be refused there.
        ("awaiting_acceptance", nx.ADMIN, {"corrections", "cancel"}),
        ("awaiting_internal_review", nx.ORCHESTRATOR, {"review", "cancel"}),
        ("publish_failed", nx.ORCHESTRATOR, {"republish", "corrections", "cancel"}),
        ("publish_failed", nx.ADMIN, {"corrections", "cancel"}),
        ("ci_certification_failed", nx.ORCHESTRATOR, {"ci-decision", "corrections", "cancel"}),
        ("head_diverged", nx.ORCHESTRATOR, {"head-decision", "cancel"}),
        ("external_feedback_received", nx.ORCHESTRATOR, {"dispositions", "corrections", "cancel"}),
        ("accepted", nx.ORCHESTRATOR, {"close", "corrections", "cancel"}),
        ("merged", nx.ADMIN, set()),
        ("running", nx.ORCHESTRATOR, {"cancel"}),
        ("closed", nx.ORCHESTRATOR, set()),
        ("cancelled", nx.ADMIN, set()),
    ],
)
def test_next_by_state_and_role(state: str, role: str, expected: set[str]) -> None:
    assert {e["action"] for e in nx.task_actions(_task(state), role, ["crucible"])} == expected


def test_an_open_escalation_offers_its_decision_and_a_blocked_task_can_reschedule() -> None:
    task = _task("blocked", open_escalations=[{"id": "E9", "state": "open", "question": "q"}])
    decision = next(
        e
        for e in nx.task_actions(task, nx.ORCHESTRATOR, ["crucible"])
        if e["action"] == "decisions"
    )
    assert decision["command"][decision["command"].index("--escalation-id") + 1] == "E9"
    assert decision["optional"] == [
        {"flag": "--reschedule", "description": "send the blocked task back to work"}
    ]


def test_start_carries_the_contracts_policy_version() -> None:
    (start, _cancel) = nx.task_actions(_task("submitted"), nx.ORCHESTRATOR, ["crucible"])
    assert start["command"][:5] == ["crucible", "start", "T1", "--policy-version", "3"]


# ----- the role comes from the API --------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("orchestrator", {"accept", "corrections", "cancel"}),
        ("admin", {"corrections", "cancel"}),
        ("observer", set()),
    ],
)
def test_a_read_probes_the_role_and_offers_what_it_admits(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str], role: str, expected: set[str]
) -> None:
    fake.role(role)
    fake.response = _task("awaiting_acceptance")
    assert run(["task", "T1"]) == 0
    document = _out(capsys)
    assert document["kind"] == "task" and document["state"] == "awaiting_acceptance"
    assert document["principal_role"] == role
    assert set(_actions(document)) == expected
    assert document["data"] == _task("awaiting_acceptance")


def test_an_unknown_role_offers_nothing_and_says_why(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.routes["/v1/admin/audit"] = (503, {"type": "urn:crucible:problem:x", "detail": "down"})
    fake.response = _task("awaiting_acceptance")
    assert run(["task", "T1"]) == 0
    document = _out(capsys)
    assert document["next"] == [] and document["principal_role"] is None
    assert "role is unknown" in document["warnings"][0]


def test_an_orchestrator_route_proves_the_role_without_a_probe(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.response = _task("accepted")
    assert run(["accept", "T1", "--verdict", "accepted", "--reason", "ok"]) == 0
    assert [r["path"] for r in fake.requests] == ["/v1/tasks/T1/accept"]
    document = _out(capsys)
    assert document["principal_role"] == "orchestrator"
    assert _actions(document) == ["corrections", "close", "cancel"]


def test_a_refusal_is_the_apis_problem_in_the_envelope(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    problem = {
        "type": "urn:crucible:problem:forbidden",
        "title": "orchestrator or operator role required",
        "status": 403,
        "detail": None,
        "instance": "/v1/tasks/T1/accept",
        "errors": [],
    }
    fake.status = 403
    fake.response = problem
    assert run(["accept", "T1", "--verdict", "accepted", "--reason", "ok"]) == 1
    document = _out(capsys)
    assert document["ok"] is False and document["next"] == []
    assert document["error"]["code"] == "forbidden"
    assert document["error"]["problem"] == problem
    assert "role" in document["error"]["hint"]


def test_wakes_offer_ack_and_the_task(
    fake: FakeCrucible, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.role("orchestrator")
    fake.response = {
        "schema_version": "1.0",
        "items": [{"id": "W1", "task_id": "T1", "acked_at": None}],
        "next_cursor": None,
    }
    assert run(["wakes"]) == 0
    document = _out(capsys)
    assert document["kind"] == "wake_list" and _actions(document) == ["ack", "show-task"]


# ----- usage and failure are envelopes too ---------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bogus"],
        ["admin"],
        ["admin", "credentials"],
        ["accept", "T1", "--verdict", "maybe", "--reason", "r"],
    ],
)
def test_usage_is_an_envelope_with_exit_two(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert run(argv) == 2
    document = _out(capsys)
    assert document["ok"] is False and document["error"]["code"] == "usage"
    assert document["error"]["hint"].startswith("run `crucible")


def test_help_is_prose_and_complete(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        run(["--help"])
    assert raised.value.code == 0
    text = capsys.readouterr().out
    for word in ("envelope", "next", "CRUCIBLE_TOKEN", "--table", "Exit code", "schema"):
        assert word in text


# ----- the admin group's next ---------------------------------------------------------


@pytest.mark.parametrize("state", ["absent", "configured"])
def test_login_is_offered_remotely_too(state: str) -> None:
    """The remote admin API runs login in a promoted worker image
    (crucible/adapters/api/routers/admin.py `admin_login`), so `next` offers it on the
    credential's state alone, the same as local mode."""
    actions = nx.credential_actions("codex", state, ["crucible", "admin", "--api-url", "https://x"])
    login = next(e for e in actions if e["action"] == "login")
    assert ("--replace" in login["command"]) == (state != "absent")
    fill = {name: "x" for name in login["needs"]}
    argv = [re.sub(r"\{(\w+)\}", lambda m: fill[m.group(1)], arg) for arg in login["command"][1:]]
    parsed = build_parser().parse_args(argv)
    assert parsed.group == "admin"

    assert nx.credential_actions("hermes", "not_required", []) == []


def _every_next_action() -> list[dict[str, Any]]:
    """Every action every `next` rule can offer, across states, roles, and documents, so
    a defect in any one rule's `optional` flags is caught without special-casing a verb."""
    prefix = ["crucible"]
    admin_prefix = ["crucible", "admin"]
    escalation = {"id": "E1", "state": "open", "question": "q?"}
    entries: list[dict[str, Any]] = []
    for state in TaskState:
        for role in nx.ROLES:
            entries += nx.task_actions(
                _task(state.value, open_escalations=[escalation]), role, prefix
            )
    entries += nx.task_list_actions({"items": [{"id": "T1"}]}, prefix)
    entries += nx.wake_actions(
        {"items": [{"id": "W1", "task_id": "T1", "acked_at": None}]}, nx.ORCHESTRATOR, prefix
    )
    for harness in ("codex", "claude_code", "agy", "hermes"):
        for credential_state in ("absent", "configured", "invalid", "validated"):
            entries += nx.credential_actions(harness, credential_state, admin_prefix)
    entries += nx.local_endpoint_actions(
        {"models": [{"id": "coder", "enabled": True}, {"id": "other", "enabled": False}]},
        admin_prefix,
    )
    entries += nx.harness_actions([{"name": "codex", "enabled": True}], admin_prefix)
    entries += nx.harness_actions([{"name": "codex", "enabled": False}], admin_prefix)
    entries += nx.image_actions(
        [
            {
                "digest": "sha256:x",
                "harnesses": ["codex"],
                "supported": True,
                "promotion_state": "candidate",
            }
        ],
        admin_prefix,
    )
    entries += nx.exhaustion_actions([{"pool": "p1", "active": True}], admin_prefix)
    entries += nx.token_actions([{"id": "P1", "disabled_at": None}], admin_prefix)
    entries += nx.bootstrap_actions({"state": "verified", "import_id": "I1"}, admin_prefix)
    entries += nx.audit_actions({"next_cursor": 5}, admin_prefix, 100, 0)
    return entries


def test_every_next_action_and_its_optional_flags_parse() -> None:
    """Every `next` argv parses, and so does every advertised optional flag added to it,
    whether it takes a value or not: the class of defect where an advertised flag
    contradicts one already baked into the command (set-local-endpoint's --enable and
    --disable) is not specific to one verb."""
    entries = _every_next_action()
    assert entries
    for entry in entries:
        fill = {
            name: (need["choices"][0] if isinstance(need, dict) else "x")
            for name, need in entry["needs"].items()
        }
        base = [
            re.sub(r"\{(\w+)\}", lambda m, f=fill: f[m.group(1)], arg)  # type: ignore[misc]
            for arg in entry["command"][1:]
        ]
        build_parser().parse_args(base)
        for optional in entry.get("optional", []):
            flag = optional["flag"]
            parses = False
            for argv in ([*base, flag], [*base, flag, "x"], [*base, flag, "1"]):
                try:
                    build_parser().parse_args(argv)
                    parses = True
                    break
                except UsageError:
                    continue
            assert parses, f"{entry['action']}: no encoding of {flag} parses on {base}"


def test_a_remote_admin_command_names_its_url_in_next(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        return {"harness": "codex", "credential": {"state": "configured"}}

    monkeypatch.setattr("crucible.client.http.Api.call", fake_call)
    monkeypatch.setenv("CRUCIBLE_ADMIN_TOKEN", "cru_" + "0" * 26 + "." + "s" * 40)
    argv = ["admin", "--api-url", "http://127.0.0.1:1", "--reason", "r"]
    assert run([*argv, "credentials", "probe", "--harness", "codex"]) == 0
    document = _out(capsys)
    assert document["kind"] == "credential_report" and document["state"] == "configured"
    assert document["principal_role"] == "admin"
    assert {e["action"] for e in document["next"]} == {
        "validate",
        "probe",
        "rotate",
        "remove",
        "login",
    }
    for entry in document["next"]:
        assert entry["command"][:4] == ["crucible", "admin", "--api-url", "http://127.0.0.1:1"]


# ----- the schema ----------------------------------------------------------------------


def test_schema_names_every_kind_a_command_can_print(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["schema"]) == 0
    document = _out(capsys)
    kinds = document["data"]["kinds"]
    tree = walk(build_parser())
    produced = {"task", "task_list", "task_detail", "wake", "wake_list", "health"}
    for path, shape in tree.items():
        words = path.split()
        if words[:1] != ["admin"] or any(p.startswith(path + " ") for p in tree):
            continue
        argv = [*words, *("x" for _ in shape["positionals"])]
        for flag, option in shape["options"].items():
            if option["required"]:
                default = "1" if option["type"] == "int" else "x"
                argv += [flag, (option["choices"] or [default])[0]]
        if path == "admin routing set-local-endpoint":
            # A required mutually-exclusive group; walk() records its members but not
            # the group's own requiredness, so synthesize the missing choice here.
            argv += ["--enable"]
        produced.add(admin.kind_of(build_parser().parse_args(argv)))
    assert produced | {"schema", "error"} <= set(kinds), produced - set(kinds)
    assert document["data"]["envelope"] == envelope_schema()
    assert set(kind_schemas()) == set(kinds)


def test_success_and_failure_carry_every_required_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    required = set(envelope_schema()["required"])
    assert run(["schema"]) == 0
    assert required <= set(_out(capsys))
    assert run(["bogus"]) == 2
    failure = _out(capsys)
    assert required | {"error"} <= set(failure)
    assert set(failure["error"]) >= {"code", "message", "hint"}
