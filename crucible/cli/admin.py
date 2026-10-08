"""`crucible admin` (25): every row of the operations table, through the same application
services the API calls. Local mode runs them in process against the configured
database and daemon; `--api-url` (or `--remote`) with a token runs the same operations
against a running API. Each prints one envelope (docs/client.md) on stdout; logs and the
interactive parts of a login go to stderr. `crucible-admin` is the same group behind a
deprecation line.

Two gaps are deliberate and named here rather than implied. 25 lists four CLI-only
operations; `migrate` and `token create` are below, the bootstrap import of 15 is the
`bootstrap` group below (C6, with its API under `/v1/import/bootstrap`), and the
portable `export` of 14 is not implemented yet. `credentials login` runs the harness's
own CLI: in local mode, directly on this host, and it needs that CLI installed there;
remotely, the API runs it in the promoted worker image and refuses with a clear reason
when none is available, rather than hanging.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import time
import urllib.parse
from collections.abc import Mapping
from typing import Any

from crucible.adapters.clock import SystemClock
from crucible.adapters.persistence.migrate import head_revision, upgrade
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.application.admin import (
    audit,
    bootstrap,
    credentials,
    gateway,
    github,
    github_manifest,
    harness_test,
    harnesses,
    images,
    login,
    routing,
    routing_preference,
)
from crucible.application.admin import gate_classes as gate_classes_admin
from crucible.application.admin import kubernetes as kubernetes_admin
from crucible.application.admin import limits as limits_admin
from crucible.application.admin import providers as providers_admin
from crucible.application.admin import repositories as repositories_admin
from crucible.application.admin import status as status_admin
from crucible.application.admin import (
    tokens as tokens_admin,
)
from crucible.application.admin.context import AdminContext
from crucible.application.admin.login import LoginRegistry
from crucible.application.auth import mint_token
from crucible.application.errors import ApplicationError
from crucible.application.first_run import FIRST_RUN_PREFIX
from crucible.application.queries import task_view
from crucible.application.republish import republish_task
from crucible.application.transitions import record_event
from crucible.cli.wiring import Wiring, first_run_delivery, wire
from crucible.client import next as nx
from crucible.client.config import ADMIN_TOKEN_ENV, TOKEN_ENV, require_remote, resolve
from crucible.client.envelope import ClientError, Result, UsageError
from crucible.client.http import Api
from crucible.contracts.api import (
    ExternalReviewAttestation,
    PublishRetryRequest,
    RepositoryRegistration,
)
from crucible.contracts.problem import problem_type
from crucible.domain.cluster_egress import parse_labels
from crucible.domain.entities import Principal, Role
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.logs import configure_logging
from crucible.ports.first_run import FirstRunDelivery
from crucible.settings import load_settings

CLI_PRINCIPAL = "crucible-admin"
TOKEN_ENVS = (ADMIN_TOKEN_ENV, TOKEN_ENV)

DESCRIPTION = """\
The operator's console (25): harness gates, credentials and login, image promotion,
tokens, repositories, routing, the local gateway, GitHub, the bootstrap import, audit.
Runs in process against the configured database by default; with --api-url URL (or
--remote, which takes the URL from CRUCIBLE_URL or the client configuration file) it
calls the running API with the token in CRUCIBLE_ADMIN_TOKEN, else CRUCIBLE_TOKEN. A
mutation takes an optional --reason, recorded in the audit log, before or after the verb:
`crucible admin harnesses disable codex --reason TEXT`. Revoking a token, removing a
repository or a credential, and committing a bootstrap import require one.
Output is one JSON envelope (see `crucible --help`)."""


def build_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """The admin group's arguments, unchanged from `crucible-admin`, onto `parser`."""
    parser.description = DESCRIPTION
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument(
        "--config", default=None, help="the server's TOML configuration file (local mode)"
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="remote mode: the API base; token from CRUCIBLE_ADMIN_TOKEN, else CRUCIBLE_TOKEN",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="remote mode with the base URL from CRUCIBLE_URL or the client configuration",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="a note recorded on a mutation; required to revoke a token, remove a "
        "repository or a credential, or commit a bootstrap import",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply migrations to head (local only)")
    sub.add_parser("status", help="the sanitized status document (25)")

    task = sub.add_parser("task", help="task recovery operations")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    republish = task_sub.add_parser("republish", help="retry a failed publication")
    republish.add_argument("task_id", help="the task in publish_failed")
    republish.add_argument("--reason", required=True, help="why the retry is safe now")

    token = sub.add_parser("token", help="token management")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_sub.add_parser("list", help="list principals without token values")
    create = token_sub.add_parser("create", help="create a principal and print its token once")
    create.add_argument("--principal", required=True, help="the new principal's name")
    create.add_argument("--role", required=True, choices=[r.value for r in Role])
    create.add_argument("--rotate", action="store_true", help="refused: revoke and create")
    revoke = token_sub.add_parser("revoke", help="disable a principal token")
    revoke.add_argument("principal_id", help="an id from `token list`")
    rename = token_sub.add_parser(
        "rename", help="rename a principal; its tasks and token follow it (ADR 0029)"
    )
    rename.add_argument("principal_id", help="an id from `token list`")
    rename.add_argument("name", help="the new name")

    for name in ("repository", "repositories"):
        repo = sub.add_parser(name, help="repository registry")
        repo_sub = repo.add_subparsers(dest="repo_command", required=True)
        repo_sub.add_parser("list", help="registered repositories")
        register = repo_sub.add_parser("register", help="register or update a repository")
        register.add_argument("--name", required=True)
        register.add_argument("--url", required=True, help="https://github.com/OWNER/REPO")
        register.add_argument("--default-branch", default="main")
        register.add_argument("--policy", default="default-software", help="the policy name")
        register.add_argument(
            "--installation-id", type=int, default=None, help="the GitHub App installation"
        )
        register.add_argument(
            "--attest-external-review-all-prs",
            action="store_true",
            help="attest the external reviewer reviews every pull request (23)",
        )
        register.add_argument("--attested-by", default=None, help="who attests")
        register.add_argument(
            "--private",
            action="store_true",
            help="clone with the GitHub App's read-only token; needs --installation-id (ADR 0019)",
        )
        remove = repo_sub.add_parser("remove", help="remove a registered repository")
        remove.add_argument("name")

    h = sub.add_parser("harnesses", help="list, enable, disable")
    h_sub = h.add_subparsers(dest="harness_command", required=True)
    h_sub.add_parser("list", help="harnesses, their enable flags, credentials and images")
    for verb in ("enable", "disable"):
        p = h_sub.add_parser(verb, help=f"{verb} a harness for new launches")
        p.add_argument("name", help="claude_code, codex, agy, hermes")
    test = h_sub.add_parser(
        "test", help="run the path a task takes: image, credential, worker, one model call"
    )
    test.add_argument("name", help="claude_code, codex, agy, hermes")

    c = sub.add_parser("credentials", help="status, set, validate, probe, login, rotate, remove")
    c_sub = c.add_subparsers(dest="credential_command", required=True)
    words = {
        "status": "presence, permissions, expiry class; never a value",
        "validate": "shape check of the auth files, then the bounded probe",
        "probe": "a bounded run of the hardened image",
        "remove": "retain and shred the credential",
    }
    for verb in ("status", "validate", "probe", "remove"):
        p = c_sub.add_parser(verb, help=words[verb])
        p.add_argument("--harness", required=True)
    login_cmd = c_sub.add_parser(
        "login",
        help="run the harness's own login (locally, or in the worker image remotely; "
        "see the module docstring)",
    )
    login_cmd.add_argument("--harness", required=True)
    login_cmd.add_argument(
        "--replace",
        action="store_true",
        help="retain and shred the existing credential first; refused without it when one is valid",
    )
    rotate = c_sub.add_parser("rotate", help="copy a prepared credential directory in")
    rotate.add_argument("--harness", required=True)
    rotate.add_argument(
        "--new-path",
        required=True,
        help="a prepared directory to copy in; it is left untouched and is yours to dispose of",
    )
    set_key = c_sub.add_parser("set", help="read an API key without placing it in argv")
    set_key.add_argument("--harness", default="hermes", choices=("hermes",))

    i = sub.add_parser("images", help="list, promote, roll back: per harness (ADR 0018)")
    i_sub = i.add_subparsers(dest="image_command", required=True)
    i_sub.add_parser(
        "list",
        help="worker images, and each harness's default and choices (ci-* tags are not listed)",
    )
    promote = i_sub.add_parser("promote", help="make an image one harness's default")
    promote.add_argument("digest", help="the image's digest or reference")
    promote.add_argument("--harness", required=True, help="the harness it becomes the default of")
    back = i_sub.add_parser("rollback", help="return a harness to its previous image")
    back.add_argument("--harness", required=True)

    pr = sub.add_parser("providers", help="execution providers")
    pr_sub = pr.add_subparsers(dest="provider_command", required=True)
    pr_sub.add_parser("status", help="each provider's health")

    g = sub.add_parser("github", help="the GitHub App")
    g_sub = g.add_subparsers(dest="github_command", required=True)
    g_sub.add_parser("status", help="the App's configuration and key")
    g_sub.add_parser("check", help="mint and discard a token per registered repository")
    g_sub.add_parser(
        "external-url",
        help="where GitHub sends the browser back when creating the App (crucible#168)",
    )
    set_url = g_sub.add_parser(
        "set-external-url",
        help="override that address; an empty --url clears it (the browser's address is used)",
    )
    set_url.add_argument("--url", required=True, help="e.g. https://hades.example.internal")
    g_sub.add_parser(
        "installations", help="the App's install link, installations and their repositories"
    )
    add_repo = g_sub.add_parser(
        "add-repository",
        help="register a repository an installation covers, with GitHub's default branch",
    )
    add_repo.add_argument("--installation-id", type=int, required=True)
    add_repo.add_argument("--repository", required=True, help="OWNER/NAME")
    add_repo.add_argument(
        "--name", default=None, help="the registered name (default: the repository's own)"
    )
    add_repo.add_argument("--policy", default="default-software", help="the policy name")
    add_repo.add_argument(
        "--attest-external-review-all-prs",
        action="store_true",
        help="attest the external reviewer reviews every pull request (23)",
    )
    add_repo.add_argument("--attested-by", default=None, help="who attests")

    gw = sub.add_parser(
        "gateway", help="the local gateway: its URL, the Hermes key, a test, and its models"
    )
    gw_sub = gw.add_subparsers(dest="gateway_command", required=True)
    gw_sub.add_parser("show", help="the gateway URL, whether a key is set, and the last test")
    gw_set = gw_sub.add_parser(
        "set", help="set the gateway URL (and the key with --key), then test both"
    )
    gw_set.add_argument(
        "--endpoint-url", required=True, help="the gateway's base URL, ending in /v1"
    )
    gw_set.add_argument(
        "--key",
        action="store_true",
        help="also read a new Hermes key from a hidden prompt or stdin, never argv",
    )
    gw_sub.add_parser("test", help="test the saved URL and key again")
    limits = gw_sub.add_parser(
        "limits",
        help=(
            "show the Hermes run limits, or set them with --max-turns, --context-length "
            "and --max-output-tokens"
        ),
    )
    limits.add_argument(
        "--max-turns", type=int, default=None, help="model turns one Hermes run may take"
    )
    limits.add_argument(
        "--context-length",
        type=int,
        default=None,
        help="the context window Hermes is told, in tokens; 0 lets Hermes find it",
    )
    limits.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="the response allowance the gateway reserves out of the window, in tokens",
    )
    gw_sub.add_parser("models", help="the models the key can see, beside the entries in force")
    pick = gw_sub.add_parser(
        "pick", help="enable or disable gateway models; writes a new routing policy version"
    )
    pick.add_argument("--harness", choices=("hermes", "codex"), default="hermes")
    pick.add_argument("--enable", action="append", default=[], metavar="MODEL")
    pick.add_argument("--disable", action="append", default=[], metavar="MODEL")
    pick.add_argument(
        "--thinking",
        action="append",
        default=[],
        metavar="MODEL",
        help="thinking on by default for this picked model (off for the others)",
    )
    pick.add_argument(
        "--capability",
        action="append",
        default=[],
        metavar="MODEL=CAPABILITY",
        help="small, mid or frontier (a new model defaults to mid)",
    )
    pick.add_argument("--max-concurrency", type=int, default=None, help="the pool's limit")

    a = sub.add_parser("audit", help="the audit log")
    a_sub = a.add_subparsers(dest="audit_command", required=True)
    tail = a_sub.add_parser("tail", help="administrative events, oldest first")
    tail.add_argument("--cursor", type=int, default=None, help="a next_cursor from a page")
    tail.add_argument("--limit", type=int, default=50)

    route = sub.add_parser("routing", help="local endpoint and reactive quota exhaustion")
    route_sub = route.add_subparsers(dest="routing_command", required=True)
    route_sub.add_parser("exhaustion", help="the exhaustion marks")
    clear = route_sub.add_parser("clear-exhaustion", help="clear a pool's exhaustion mark")
    clear.add_argument("pool")
    route_sub.add_parser("local-endpoint")
    local_set = route_sub.add_parser("set-local-endpoint")
    local_set.add_argument("--endpoint-url", required=True)
    local_set.add_argument("--model", default="coder")
    local_set.add_argument("--harness", default=None)
    state = local_set.add_mutually_exclusive_group(required=True)
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    local_set.add_argument("--enable-thinking", action="store_true")
    local_set.add_argument("--max-concurrency", type=int, default=4)
    route_sub.add_parser(
        "preference", help="the pool order per tier and the demotion settings in force"
    )
    pref_set = route_sub.add_parser(
        "set-preference",
        help="write a routing version with this pool order or these demotion settings",
    )
    pref_set.add_argument(
        "--tier",
        action="append",
        default=[],
        metavar="TIER=POOL,POOL",
        help="the pools routing tries first for TIER, in order; TIER= for none, "
        "TIER=default for the default; repeat per tier",
    )
    feedback = pref_set.add_mutually_exclusive_group()
    feedback.add_argument("--quality-feedback", dest="quality_feedback", action="store_true")
    feedback.add_argument("--no-quality-feedback", dest="quality_feedback", action="store_false")
    pref_set.set_defaults(quality_feedback=None)
    pref_set.add_argument("--quality-window", type=int, help="attempts judged per model")
    pref_set.add_argument(
        "--demote-failure-percent", type=int, help="blocking-failure percentage that demotes"
    )
    pref_set.add_argument(
        "--demote-min-sample", type=int, help="judged attempts needed before demoting (2+)"
    )
    pref_set.add_argument(
        "--probe-after-minutes", type=int, help="minutes before a demoted model is probed"
    )

    lim = sub.add_parser("limits", help="policy limits edited in place (issue 128)")
    lim_sub = lim.add_subparsers(dest="limits_command", required=True)
    lim_sub.add_parser(
        "command-timeout", help="the per-command timeout bounds of the policy in force"
    )
    timeout_set = lim_sub.add_parser(
        "set-command-timeout",
        help="write a new policy version with these per-command timeout bounds",
    )
    timeout_set.add_argument("--min", type=int, help="milliseconds; omitted keeps the value")
    timeout_set.add_argument("--max", type=int, help="milliseconds; omitted keeps the value")
    timeout_set.add_argument(
        "--default",
        type=int,
        help="milliseconds, which a contract may narrow; omitted keeps the value",
    )

    gate = sub.add_parser("gates", help="which pre-PR gates block and which are advisory")
    gate_sub = gate.add_subparsers(dest="gates_command", required=True)
    gate_sub.add_parser("advisory", help="the advisory and blocking gates of the policy in force")
    advisory_set = gate_sub.add_parser(
        "set-advisory",
        help="write a new policy version whose advisory gates are exactly these",
    )
    advisory_set.add_argument(
        "--gate",
        action="append",
        default=[],
        help="a pre-PR gate to make advisory; repeat for each. None given: every gate blocks",
    )

    kube = sub.add_parser(
        "kubernetes",
        help="the Kubernetes provider's cluster egress selectors (26, #91) and role timeout",
    )
    kube_sub = kube.add_subparsers(dest="kubernetes_command", required=True)
    kube_sub.add_parser("timeouts", help="the kubernetes.timeouts setting in force and its source")
    timeouts_set = kube_sub.add_parser(
        "set-timeouts",
        help="update kubernetes.timeouts: short-role timeouts and the pre-launch retry budget",
    )
    timeouts_set.add_argument(
        "--role-seconds",
        type=int,
        required=True,
        help="the bundle verifier's, the cleaner's and the publisher claim Job's seconds",
    )
    timeouts_set.add_argument(
        "--api-retry-seconds",
        type=int,
        help="pre-launch API transport retry budget, 1 to 600 seconds; "
        "omitted preserves the current value",
    )
    kube_sub.add_parser("egress", help="the kubernetes.egress setting in force and its source")
    egress_set = kube_sub.add_parser(
        "set-egress",
        help="replace kubernetes.egress: the resolver's pods and an in-cluster local endpoint",
    )
    egress_set.add_argument(
        "--dns-namespace",
        default="kube-system",
        help="the cluster resolver's namespace; empty for its service address alone",
    )
    egress_set.add_argument(
        "--dns-labels",
        default="k8s-app=kube-dns",
        help="the resolver's pod labels, key=value[,key=value]",
    )
    egress_set.add_argument(
        "--endpoint-namespace",
        default="",
        help="an in-cluster local endpoint's namespace; empty when it is outside the cluster",
    )
    egress_set.add_argument(
        "--endpoint-labels", default="", help="its pod labels, key=value[,key=value]"
    )
    egress_set.add_argument(
        "--endpoint-port",
        type=int,
        default=0,
        help="its pods' port; 0 for the endpoint URL's own port",
    )

    b = sub.add_parser(
        "bootstrap",
        help="the bootstrap ledger handoff (15): submit, show, list, commit, discard",
    )
    b_sub = b.add_subparsers(dest="bootstrap_command", required=True)
    submit = b_sub.add_parser(
        "submit",
        help="validate a BootstrapExportV1 bundle and write it as a verified import",
    )
    submit.add_argument("--file", required=True, help="the crucible.json foundry-ledger exported")
    submit.add_argument(
        "--owner",
        default=None,
        help="the principal the imported tasks belong to (default: this CLI's principal)",
    )
    show = b_sub.add_parser("show", help="the verification report of one import")
    show.add_argument("import_id")
    b_sub.add_parser("list", help="every import")
    commit = b_sub.add_parser("commit", help="make a verified import authoritative")
    commit.add_argument("import_id")
    discard = b_sub.add_parser(
        "discard", help="withdraw a verified import that will not be committed (ADR 0029)"
    )
    discard.add_argument("import_id")
    _reason_after_the_verb(parser)
    return parser


def _reason_after_the_verb(parser: argparse.ArgumentParser) -> None:
    """Every verb also takes `--reason` after it, where a caller appends an optional flag
    (`next`'s `optional`). SUPPRESS keeps a reason given before the verb when none
    follows it."""
    for item in parser._actions:
        if isinstance(item, argparse._SubParsersAction):
            for child in item.choices.values():
                _reason_after_the_verb(child)
            return
    if "--reason" not in parser._option_string_actions:
        parser.add_argument(
            "--reason", default=argparse.SUPPRESS, help="a note recorded on a mutation"
        )


def _read_code() -> str:
    """The login code a person pastes. The prompt goes to stderr and the code is read
    from stdin, so stdout carries the envelope alone even when stdin is not a terminal."""
    print("paste the code: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().rstrip("\r\n")


def _read_bundle(path: str) -> Any:
    """The bundle file, parsed and nothing else: validation is the service's."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read a bundle from {path}: {exc}") from None


def _not_configured() -> ClientError:
    return ClientError(
        "admin-not-configured",
        "the administrative surface is not configured",
        hint="set the [admin] section of the server configuration (25)",
    )


def application_error(exc: ApplicationError) -> ClientError:
    """A local refusal in the shape the API would have sent it (RFC 9457)."""
    problem = {
        "type": problem_type(exc.slug),
        "title": exc.title,
        "status": exc.status,
        "detail": exc.detail,
        "errors": exc.errors or [],
    }
    return ClientError(exc.slug, exc.detail or exc.title, problem=problem, status=exc.status)


def _timeouts(args: argparse.Namespace) -> dict[str, int]:
    document = {"role_timeout_seconds": args.role_seconds}
    if args.api_retry_seconds is not None:
        document["api_retry_seconds"] = args.api_retry_seconds
    return document


def _egress(args: argparse.Namespace) -> dict[str, Any]:
    """`set-egress` flags as the `kubernetes.egress` document the service checks."""
    try:
        return {
            "dns": {
                "namespace": args.dns_namespace,
                "pod_labels": parse_labels(args.dns_labels),
            },
            "local_endpoint": {
                "namespace": args.endpoint_namespace,
                "pod_labels": parse_labels(args.endpoint_labels),
                "port": args.endpoint_port,
            },
        }
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def _preference_args(
    args: argparse.Namespace,
) -> tuple[dict[str, list[str] | None], dict[str, Any]]:
    """`set-preference` flags as the tiers and rotation settings the service checks."""
    tiers: dict[str, list[str] | None] = {}
    for item in args.tier:
        name, sep, pools = item.partition("=")
        if not sep or not name.strip():
            raise UsageError(f"--tier takes TIER=POOL,POOL, not {item!r}")
        tiers[name.strip()] = routing_preference.parse_pool_order(pools)
    rotation = {
        key: value
        for key, value in (
            ("quality_feedback", args.quality_feedback),
            ("quality_window", args.quality_window),
            ("demote_failure_percent", args.demote_failure_percent),
            ("demote_min_sample", args.demote_min_sample),
            ("probe_after_minutes", args.probe_after_minutes),
        )
        if value is not None
    }
    if not tiers and not rotation:
        raise UsageError("name at least one --tier or rotation setting to change")
    return tiers, rotation


def _read_api_key() -> str:
    """Read a key from a hidden terminal prompt or stdin, never from argv."""
    return (
        getpass.getpass("LiteLLM virtual key: ")
        if sys.stdin.isatty()
        else sys.stdin.readline().rstrip("\r\n")
    )


def _picks(args: argparse.Namespace) -> list[dict[str, Any]]:
    """`gateway pick` flags as the model picks the service checks."""
    capabilities: dict[str, str] = {}
    for item in args.capability:
        model, sep, value = item.partition("=")
        if not sep or not model or not value:
            raise UsageError(f"--capability takes MODEL=CAPABILITY, not {item!r}")
        capabilities[model] = value
    named = list(dict.fromkeys([*args.enable, *args.disable]))
    stray = sorted((set(args.thinking) | set(capabilities)) - set(named))
    if stray:
        raise UsageError(f"{stray} must also be named with --enable or --disable")
    if not named:
        raise UsageError("name at least one model with --enable or --disable")
    return [
        {
            "id": model,
            **(
                {"codex_enabled": model in args.enable and model not in args.disable}
                if args.harness == "codex"
                else {"enabled": model in args.enable and model not in args.disable}
            ),
            "enable_thinking": model in args.thinking,
            "capability": capabilities.get(model),
        }
        for model in named
    ]


def _add_repository_body(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "installation_id": args.installation_id,
        "repository": args.repository,
        "name": args.name,
        "policy_name": args.policy,
        "attested_all_prs": args.attest_external_review_all_prs,
        "attested_by": args.attested_by,
    }


# ----- remote mode -------------------------------------------------------------


def _remote(args: argparse.Namespace, remote: Api) -> Any:
    reason = {"reason": args.reason or ""}
    command = args.command
    if command == "status":
        return remote.call("GET", "/v1/admin/status")
    if command == "task":
        return remote.call("POST", f"/v1/tasks/{args.task_id}/republish", {"reason": args.reason})
    if command == "token":
        if args.token_command == "list":
            return remote.call("GET", "/v1/admin/tokens")
        if args.token_command == "revoke":
            return remote.call("POST", f"/v1/admin/tokens/{args.principal_id}/revoke", reason)
        if args.token_command == "rename":
            return remote.call(
                "POST",
                f"/v1/admin/tokens/{args.principal_id}/rename",
                {**reason, "name": args.name},
            )
        if args.rotate:
            raise UsageError("remote token rotation is not supported; revoke and create")
        return remote.call(
            "POST", "/v1/admin/tokens", {**reason, "name": args.principal, "role": args.role}
        )
    if command == "harnesses":
        if args.harness_command == "list":
            return remote.call("GET", "/v1/admin/harnesses")
        if args.harness_command == "test":
            return _remote_harness_test(args, remote, reason)
        return remote.call(
            "POST", f"/v1/admin/harnesses/{args.name}/{args.harness_command}", reason
        )
    if command == "credentials":
        verb = args.credential_command
        if verb == "status":
            return remote.call("GET", f"/v1/admin/credentials/{args.harness}")
        if verb == "set":
            return remote.call(
                "POST",
                f"/v1/admin/credentials/{args.harness}/set",
                {**reason, "api_key": _read_api_key()},
            )
        if verb == "rotate":
            return remote.call(
                "POST",
                f"/v1/admin/credentials/{args.harness}/rotate",
                {**reason, "new_path": args.new_path},
            )
        if verb == "login":
            return _remote_login(args, remote, reason)
        return remote.call("POST", f"/v1/admin/credentials/{args.harness}/{verb}", reason)
    if command == "images":
        if args.image_command == "list":
            return remote.call("GET", "/v1/admin/images")
        if args.image_command == "rollback":
            return remote.call(
                "POST", "/v1/admin/images/rollback", {**reason, "harness": args.harness}
            )
        return remote.call(
            "POST", f"/v1/admin/images/{args.digest}/promote", {**reason, "harness": args.harness}
        )
    if command == "providers":
        return remote.call("GET", "/v1/admin/providers")
    if command == "github":
        if args.github_command == "status":
            return remote.call("GET", "/v1/admin/github")
        if args.github_command == "installations":
            return remote.call("GET", "/v1/admin/github/installations")
        if args.github_command == "external-url":
            return remote.call("GET", "/v1/admin/github/external-url")
        if args.github_command == "set-external-url":
            return remote.call(
                "POST", "/v1/admin/github/external-url", {**reason, "url": args.url or None}
            )
        if args.github_command == "add-repository":
            return remote.call(
                "POST", "/v1/admin/github/repositories", {**reason, **_add_repository_body(args)}
            )
        return remote.call("POST", "/v1/admin/github/check", reason)
    if command == "gateway":
        verb = args.gateway_command
        if verb == "show":
            return remote.call("GET", "/v1/admin/gateway")
        if verb == "models":
            return remote.call("GET", "/v1/admin/gateway/models")
        if verb == "test":
            return remote.call("POST", "/v1/admin/gateway/test", reason)
        if verb == "limits":
            current = remote.call("GET", "/v1/admin/gateway/hermes-limits")
            if _limits_unset(args):
                return current
            return remote.call(
                "POST",
                "/v1/admin/gateway/hermes-limits",
                {**reason, **_limits(args, current)},
            )
        if verb == "pick":
            return remote.call(
                "POST",
                "/v1/admin/gateway/models",
                {**reason, "models": _picks(args), "max_concurrency": args.max_concurrency},
            )
        return remote.call(
            "POST",
            "/v1/admin/gateway",
            {
                **reason,
                "endpoint_url": args.endpoint_url,
                "api_key": _read_api_key() if args.key else None,
            },
        )
    if command == "audit":
        query = f"?limit={args.limit}" + (f"&cursor={args.cursor}" if args.cursor else "")
        return remote.call("GET", "/v1/admin/audit" + query)
    if command == "routing":
        if args.routing_command == "exhaustion":
            return remote.call("GET", "/v1/admin/routing/exhaustion")
        if args.routing_command == "clear-exhaustion":
            return remote.call("POST", f"/v1/admin/routing/exhaustion/{args.pool}/clear", reason)
        if args.routing_command == "local-endpoint":
            return remote.call("GET", "/v1/admin/routing/local-endpoint")
        if args.routing_command == "preference":
            return remote.call("GET", "/v1/admin/routing/preference")
        if args.routing_command == "set-preference":
            tiers, rotation = _preference_args(args)
            return remote.call(
                "POST",
                "/v1/admin/routing/preference",
                {**reason, "tiers": tiers, "rotation": rotation},
            )
        return remote.call(
            "POST",
            "/v1/admin/routing/local-endpoint",
            {
                **reason,
                "endpoint_url": args.endpoint_url,
                "models": [
                    {
                        **(
                            {"model": args.model, "harness": args.harness}
                            if args.harness
                            else {"id": args.model}
                        ),
                        "enabled": args.enable,
                        "enable_thinking": args.enable_thinking,
                    }
                ],
                "max_concurrency": args.max_concurrency,
            },
        )
    if command == "gates":
        if args.gates_command == "advisory":
            return remote.call("GET", "/v1/admin/gates/advisory")
        return remote.call(
            "POST", "/v1/admin/gates/advisory", {**reason, "advisory": list(args.gate)}
        )
    if command == "limits":
        if args.limits_command == "command-timeout":
            return remote.call("GET", "/v1/admin/limits/command-timeout")
        return remote.call(
            "POST",
            "/v1/admin/limits/command-timeout",
            {
                **reason,
                **{
                    key: value
                    for key, value in (
                        ("min", args.min),
                        ("max", args.max),
                        ("default", args.default),
                    )
                    if value is not None
                },
            },
        )
    if command == "kubernetes":
        if args.kubernetes_command == "egress":
            return remote.call("GET", "/v1/admin/kubernetes/egress")
        if args.kubernetes_command == "timeouts":
            return remote.call("GET", "/v1/admin/kubernetes/timeouts")
        if args.kubernetes_command == "set-timeouts":
            return remote.call(
                "POST",
                "/v1/admin/kubernetes/timeouts",
                {**reason, **_timeouts(args)},
            )
        return remote.call("POST", "/v1/admin/kubernetes/egress", {**reason, **_egress(args)})
    if command == "bootstrap":
        verb = args.bootstrap_command
        if verb == "submit":
            query = "?" + urllib.parse.urlencode(
                {k: v for k, v in (("reason", args.reason), ("owner", args.owner)) if v}
            )
            return remote.call("POST", "/v1/import/bootstrap" + query, _read_bundle(args.file))
        if verb == "show":
            return remote.call("GET", f"/v1/import/bootstrap/{args.import_id}")
        if verb == "list":
            return remote.call("GET", "/v1/import/bootstrap")
        if verb == "discard":
            return remote.call("POST", f"/v1/import/bootstrap/{args.import_id}/discard", reason)
        return remote.call("POST", f"/v1/import/bootstrap/{args.import_id}/commit", reason)
    if command in ("repository", "repositories"):
        if args.repo_command == "list":
            return remote.call("GET", "/v1/admin/repositories")
        if args.repo_command == "remove":
            return remote.call("DELETE", f"/v1/admin/repositories/{args.name}", reason)
        return remote.call(
            "PUT",
            f"/v1/admin/repositories/{args.name}",
            {
                **reason,
                "url": args.url,
                "default_branch": args.default_branch,
                "policy_name": args.policy,
                "installation_id": args.installation_id,
                "attested_all_prs": args.attest_external_review_all_prs,
                "attested_by": args.attested_by,
                "private": args.private,
            },
        )
    raise UsageError(f"{command} is CLI-only and runs in local mode; drop --api-url")


def _remote_login(args: argparse.Namespace, remote: Api, reason: dict[str, str]) -> Any:
    started = remote.call(
        "POST",
        f"/v1/admin/credentials/{args.harness}/login",
        {**reason, "replace": bool(getattr(args, "replace", False))},
    )
    print(started["window"], file=sys.stderr)
    shown: set[str] = set()
    while True:
        state = remote.call("GET", f"/v1/admin/credentials/{args.harness}/login")
        for line in state.get("output_tail", []):
            if line not in shown:
                shown.add(line)
                print(line, file=sys.stderr)
        if state["state"] == "waiting_for_code":
            code = _read_code()
            remote.call(
                "POST",
                f"/v1/admin/credentials/{args.harness}/login/code",
                {**reason, "code": code},
            )
        elif state["state"] in ("finished", "failed"):
            break
        time.sleep(1)
    return remote.call("POST", f"/v1/admin/credentials/{args.harness}/login/finish", reason)


# issue 147: the service runs a harness test in the background; the CLI waits for the
# result, polling the stored test every so often for at most so long.
HARNESS_TEST_POLL_SECONDS = 2.0
HARNESS_TEST_WAIT_SECONDS = 900.0


def _remote_harness_test(args: argparse.Namespace, remote: Api, reason: dict[str, str]) -> Any:
    """Start the test and wait for its result, which is what the operator asked for. A
    service that answers the POST with the result itself is printed as it is."""
    started = remote.call("POST", f"/v1/admin/harnesses/{args.name}/test", reason)
    if not harness_test.is_running(started):
        return started
    print(f"{args.name}: the test is running; waiting for the result", file=sys.stderr)
    deadline = time.monotonic() + HARNESS_TEST_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(HARNESS_TEST_POLL_SECONDS)
        latest = remote.call("GET", f"/v1/admin/harnesses/{args.name}/test")
        if harness_test.is_result_of(latest, started):
            return latest
    raise ClientError(
        "timeout",
        f"the {args.name} test did not finish within {HARNESS_TEST_WAIT_SECONDS:g} s",
        hint="`crucible admin harnesses list` shows the result once it lands",
    )


# ----- local mode --------------------------------------------------------------


def _local_login(args: argparse.Namespace, wiring: Wiring, admin: AdminContext) -> Any:
    registry = LoginRegistry()
    with wiring.ctx.uow_factory() as uow:
        started = login.start_login(
            admin,
            uow,
            registry,
            principal=CLI_PRINCIPAL,
            harness=args.harness,
            reason=args.reason,
            replace=getattr(args, "replace", False),
        )
        uow.commit()
    print(started["window"], file=sys.stderr)
    session = registry.get(args.harness)
    assert session is not None
    shown = 0
    while session.state not in ("finished", "failed"):
        for line in session.lines[shown:]:
            print(line, file=sys.stderr)
        shown = len(session.lines)
        if session.state == "waiting_for_code":
            session.submit_code(_read_code())
        time.sleep(0.5)
    for line in session.lines[shown:]:
        print(line, file=sys.stderr)
    with wiring.ctx.uow_factory() as uow:
        result = login.finish_login(
            admin, uow, registry, principal=CLI_PRINCIPAL, harness=args.harness, reason=args.reason
        )
        uow.commit()
    return result


def _local(args: argparse.Namespace, wiring: Wiring) -> Any:
    admin = wiring.admin
    if admin is None:
        raise _not_configured()
    principal = CLI_PRINCIPAL
    command = args.command
    if command == "status":
        with wiring.ctx.uow_factory() as uow:
            return asyncio.run(status_admin.status(admin, uow))
    if command == "task":
        with wiring.ctx.uow_factory() as uow:
            task = republish_task(
                uow,
                wiring.ctx.clock,
                principal=Principal(
                    id=CLI_PRINCIPAL,
                    name=CLI_PRINCIPAL,
                    role=Role.ADMIN,
                    created_at=wiring.ctx.clock.now(),
                ),
                task_id=args.task_id,
                request=PublishRetryRequest(reason=args.reason),
            )
            uow.commit()
            return task_view(uow, task.id).model_dump(mode="json")
    if command == "harnesses":
        with wiring.ctx.uow_factory() as uow:
            if args.harness_command == "list":
                found = asyncio.run(harnesses.list_images(admin))
                return {"items": harnesses.list_harnesses(admin, uow, [i for _, i in found])}
            if args.harness_command == "test":
                tested = asyncio.run(
                    harness_test.test_harness(
                        admin, uow, principal=principal, harness=args.name, reason=args.reason
                    )
                )
                uow.commit()
                return tested
            result = harnesses.set_enabled(
                admin,
                uow,
                principal=principal,
                harness=args.name,
                enabled=args.harness_command == "enable",
                reason=args.reason,
            )
            uow.commit()
            return result
    if command == "credentials":
        return _local_credentials(args, wiring, admin, principal)
    if command == "images":
        with wiring.ctx.uow_factory() as uow:
            if args.image_command == "list":
                return {
                    "items": asyncio.run(images.list_all(admin, uow)),
                    "defaults": asyncio.run(images.defaults(admin, uow)),
                }
            if args.image_command == "rollback":
                result = asyncio.run(
                    images.rollback(
                        admin, uow, principal=principal, harness=args.harness, reason=args.reason
                    )
                )
            else:
                result = asyncio.run(
                    images.promote(
                        admin,
                        uow,
                        principal=principal,
                        harness=args.harness,
                        digest=args.digest,
                        reason=args.reason,
                    )
                )
            uow.commit()
            return result
    if command == "providers":
        return {"items": asyncio.run(providers_admin.providers_status(admin))}
    if command == "github":
        with wiring.ctx.uow_factory() as uow:
            verb = args.github_command
            if verb == "status":
                return github.status(admin, uow)
            if verb == "installations":
                return github.apps_view(admin, uow)
            if verb == "external-url":
                return github_manifest.external_url_view(admin, uow)
            if verb == "set-external-url":
                result = github_manifest.save_external_url(
                    admin, uow, principal=principal, url=args.url or None, reason=args.reason
                )
                uow.commit()
                return result
            if verb == "add-repository":
                body = _add_repository_body(args)
                result = github.add_repository(
                    admin,
                    uow,
                    principal=principal,
                    installation_id=body["installation_id"],
                    repository=body["repository"],
                    name=body["name"],
                    policy_name=body["policy_name"],
                    attested_all_prs=body["attested_all_prs"],
                    attested_by=body["attested_by"],
                    reason=args.reason,
                )
            else:
                result = github.check(admin, uow, principal=principal, reason=args.reason)
            uow.commit()
            return result
    if command == "gateway":
        return _local_gateway(args, wiring, admin)
    if command == "audit":
        with wiring.ctx.uow_factory() as uow:
            return audit.tail(uow, cursor=args.cursor, limit=args.limit)
    if command == "routing":
        with wiring.ctx.uow_factory() as uow:
            if args.routing_command == "exhaustion":
                return routing.list_exhaustions(admin, uow)
            if args.routing_command == "clear-exhaustion":
                result = routing.clear_exhaustion(
                    admin, uow, principal=principal, pool=args.pool, reason=args.reason
                )
                uow.commit()
                return result
            if args.routing_command == "local-endpoint":
                return routing.local_endpoint_view(uow)
            if args.routing_command == "preference":
                return routing_preference.preference_view(uow)
            if args.routing_command == "set-preference":
                tiers, rotation = _preference_args(args)
                result = routing_preference.save_preference(
                    admin,
                    uow,
                    principal=Principal(
                        id=CLI_PRINCIPAL,
                        name=CLI_PRINCIPAL,
                        role=Role.ADMIN,
                        created_at=wiring.ctx.clock.now(),
                    ),
                    tiers=tiers,
                    rotation=rotation,
                    reason=args.reason,
                )
                uow.commit()
                return result
            result = routing.save_local_endpoint(
                admin,
                uow,
                principal=Principal(
                    id=CLI_PRINCIPAL,
                    name=CLI_PRINCIPAL,
                    role=Role.ADMIN,
                    created_at=wiring.ctx.clock.now(),
                ),
                endpoint_url=args.endpoint_url,
                models=[
                    {
                        **(
                            {"model": args.model, "harness": args.harness}
                            if args.harness
                            else {"id": args.model}
                        ),
                        "enabled": args.enable,
                        "enable_thinking": args.enable_thinking,
                    }
                ],
                max_concurrency=args.max_concurrency,
                reason=args.reason,
            )
            uow.commit()
            return result
    if command == "limits":
        with wiring.ctx.uow_factory() as uow:
            if args.limits_command == "command-timeout":
                return limits_admin.command_timeout_view(uow)
            result = limits_admin.save_command_timeout(
                admin,
                uow,
                principal=Principal(
                    id=CLI_PRINCIPAL,
                    name=CLI_PRINCIPAL,
                    role=Role.ADMIN,
                    created_at=wiring.ctx.clock.now(),
                ),
                minimum=args.min,
                maximum=args.max,
                default=args.default,
                reason=args.reason,
            )
            uow.commit()
            return result
    if command == "gates":
        with wiring.ctx.uow_factory() as uow:
            if args.gates_command == "advisory":
                return gate_classes_admin.gate_classes_view(uow)
            result = gate_classes_admin.save_gate_classes(
                admin,
                uow,
                principal=Principal(
                    id=CLI_PRINCIPAL,
                    name=CLI_PRINCIPAL,
                    role=Role.ADMIN,
                    created_at=wiring.ctx.clock.now(),
                ),
                advisory=list(args.gate),
                reason=args.reason,
            )
            uow.commit()
            return result
    if command == "kubernetes":
        with wiring.ctx.uow_factory() as uow:
            if args.kubernetes_command == "egress":
                return kubernetes_admin.egress_view(admin, uow)
            if args.kubernetes_command == "timeouts":
                return kubernetes_admin.timeouts_view(admin, uow)
            if args.kubernetes_command == "set-timeouts":
                result = kubernetes_admin.save_timeouts(
                    admin,
                    uow,
                    principal=principal,
                    document=_timeouts(args),
                    reason=args.reason,
                )
            else:
                result = kubernetes_admin.save_egress(
                    admin, uow, principal=principal, document=_egress(args), reason=args.reason
                )
            uow.commit()
            return result
    if command == "bootstrap":
        return _local_bootstrap(args, wiring, admin, principal)
    raise UsageError(f"unknown command: {command}")


def _limits_unset(args: argparse.Namespace) -> bool:
    """`gateway limits` with no flag only shows the limits."""
    return args.max_turns is None and args.context_length is None and args.max_output_tokens is None


def _limits(args: argparse.Namespace, current: Mapping[str, Any]) -> dict[str, int]:
    """`gateway limits` flags over the limits in force: a flag not given keeps its value."""

    def pick(name: str) -> int:
        value = getattr(args, name)
        return int(value) if value is not None else int(current[name])

    return {name: pick(name) for name in ("max_turns", "context_length", "max_output_tokens")}


def _local_gateway(args: argparse.Namespace, wiring: Wiring, admin: AdminContext) -> Any:
    principal = Principal(
        id=CLI_PRINCIPAL, name=CLI_PRINCIPAL, role=Role.ADMIN, created_at=wiring.ctx.clock.now()
    )
    verb = args.gateway_command
    with wiring.ctx.uow_factory() as uow:
        if verb == "show":
            return gateway.gateway_view(admin, uow)
        if verb == "models":
            return asyncio.run(gateway.models_view(admin, uow))
        if verb == "limits" and _limits_unset(args):
            return gateway.hermes_limits_view(uow)
        if verb == "test":
            result = asyncio.run(
                gateway.test_gateway(admin, uow, principal=principal, reason=args.reason)
            )
        elif verb == "limits":
            result = gateway.save_hermes_limits(
                admin,
                uow,
                principal=principal,
                reason=args.reason,
                **_limits(args, gateway.hermes_limits_view(uow)),
            )
        elif verb == "pick":
            result = asyncio.run(
                gateway.save_models(
                    admin,
                    uow,
                    principal=principal,
                    models=_picks(args),
                    max_concurrency=args.max_concurrency,
                    reason=args.reason,
                )
            )
        else:
            result = asyncio.run(
                gateway.save_gateway(
                    admin,
                    uow,
                    principal=principal,
                    endpoint_url=args.endpoint_url,
                    api_key=_read_api_key() if args.key else None,
                    reason=args.reason,
                )
            )
        uow.commit()
        return result


def _local_credentials(
    args: argparse.Namespace, wiring: Wiring, admin: AdminContext, principal: str
) -> Any:
    verb = args.credential_command
    if verb == "login":
        return _local_login(args, wiring, admin)
    with wiring.ctx.uow_factory() as uow:
        if verb == "status":
            return credentials.state_view(admin, uow, args.harness)
        if verb == "set":
            result = asyncio.run(
                credentials.set_api_key(
                    admin,
                    uow,
                    principal=principal,
                    harness=args.harness,
                    api_key=_read_api_key(),
                    reason=args.reason,
                )
            ).as_dict()
        elif verb == "validate":
            result = asyncio.run(
                credentials.validate(
                    admin, uow, principal=principal, harness=args.harness, reason=args.reason
                )
            ).as_dict()
        elif verb == "probe":
            result = asyncio.run(
                credentials.probe(
                    admin, uow, principal=principal, harness=args.harness, reason=args.reason
                )
            ).as_dict()
        elif verb == "rotate":
            result = credentials.rotate(
                admin,
                uow,
                principal=principal,
                harness=args.harness,
                new_path=args.new_path,
                reason=args.reason,
            ).as_dict()
        else:
            result = credentials.remove(
                admin, uow, principal=principal, harness=args.harness, reason=args.reason
            ).as_dict()
        uow.commit()
        return result


def _local_bootstrap(
    args: argparse.Namespace, wiring: Wiring, admin: AdminContext, principal: str
) -> Any:
    """15 through the same services the API calls. The bundle file is read here and
    handed over parsed; every rule of step 2 is the service's, on both entry points."""
    verb = args.bootstrap_command
    if verb == "submit":
        bundle = _read_bundle(args.file)
        with wiring.ctx.uow_factory() as uow:
            report, _created = bootstrap.submit(
                admin,
                uow,
                principal=principal,
                bundle=bundle,
                reason=args.reason,
                owner=args.owner,
            )
            uow.commit()
        return report
    with wiring.ctx.uow_factory() as uow:
        if verb == "show":
            return bootstrap.show(uow, args.import_id)
        if verb == "list":
            return {"items": bootstrap.list_imports(uow)}
        if verb == "discard":
            discarded = bootstrap.discard(
                admin, uow, principal=principal, import_id=args.import_id, reason=args.reason
            )
            uow.commit()
            return discarded
        result = bootstrap.commit(
            admin, uow, principal=principal, import_id=args.import_id, reason=args.reason
        )
        uow.commit()
        return result


def _token(args: argparse.Namespace, wiring: Wiring) -> Any:
    if wiring.admin is None:
        raise _not_configured()
    with wiring.ctx.uow_factory() as uow:
        if args.token_command == "list":
            return {"items": tokens_admin.list_principals(uow)}
        if args.token_command == "revoke":
            result = tokens_admin.revoke(
                wiring.admin,
                uow,
                principal=CLI_PRINCIPAL,
                principal_id=args.principal_id,
                reason=args.reason,
            )
            uow.commit()
            tokens_admin.after_revoke(wiring.admin, result)
            return result
        if args.token_command == "rename":
            renamed = tokens_admin.rename(
                wiring.admin,
                uow,
                principal=CLI_PRINCIPAL,
                principal_id=args.principal_id,
                name=args.name,
                reason=args.reason,
            )
            uow.commit()
            return renamed
        if args.rotate:
            raise UsageError("token rotation is replaced by revoke and create")
        minted = tokens_admin.create(
            wiring.admin,
            uow,
            principal=CLI_PRINCIPAL,
            name=args.principal,
            role=args.role,
            reason=args.reason,
        )
        uow.commit()
    # The token is printed exactly once and never stored in clear.
    return {
        "principal": minted.principal.name,
        "role": minted.principal.role.value,
        "token": minted.token,
    }


def ensure_first_admin(database_url: str, delivery: FirstRunDelivery | None) -> None:
    """Create the first browser principal only when no administrator exists.

    Its token never reaches stdout, stderr or a log (crucible#122, ADR 0016): it goes to
    `delivery`, a Secret on Kubernetes or a mode 0600 file on Docker, before the
    principal is committed, so a token that could not be handed over is never minted.
    The log says only where to read it. Without a delivery nothing is minted and the log
    says how to make an administrator instead. A rerun sees the principal and does
    nothing.
    """
    engine = make_engine(database_url)
    try:
        factory = SqlUnitOfWorkFactory(engine)
        with factory() as uow:
            if any(
                item.role is Role.ADMIN and item.disabled_at is None
                for item in uow.principals.list_all()
            ):
                return
            if delivery is None:
                print(
                    "No first-run administrator was created: this deployment has no "
                    "private place for its token (the Kubernetes provider's Secret or the "
                    "Docker credential root). With the supervisor running, create one "
                    'with `crucible admin --reason "<why>" token create --principal '
                    "<name> --role admin`.",
                    file=sys.stderr,
                )
                return
            name = FIRST_RUN_PREFIX
            if uow.principals.get_by_name(name) is not None:
                name = f"{FIRST_RUN_PREFIX}-{new_id()[-8:].lower()}"
            minted = mint_token(
                uow,
                SystemClock(),
                name=name,
                role=Role.ADMIN,
            )
            record_event(
                uow,
                SystemClock(),
                EventKind.PRINCIPAL_CREATED,
                principal="crucible-migrate",
                payload={
                    "principal": minted.principal.name,
                    "role": minted.principal.role.value,
                    "first_run": True,
                },
            )
            delivery.deliver(minted.token)
            uow.commit()
        border = "=" * 72
        for line in (
            border,
            f"CRUCIBLE FIRST-RUN ADMINISTRATOR {minted.principal.name!r} CREATED",
            f"Its one-time token is in {delivery.where()}",
            "Open /ui and sign in with it; that removes it from there.",
            border,
        ):
            print(line, file=sys.stderr)
    finally:
        engine.dispose()


def _register(args: argparse.Namespace, wiring: Wiring, admin: AdminContext) -> Any:
    """The same guarded service the API route calls, returning the same document."""
    with wiring.ctx.uow_factory() as uow:
        if args.repo_command == "list":
            return {"items": repositories_admin.list_all(uow)}
        if args.repo_command == "remove":
            result = repositories_admin.remove(
                admin, uow, principal=CLI_PRINCIPAL, name=args.name, reason=args.reason
            )
            uow.commit()
            return result
        result = repositories_admin.register(
            admin,
            uow,
            principal=CLI_PRINCIPAL,
            name=args.name,
            registration=RepositoryRegistration(
                url=args.url,
                default_branch=args.default_branch,
                policy_name=args.policy,
                installation_id=args.installation_id,
                external_review=ExternalReviewAttestation(
                    attested_all_prs=args.attest_external_review_all_prs,
                    attested_by=args.attested_by,
                ),
                private=args.private,
            ),
            reason=args.reason,
        )
        uow.commit()
    return result


# ----- the envelope ------------------------------------------------------------


def kind_of(args: argparse.Namespace) -> str:
    """The `kind` each verb's document is (crucible/client/schema.py)."""
    command = str(args.command)
    verb = {
        "task": "task_command",
        "token": "token_command",
        "repository": "repo_command",
        "repositories": "repo_command",
        "harnesses": "harness_command",
        "credentials": "credential_command",
        "images": "image_command",
        "providers": "provider_command",
        "github": "github_command",
        "audit": "audit_command",
        "routing": "routing_command",
        "kubernetes": "kubernetes_command",
        "limits": "limits_command",
        "gates": "gates_command",
        "bootstrap": "bootstrap_command",
        "gateway": "gateway_command",
    }.get(command)
    sub = getattr(args, verb) if verb else None
    table: dict[tuple[str, str | None], str] = {
        ("migrate", None): "migration",
        ("status", None): "admin_status",
        ("task", "republish"): "task",
        ("token", "list"): "token_list",
        ("token", "create"): "token_created",
        ("token", "revoke"): "token_revoked",
        ("token", "rename"): "token_renamed",
        ("repository", "list"): "repository_list",
        ("repository", "register"): "repository",
        ("repository", "remove"): "repository_removed",
        ("harnesses", "list"): "harness_list",
        ("harnesses", "enable"): "harness",
        ("harnesses", "disable"): "harness",
        ("harnesses", "test"): "harness_test",
        ("credentials", "status"): "credential_state",
        ("credentials", "login"): "credential_login",
        ("images", "list"): "image_list",
        ("images", "promote"): "image_promotion",
        ("images", "rollback"): "image_promotion",
        ("providers", "status"): "provider_list",
        ("github", "status"): "github_status",
        ("github", "check"): "github_check",
        ("github", "installations"): "github_installations",
        ("github", "external-url"): "github_external_url",
        ("github", "set-external-url"): "github_external_url",
        ("github", "add-repository"): "repository",
        ("gateway", "show"): "gateway",
        ("gateway", "set"): "gateway_test",
        ("gateway", "test"): "gateway_test",
        ("gateway", "models"): "gateway_models",
        ("gateway", "pick"): "gateway_models_saved",
        ("audit", "tail"): "audit_page",
        ("routing", "exhaustion"): "exhaustion_list",
        ("routing", "clear-exhaustion"): "exhaustion_cleared",
        ("routing", "local-endpoint"): "local_endpoint",
        ("routing", "set-local-endpoint"): "local_endpoint",
        ("routing", "preference"): "routing_preference",
        ("routing", "set-preference"): "routing_preference",
        ("kubernetes", "egress"): "kubernetes_egress",
        ("kubernetes", "set-egress"): "kubernetes_egress",
        ("kubernetes", "timeouts"): "kubernetes_timeouts",
        ("kubernetes", "set-timeouts"): "kubernetes_timeouts",
        ("gates", "advisory"): "gate_classes",
        ("gates", "set-advisory"): "gate_classes",
        ("limits", "command-timeout"): "command_timeout",
        ("limits", "set-command-timeout"): "command_timeout",
        ("bootstrap", "list"): "bootstrap_import_list",
    }
    key = ("repository" if command == "repositories" else command, sub)
    if key in table:
        return table[key]
    if command == "credentials":
        return "credential_report"
    if command == "bootstrap":
        return "bootstrap_import"
    return command


def _items(document: Any) -> list[Any]:
    items = document.get("items") if isinstance(document, dict) else None
    return items if isinstance(items, list) else []


def result_for(
    args: argparse.Namespace,
    document: Any,
    *,
    prefix: list[str],
    top_prefix: list[str],
    local: bool,
    role: str,
) -> Result:
    kind = kind_of(args)
    state: str | None = None
    actions: list[dict[str, Any]] = []
    if kind == "task":
        state = document.get("state") if isinstance(document, dict) else None
        if local:
            # In process the principal is this CLI's admin; its one task verb is republish.
            if state == "publish_failed":
                actions = [
                    nx.action(
                        "republish",
                        "retry the failed publication once",
                        [*prefix, "task", "republish", str(document["id"]), "--reason", "{reason}"],
                        needs=nx.REASON,
                        roles=(nx.ADMIN,),
                    )
                ]
        else:
            actions = nx.task_actions(document, role, top_prefix)
    elif kind in ("credential_state", "credential_report"):
        credential = document.get("credential", document) if isinstance(document, dict) else {}
        state = credential.get("state") if isinstance(credential, dict) else None
        actions = nx.credential_actions(args.harness, state, prefix)
    elif kind == "harness_list":
        actions = nx.harness_actions(_items(document), prefix)
    elif kind == "harness_test" and isinstance(document, dict):
        state = "passed" if document.get("ok") else "failed"
    elif kind == "harness" and isinstance(document, dict):
        state = "enabled" if document.get("enabled") else "disabled"
        actions = nx.harness_actions(
            [{"name": args.name, "enabled": document.get("enabled")}], prefix
        )
    elif kind == "image_list":
        actions = nx.image_actions(document, prefix)
    elif kind == "exhaustion_list":
        actions = nx.exhaustion_actions(_items(document), prefix)
    elif kind == "token_list":
        actions = nx.token_actions(_items(document), prefix)
    elif kind == "bootstrap_import" and isinstance(document, dict):
        state = document.get("state")
        actions = nx.bootstrap_actions(document, prefix)
    elif kind == "local_endpoint":
        actions = nx.local_endpoint_actions(document, prefix)
    elif kind == "kubernetes_egress":
        actions = nx.kubernetes_egress_actions(document, prefix)
    elif kind == "kubernetes_timeouts":
        actions = nx.kubernetes_timeouts_actions(document, prefix)
    elif kind == "command_timeout":
        actions = nx.command_timeout_actions(document, prefix)
    elif kind == "routing_preference":
        actions = nx.routing_preference_actions(document, prefix)
    elif kind == "gate_classes":
        actions = nx.gate_classes_actions(document, prefix)
    elif kind == "audit_page":
        actions = nx.audit_actions(document, prefix, args.limit, args.cursor)
    return Result(kind=kind, data=document, state=state, next=actions, role=role)


def run(args: argparse.Namespace, *, root_api_url: str | None, timezone: str | None) -> Result:
    """One admin verb, local or remote, as a Result for the envelope."""
    api_url = args.api_url or root_api_url
    if api_url or args.remote:
        if args.command == "migrate":
            raise UsageError("migrate is CLI-only and runs in local mode; drop --api-url")
        config = resolve(api_url=api_url, timezone=timezone, token_envs=TOKEN_ENVS)
        base_url, token = require_remote(config, TOKEN_ENVS)
        remote_document = _remote(args, Api(base_url, token))
        flag = ["--api-url", base_url] if api_url else ["--remote"]
        top = ["crucible", *(["--api-url", base_url] if api_url else [])]
        # Every admin route admits only an admin; `task republish` is the orchestrator's.
        role = nx.PROBED_ORCHESTRATOR if args.command == "task" else nx.ADMIN
        return result_for(
            args,
            remote_document,
            prefix=["crucible", "admin", *flag],
            top_prefix=top,
            local=False,
            role=role,
        )
    settings = load_settings(args.config)
    # Results go to stdout as JSON; logs go to stderr so callers can parse stdout.
    configure_logging(settings.service.log_level, stream=sys.stderr)
    prefix = ["crucible", "admin", *(["--config", args.config] if args.config else [])]
    try:
        if args.command == "migrate":
            upgrade(settings.database.url)
            ensure_first_admin(settings.database.url, first_run_delivery(settings))
            document: Any = {"migrated_to": head_revision(settings.database.url)}
        else:
            wiring = wire(settings, role="admin")
            if args.command == "token":
                document = _token(args, wiring)
            elif args.command in ("repository", "repositories"):
                if wiring.admin is None:
                    raise _not_configured()
                document = _register(args, wiring, wiring.admin)
            else:
                document = _local(args, wiring)
    except ApplicationError as exc:
        raise application_error(exc) from None
    return result_for(
        args, document, prefix=prefix, top_prefix=["crucible"], local=True, role=nx.ADMIN
    )
