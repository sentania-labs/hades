"""Rendering the per-attempt identity bundle (06).

The bundle is assembled by Crucible, mounted read-only, and never written into the
repository. It carries no credential, no API token, and nothing the contract did not
name. Its content hash goes on the attempt and `IDENTITY.md` is stored as an artifact,
so the exact instructions a worker saw are reproducible.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from crucible.adapters.execution import scripts
from crucible.domain.gates import ENFORCED_PRE_PR_GATES, PRE_PR_GATES
from crucible.ports.execution import IDENTITY_MOUNT, REPO_MOUNT, REPORT_MOUNT

__all__ = [
    "AMBIGUOUS_CONTRACT_STOP_RULE",
    "IDENTITY_MOUNT",
    "REPORT_MOUNT",
    "REPO_MOUNT",
    "WORKER_ABSENT_PROGRAMS_SENTENCE",
    "bundle_sha256",
    "write_bundle",
]

# hades #429: the one sentence CONTRIBUTING.md and the Checks section share, so a worker
# that reads a project's instructions to run the kind tier or build images does not
# stop over a program it was never going to have (ADR 0020).
WORKER_ABSENT_PROGRAMS_SENTENCE = (
    "Docker, kind and kubectl are absent in a worker and are CI's; a missing one is "
    "expected and is not a reason to stop."
)

# hades #393: the one paragraph of the "If you are stuck" section that tells the worker
# to stop rather than guess. The operator's words, kept as they were given.
AMBIGUOUS_CONTRACT_STOP_RULE = (
    "When the contract is ambiguous, stop rather than pick a reading: a "
    "workaround that changes the result is not a workaround, it is a wrong answer. A "
    "blocked attempt is not retried; Hades hands your reason and your words verbatim to "
    "the person who answers, and a correction brings the answer back."
)


def _items(values: Any) -> list[Any]:
    return list(values) if isinstance(values, list | tuple) else []


def commit_trailer(policy: dict[str, Any]) -> str:
    """The trailer key the policy names, which the hook adds and the checks look for."""
    return str(policy.get("git", {}).get("commit_trailer") or "Crucible-Attempt")


def _bullets(lines: list[str], empty: str = "none") -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else f"- {empty}"


def _yes_no(value: Any) -> str:
    return "yes" if value is True else "no"


def _paths(values: Any) -> str:
    return ", ".join(f"`{value}`" for value in _items(values)) or "none"


def _reference(entry: Any) -> str:
    """A `{kind, ref}` entry of the contract (context, project_instructions) as words."""
    if not isinstance(entry, dict):
        return str(entry)
    return f"{entry.get('kind', 'ref')}: `{entry.get('ref', '')}`"


def _check(verification: dict[str, Any]) -> str:
    if str(verification.get("kind", "command")) == "command":
        expect = verification.get("expect_exit", 0)
        suffix = f" (expect exit {expect})" if expect not in (0, None) else ""
        return f"`{verification.get('command')}`{suffix}"
    # An artifact is named by its collected path, `report/...`: the report directory.
    path = str(verification.get("path"))
    leaf = path.removeprefix("report/")
    return f"write `{REPORT_MOUNT}/{leaf}`" if leaf != path else f"write `{path}` in the checkout"


def render_identity_md(
    *,
    contract: dict[str, Any],
    policy: dict[str, Any],
    external_id: str,
    owner: str,
    work_branch: str,
    network_mode: str,
) -> str:
    """What the worker is told (06, FDY-0140): the task, its scope, the checks, the report
    and how to stop, in plain words and as short as that allows. Everything else is in
    `contract.yaml` beside it; Crucible itself re-runs the checks, commits what is left
    uncommitted, and enforces the scope, so none of that is asked of the worker."""
    scope = contract.get("scope") or {}
    repository = contract.get("repository") or {}
    constraints = contract.get("constraints") or {}
    escalation = contract.get("escalation") or {}
    correction = contract.get("correction") or {}
    criteria = [c for c in _items(contract.get("acceptance_criteria")) if isinstance(c, dict)]
    checks = [v for v in _items(contract.get("required_verification")) if isinstance(v, dict)]
    ids = ", ".join(f"`{c.get('id')}`" for c in criteria) or "none"
    sections = [
        f"# Task {external_id}: {contract.get('title', '(no title)')}",
        f"You are working on task `{external_id}` for `{owner}` in the repository "
        f"`{repository.get('name', '(unnamed)')}`, checked out at `{REPO_MOUNT}` on "
        f"branch `{work_branch}` from `{repository.get('base_ref', 'main')}`. Do the task "
        "yourself; do not redefine, widen or delegate it.",
        "The workspace origin is the `crucible-no-remote` helper; `git fetch origin` "
        "will fail. `origin/main` is main as of workspace preparation, so a worker "
        "told to bring in main should merge that ref rather than fetching it.",
        f"## Objective\n\n{str(contract.get('objective', '')).strip()}",
    ]
    if correction:
        addressed = [
            f"{a.get('kind')}: `{a.get('id')}`"
            for a in _items(correction.get("addresses"))
            if isinstance(a, dict)
        ]
        sections.append(
            "## This is a correction\n\n"
            f"{str(correction.get('instructions', '')).strip()}\n\n"
            f"Address:\n{_bullets(addressed)}"
        )
    sections.append(
        "## Scope\n\n"
        f"- Change only: {_paths(scope.get('allowed_paths'))}\n"
        f"- Never touch: {_paths(scope.get('prohibited_paths'))}\n"
        f"- Add dependencies: {_yes_no(scope.get('may_add_dependencies'))}. "
        f"Change CI: {_yes_no(scope.get('may_modify_ci'))}. Network: {network_mode}.\n"
        f"- Commit your work on `{work_branch}`; never push."
        + "".join(
            f"\n- Do not {str(a).strip()}" for a in _items(constraints.get("prohibited_actions"))
        )
    )
    context = [_reference(c) for c in _items(contract.get("context"))]
    context += [_reference(i) for i in _items(contract.get("project_instructions"))]
    if context:
        sections.append(f"## Read first\n\n{_bullets(context)}")
    sections.append(
        "## Acceptance criteria\n\n"
        + _bullets([f"`{c.get('id')}`: {c.get('text', '')}" for c in criteria])
    )
    sections.append(
        "## Checks\n\nRun these from the checkout and fix what fails:\n\n"
        + _bullets([_check(v) for v in checks])
        + "\n- If a required command's program is missing from the image, do not "
        "write a substitute for it; write report/blocked.md naming the program, with "
        "the line `reason: missing_capability`, and exit 75.\n- " + WORKER_ABSENT_PROGRAMS_SENTENCE
    )
    sections.append(
        "## Report\n\n"
        f"Write `{REPORT_MOUNT}/report.yaml` (schema: `{IDENTITY_MOUNT}/report-schema.json`) "
        "with "
        '`schema_version: "1.0"`, `summary`, `self_review` (`documentation`, '
        "`acceptance_criteria`, `omissions`), "
        "`acceptance_mapping` (one entry per "
        f"criterion id: {ids}; `status` is `met`, `not_met`, `partial` or "
        "`not_exercised`, with `evidence`), `proposed_pull_request` (`title`, `body`), "
        "and `limitations`, `risks`, `blockers`, `follow_ups` (lists, `[]` if none). "
        f"Then run `crucible-report check {REPORT_MOUNT}/report.yaml` and fix every "
        "problem it prints."
    )
    sections.append(
        "The worker self-review is the internal review. The required `self_review` section "
        "names where documentation was updated (or why no update was needed), maps every "
        "acceptance criterion with evidence, and lists anything knowingly left out and why."
        " Hades records acceptance and publishes after passing gates, without an "
        "orchestrator review or acceptance call, for first attempts and corrections."
    )
    conditions = [str(c) for c in _items(escalation.get("conditions"))]
    stuck = (
        "## If you are stuck\n\n"
        f"Write `{REPORT_MOUNT}/blocked.md`: a line `reason: missing_capability` (a "
        "program the image lacks) or `reason: ambiguous_contract` (the contract reads two "
        "ways), then what blocks you and what you tried, in your own words. Then stop.\n\n"
        + AMBIGUOUS_CONTRACT_STOP_RULE
    )
    if conditions:
        stuck += f" Stop this way when:\n\n{_bullets(conditions)}"
    sections.append(stuck)
    return "\n\n".join(sections) + "\n"


def render_policy_md(policy: dict[str, Any], contract: dict[str, Any]) -> str:
    """The deterministic policy in words (06): timeouts, paths, gates."""
    limits = policy.get("limits", {})
    gates = policy.get("gates", {})
    # With no list the whole pre-PR set runs (11), and the enforced gates run either way.
    listed = gates.get("pre_pr")
    pre_pr = list(sorted(PRE_PR_GATES) if listed is None else listed)
    pre_pr.extend(sorted(ENFORCED_PRE_PR_GATES - set(pre_pr)))
    timeout = contract.get("timeout_seconds") or limits.get("timeout_seconds", {}).get("default")
    return f"""# Delivery policy

- Timeout for this attempt: {timeout} seconds.
- Drain grace before kill: {limits.get("grace_seconds")} seconds.
- A run with no activity for {limits.get("stall_fail_seconds")} seconds is stalled
  and terminated.
- Work branch pattern: `{policy.get("git", {}).get("work_branch_pattern")}`.
- Protected branches you may never touch: {_paths(policy.get("git", {}).get("protected_branches"))}.

## Gates Crucible evaluates before anything is published

{_bullets(pre_pr)}

These are mechanical. Crucible re-runs every required verification command
itself, from the collected tree, in a container you do not control. Your own
logs are shown to the orchestrator as a claim and never satisfy a gate.
"""


def write_bundle(
    directory: Path,
    *,
    contract: dict[str, Any],
    policy: dict[str, Any],
    external_id: str,
    owner: str,
    work_branch: str,
    network_mode: str,
    report_schema: dict[str, Any],
    history: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Write the bundle and return (IDENTITY.md text, bundle sha256)."""
    directory.mkdir(parents=True, exist_ok=True)
    identity_md = render_identity_md(
        contract=contract,
        policy=policy,
        external_id=external_id,
        owner=owner,
        work_branch=work_branch,
        network_mode=network_mode,
    )
    contract_yaml = yaml.safe_dump(contract, sort_keys=True, default_flow_style=False)
    files: dict[str, str] = {
        "IDENTITY.md": identity_md,
        "contract.yaml": contract_yaml,
        # The same document as JSON. 06 names contract.yaml; the JSON copy is what a
        # worker with jq and no YAML parser reads, and it is byte-for-byte the same
        # object, so contract.sha256 still covers what the worker was given.
        "contract.json": json.dumps(contract, indent=2, sort_keys=True) + "\n",
        "contract.sha256": hashlib.sha256(contract_yaml.encode("utf-8")).hexdigest() + "\n",
        "policy.md": render_policy_md(policy, contract),
        "report-schema.json": json.dumps(report_schema, indent=2, sort_keys=True) + "\n",
    }
    for name, text in files.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o444)
    # hades FDY-0135: the checkout's core.hooksPath names this directory, so the hook is
    # Crucible's text, read-only, and covered by the bundle hash like the rest.
    hook_dir = directory / scripts.COMMIT_HOOK_DIR
    hook_dir.mkdir(exist_ok=True)
    # Traversable by the worker uid whatever the service's umask; git skips a hook it
    # cannot reach or execute without a word.
    os.chmod(hook_dir, 0o755)
    hook = hook_dir / "commit-msg"
    hook.write_text(
        scripts.commit_msg_hook(
            trailer=commit_trailer(policy),
            value=external_id,
        ),
        encoding="utf-8",
    )
    os.chmod(hook, 0o555)
    history_dir = directory / "history"
    history_dir.mkdir(exist_ok=True)
    for name, text in history or []:
        entry = history_dir / name
        entry.write_text(text, encoding="utf-8")
        os.chmod(entry, 0o444)
    return identity_md, bundle_sha256(directory)


def bundle_sha256(directory: Path) -> str:
    """A content hash over every file in the bundle, path and bytes, in sorted order."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
