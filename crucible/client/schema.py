"""`crucible schema`: the envelope's JSON schema and the schema of every `kind`'s `data`.

The orchestrator records are the API's own response models (04), so their schemas are
generated from those models and cannot drift from them. The administrative documents
(25) are plain objects the admin services build; their schemas name the fields a caller
can rely on and leave the rest open (`additionalProperties` true), which is what the
admin API itself promises.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from crucible.client.envelope import ENVELOPE_VERSION
from crucible.client.next import ROLES
from crucible.contracts.api import (
    AttemptView,
    CompletionClaimView,
    EventList,
    EvidenceList,
    GateList,
    HealthView,
    ImageView,
    PullRequestView,
    TaskList,
    TaskView,
    WakeList,
    WakeView,
)

JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _model(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema(mode="serialization")


def _obj(
    description: str,
    required: dict[str, Any] | None = None,
    optional: dict[str, Any] | None = None,
) -> dict[str, Any]:
    properties = {**(required or {}), **(optional or {})}
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": sorted(required or {}),
        "additionalProperties": True,
    }


def _items(description: str, item: dict[str, Any]) -> dict[str, Any]:
    return _obj(description, {"items": {"type": "array", "items": item}})


STR = {"type": "string"}
NSTR = {"type": ["string", "null"]}
BOOL = {"type": "boolean"}
ANY_OBJ = {"type": "object", "additionalProperties": True}

ACTION = {
    "type": "object",
    "description": "One action valid from the record's state for the principal in use.",
    "properties": {
        "action": {"type": "string", "description": "the action's name"},
        "description": {"type": "string"},
        "command": {
            "type": "array",
            "items": STR,
            "description": "argv that performs it; `{name}` tokens are filled from `needs`",
        },
        "needs": {
            "type": "object",
            "description": "what each `{name}` token must be: a description, or "
            "{choices: [...]} when the API takes a fixed set",
        },
        "optional": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"flag": STR, "description": STR},
                "required": ["flag", "description"],
            },
        },
        "requires": {
            "type": "object",
            "properties": {
                "roles": {"type": "array", "items": {"enum": list(ROLES)}},
                "owner": {
                    "type": "string",
                    "description": "the task's principal; an orchestrator acts only on "
                    "its own tasks",
                },
            },
            "required": ["roles"],
        },
    },
    "required": ["action", "description", "command", "needs", "requires"],
    "additionalProperties": False,
}

ERROR = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "the API problem type's slug (e.g. `forbidden`, "
            "`transition-not-allowed`), or a client code: `usage`, `config`, "
            "`unreachable`, `protocol`",
        },
        "message": STR,
        "hint": {"type": ["string", "null"], "description": "what the caller can do"},
        "status": {"type": "integer", "description": "the HTTP status, when there was one"},
        "problem": {
            "type": "object",
            "description": "the API's RFC 9457 problem detail, whole, when there is one",
        },
    },
    "required": ["code", "message", "hint"],
    "additionalProperties": False,
}


def envelope_schema() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA,
        "title": "crucible command envelope",
        "description": "Every `crucible` command except `serve` prints exactly one of these "
        "on stdout. Exit code 0 when `ok`, 1 when the operation was refused or failed, 2 on "
        "usage.",
        "type": "object",
        "properties": {
            "ok": BOOL,
            "envelope": {"const": ENVELOPE_VERSION},
            "kind": {"type": "string", "description": "names the schema of `data`"},
            "state": {
                "type": ["string", "null"],
                "description": "the record's lifecycle state, where it has one",
            },
            "principal_role": {
                "type": ["string", "null"],
                "enum": [*ROLES, None],
                "description": "the role the token in use holds, as the API showed it; "
                "`orchestrator` also stands for operator; null when unknown",
            },
            "data": {"description": "the API's record, exactly as the API returned it"},
            "next": {"type": "array", "items": ACTION},
            "warnings": {"type": "array", "items": STR},
            "error": ERROR,
        },
        "required": [
            "ok",
            "envelope",
            "kind",
            "state",
            "principal_role",
            "data",
            "next",
            "warnings",
        ],
        "additionalProperties": False,
    }


def kind_schemas() -> dict[str, dict[str, Any]]:
    credential = _obj(
        "a credential's state (25): presence, permissions, expiry class; never a value",
        {"state": {"enum": ["not_required", "absent", "configured", "invalid", "validated"]}},
        {
            "mount_mode": NSTR,
            "source_fingerprint": NSTR,
            "files": {"type": "array"},
            "detail": STR,
            "session_compatibility": STR,
        },
    )
    bootstrap = _obj(
        "a bootstrap import's verification report (15)",
        {"import_id": STR, "state": STR},
    )
    return {
        # the orchestrator verbs (04)
        "task": _model(TaskView),
        "task_list": _model(TaskList),
        "task_detail": _obj(
            "a task with the related records its flags asked for",
            {"task": _model(TaskView)},
            {
                "events": _model(EventList),
                "pull_request": _model(PullRequestView),
                "attempts": {"type": "array", "items": _model(AttemptView)},
                "gates": _model(GateList),
                "report": _model(CompletionClaimView),
                "evidence": _model(EvidenceList),
            },
        ),
        "wake": _model(WakeView),
        "wake_list": _model(WakeList),
        "health": _model(HealthView),
        # the admin group (25)
        "migration": _obj("the schema head migrations reached", {"migrated_to": NSTR}),
        "admin_status": _obj(
            "the sanitized status document (25)",
            {"harnesses": {"type": "array"}, "providers": {"type": "array"}},
        ),
        "token_list": _items(
            "principals without token values",
            _obj(
                "a principal",
                {"id": STR, "name": STR, "role": STR, "created_at": STR, "disabled_at": NSTR},
            ),
        ),
        "token_created": _obj(
            "a new principal and its token, printed this once and never stored in clear",
            {"principal": STR, "role": STR, "token": STR},
        ),
        "token_revoked": _obj("the revoked principal", {"id": STR, "name": STR, "revoked": BOOL}),
        "token_renamed": _obj(
            "the renamed principal (ADR 0029)",
            {"id": STR, "name": STR, "previous_name": STR},
        ),
        "repository_list": _items("registered repositories", ANY_OBJ),
        "repository": _obj("a registered repository", {}),
        "repository_removed": _obj("the removed repository", {}),
        "harness_list": _items(
            "harnesses with their enable flags, credential and images",
            _obj(
                "a harness",
                {"name": STR, "enabled": BOOL, "enabled_by_administrator": BOOL},
                {
                    "enabled_by_configuration": BOOL,
                    "decided_by_administrator": BOOL,
                    "warning": STR,
                },
            ),
        ),
        "harness": _obj(
            "a harness after enable or disable",
            {"harness": STR, "enabled": BOOL},
            {
                "reason": STR,
                "session_compatibility": STR,
                "running_attempts": {"type": "integer"},
                "decided_by_administrator": BOOL,
                "warning": STR,
            },
        ),
        "credential_state": credential,
        "credential_report": _obj(
            "what a credential operation found and did",
            {"harness": STR},
            {"credential": credential, "shape": ANY_OBJ, "probe": ANY_OBJ},
        ),
        "credential_login": _obj(
            "a finished harness login",
            {"harness": STR},
            {"login": ANY_OBJ, "shape": ANY_OBJ},
        ),
        "harness_test": _obj(
            "a harness test: each step of a task's path, pass or fail in plain words",
            {"harness": STR, "ok": BOOL, "steps": {"type": "array", "items": ANY_OBJ}},
            {"failed_step": NSTR},
        ),
        "image_list": _obj(
            "worker images, and each harness's default, previous image and choices",
            {"items": {"type": "array", "items": _model(ImageView)}},
            {"defaults": {"type": "array", "items": ANY_OBJ}},
        ),
        "image_promotion": _obj(
            "one harness's default image after a promotion or rollback",
            {"harness": STR},
            {"promotion_state": STR},
        ),
        "provider_list": _items("execution providers and their health", ANY_OBJ),
        "github_status": _obj("the GitHub App's health", {"configured": BOOL}),
        "github_check": _obj("the per-repository token check", {}),
        "github_installations": _obj(
            "the repository picker: the App, its install link, whether it is public, "
            "an install-on-another-account link, and each installation's repositories "
            "grouped by account (crucible#266)",
            {"connected": BOOL, "installations": {"type": "array"}},
            {
                "app": ANY_OBJ,
                "app_public": BOOL,
                "install_url": NSTR,
                "install_target_url": NSTR,
                "error": NSTR,
            },
        ),
        "github_external_url": _obj(
            "the github.external_url setting: where GitHub sends the browser back when "
            "creating the App; null means the address the browser used (crucible#168)",
            {"setting": STR, "source": STR},
            {"url": NSTR, "updated_at": NSTR, "updated_by": NSTR, "reason": NSTR},
        ),
        "gateway": _obj(
            "the local gateway: its URL and where it comes from, whether a key is set, the "
            "last test in plain words, and the local model entries in force",
            {"endpoint_url": NSTR, "url_source": STR, "key_set": BOOL},
            {"last_test": STR, "models": {"type": "array"}},
        ),
        "gateway_test": _obj(
            "a gateway save or test: the gateway and the test's result in plain words",
            {"gateway": ANY_OBJ, "test": ANY_OBJ},
        ),
        "gateway_models": _obj(
            "the models the key can see, one row per model, beside the entries in force",
            {"models": {"type": "array"}, "reachable": BOOL},
            {"endpoint_url": NSTR, "error": NSTR},
        ),
        "gateway_models_saved": _obj(
            "the routing policy version the picks wrote and what they changed",
            {"enabled": {"type": "array"}, "routing_policy": ANY_OBJ},
            {"added": {"type": "array"}, "disabled_not_offered": {"type": "array"}},
        ),
        "audit_page": _obj(
            "one page of the audit log, oldest first",
            {"items": {"type": "array"}},
            {"next_cursor": {"type": ["integer", "null"]}},
        ),
        "exhaustion_list": _items(
            "reactive quota exhaustion marks",
            _obj("a mark", {"pool": STR, "active": BOOL}),
        ),
        "exhaustion_cleared": _obj("the cleared mark", {}),
        "local_endpoint": _obj(
            "the local model endpoint panel: its URL, model entries, and pool",
            {"endpoint_url": NSTR, "models": {"type": "array"}, "pool": ANY_OBJ},
            {"policy": ANY_OBJ, "routing_policy": ANY_OBJ},
        ),
        "kubernetes_egress": _obj(
            "the kubernetes.egress setting: the resolver's and an in-cluster local "
            "endpoint's namespace and pod labels, and where the values came from",
            {"setting": STR, "source": STR, "document": ANY_OBJ},
            {"settings_file": ANY_OBJ, "provider_enabled": BOOL},
        ),
        "kubernetes_timeouts": _obj(
            "the kubernetes.timeouts setting: the short roles' seconds from their Pod "
            "Running, its bounds, and where the value came from",
            {"setting": STR, "source": STR, "document": ANY_OBJ},
            {"settings_file": ANY_OBJ, "bounds": ANY_OBJ, "provider_enabled": BOOL},
        ),
        "command_timeout": _obj(
            "the per-command timeout bounds of the policy in force, in milliseconds "
            "(issue 128): a contract may narrow the default within them",
            {"policy": ANY_OBJ, "command_timeout_ms": ANY_OBJ},
            {"timeout_seconds": ANY_OBJ},
        ),
        "routing_preference": _obj(
            "the pool order routing tries first per tier, and when a model is demoted "
            "for failing blocking gates and probed again (ADR 0028)",
            {"tiers": ANY_OBJ, "rotation": ANY_OBJ, "pools": {"type": "array"}},
            {"policy": ANY_OBJ, "routing_policy": ANY_OBJ, "local_pools": {"type": "array"}},
        ),
        "gate_classes": _obj(
            "which pre-PR gates of the policy in force are advisory (a failure goes to the "
            "reviewer) and which block (a failure stops the task), ADR 0024",
            {"policy": ANY_OBJ, "advisory": {"type": "array"}, "blocking": {"type": "array"}},
            {
                "default": BOOL,
                "default_advisory": {"type": "array"},
                "always_blocking": {"type": "array"},
                "always_advisory": {"type": "array"},
                "note": STR,
            },
        ),
        "bootstrap_import": bootstrap,
        "bootstrap_import_list": _items("bootstrap imports", ANY_OBJ),
        # this command
        "schema": _obj(
            "this document",
            {
                "envelope": ANY_OBJ,
                "kinds": ANY_OBJ,
                "decision_kinds": {
                    "type": "array",
                    "description": "The closed list of accepted decision kinds (FDY-0509): the "
                    "same set that the server validates against for "
                    "`POST /tasks/{id}/decisions`.",
                    "items": {"type": "string"},
                },
            },
        ),
        "error": {"type": "null", "description": "a failure carries no data; see `error`"},
    }


def _decision_kinds() -> list[str]:
    """Return the accepted decision kinds (FDY-0509) from the same constant the validator uses."""
    from crucible.domain.decisions import ACCEPTED_DECISION_KINDS  # noqa: PLC0415

    return sorted(ACCEPTED_DECISION_KINDS)


def document() -> dict[str, Any]:
    return {
        "envelope": envelope_schema(),
        "kinds": kind_schemas(),
        "decision_kinds": _decision_kinds(),
    }
