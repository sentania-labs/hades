#!/usr/bin/python3
"""crucible-report: check a worker's report.yaml before the worker exits (hades #215).

    crucible-report check <report.yaml> [--contract <contract.json>]

Prints each problem in plain words and exits 1 while there is any, 0 when there is
none, and 2 when it cannot run at all. It reads the contract from the identity bundle
(`/crucible/identity/contract.json`) to check that every acceptance criterion has an
entry. It needs no network and nothing beyond the image: the standard library, and
PyYAML from the Hermes venv, which it re-executes under when the system Python has no
YAML parser.

It mirrors CompletionClaimV1 in crucible/contracts/completion_claim.py, which is what
Crucible itself parses; tests/unit/test_report_check.py holds the two in agreement.
The worker writes judgement: summary, self_review, acceptance_mapping, the proposed pull
request's title and body, limitations, risks, blockers and follow_ups. Crucible fills the facts
(task_external_id, changed_files, refs, checks, run_evidence) from its own evidence, so
they are optional here.

Secret scanning (hades #221): copies the patterns from crucible/domain/secrets.py into
a module constant so the standalone worker can flag credential-shaped strings in the
report itself.

Scope checking (hades #227): when a contract is readable and git is available, computes
the diff and flags any changed path outside allowed_paths or inside prohibited_paths.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - the image's system Python
    yaml = None  # type: ignore[assignment]

HERMES_PYTHON = "/opt/hermes/bin/python3"
DEFAULT_CONTRACT = "/crucible/identity/contract.json"
REEXEC_MARK = "CRUCIBLE_REPORT_REEXEC"

FACT_FIELDS = ("task_external_id", "changed_files", "refs", "checks", "run_evidence")
JUDGEMENT_FIELDS = (
    "summary",
    "self_review",
    "acceptance_mapping",
    "proposed_pull_request",
    "limitations",
    "risks",
    "blockers",
    "follow_ups",
)
LIST_FIELDS = ("limitations", "risks", "blockers", "follow_ups")
DISPOSITION_KEYS = ("review_comment_id", "disposition", "commit", "reason")
STATUSES = ("met", "not_met", "not_exercised", "partial")
MAPPING_KEYS = ("id", "status", "evidence")
SELF_REVIEW_KEYS = ("documentation", "acceptance_criteria", "omissions")
PULL_REQUEST_KEYS = ("title", "body", "closes")
REFS_KEYS = ("branch", "head_sha", "commits")
CHECK_KEYS = ("id", "command", "exit", "log")
KNOWN = ("schema_version", *FACT_FIELDS, *JUDGEMENT_FIELDS, "finding_dispositions")
VERSION = re.compile(r"^1\.[0-9]+$")

WHY_UNKNOWN = {
    "head": "Crucible reads the head from the collected branch",
    "head_sha": "Crucible reads the head from the collected branch",
    "base": "Crucible takes the base from the contract",
}

# --------------------------------------------------------------------------- Secret
# patterns (hades #221) - copied verbatim from crucible/domain/secrets.py so the
# standalone worker can scan the report itself for credential-shaped strings.

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github_installation_token", re.compile(r"\bghs_[A-Za-z0-9._-]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github_fine_grained_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("anthropic_oauth_token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("google_oauth_access_token", re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}")),
    ("google_oauth_refresh_token", re.compile(r"\b1//[A-Za-z0-9_-]{20,}")),
    (
        "codex_refresh_token",
        re.compile(r"\b[A-Za-z0-9]{1,8}\.[A-Za-z0-9]{1,8}\.[A-Za-z0-9_-]{100,}"),
    ),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\b",
        ),
    ),
    (
        "crucible_token",
        re.compile(r"\bcru_[A-Za-z0-9]{26}\.[A-Za-z0-9_-]{30,}\b"),
    ),
]

# The closing-keyword and at-mention regexes that publication.py's validate_title
# uses.  Copied here so the standalone checker enforces the same rules (hades #314).
_CLOSING_KEYWORDS: tuple[str, ...] = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)
CLOSING_RE = re.compile(
    r"\b(" + "|".join(_CLOSING_KEYWORDS) + r")\b(\s*:?\s*)(?=(?:[\w.-]+/[\w.-]+)?#\d+|https?://)",
    re.IGNORECASE,
)
MENTION_RE = re.compile(r"(?<!\w)@(?=[A-Za-z0-9][\w-]*)")


def _glob_re(pattern: str) -> re.Pattern[str]:
    """Return a compiled regex that matches * at one level and ** at any depth."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                out.append("(?:.*/)?")
                index += 3
                continue
            if pattern.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            close = pattern.find("]", index + 1)
            if close != -1:
                out.append(pattern[index : close + 1])
                index = close + 1
                continue
        out.append(re.escape(char))
        index += 1
    return re.compile("".join(out) + r"\Z")


def _matches_any(path: str, patterns: list[str]) -> bool:
    """Return True if *path* matches any of the *patterns* (fnmatch with **)."""
    normalized = posixpath.normpath(path)
    for pattern in patterns:
        normalized_pattern = pattern.rstrip("/")
        if _glob_re(normalized_pattern).match(normalized):
            return True
        if normalized_pattern.endswith("/**") and normalized.startswith(normalized_pattern[:-2]):
            return True
    return False


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fact_problem(name: str, value: Any) -> str | None:
    """What is wrong with a fact field the worker chose to write, or None."""
    if name == "task_external_id":
        return None if isinstance(value, str) and value else "it must be the task's id as text"
    if name in ("changed_files", "run_evidence"):
        return None if _is_str_list(value) else "it must be a list of paths"
    if name == "refs":
        if not isinstance(value, dict):
            return "it must be a mapping of branch, head_sha and commits"
        extra = sorted(str(k) for k in value if k not in REFS_KEYS)
        if extra:
            return f"it has fields that are not refs fields: {', '.join(extra)}"
        if not (isinstance(value.get("branch"), str) and value.get("branch")):
            return "refs.branch must be the branch name"
        if not (isinstance(value.get("head_sha"), str) and value.get("head_sha")):
            return "refs.head_sha must be the commit hash"
        if not (_is_int(value.get("commits")) and value["commits"] >= 0):
            return "refs.commits must be a whole number"
        return None
    if not isinstance(value, list):
        return "it must be a list of checks"
    for index, check in enumerate(value):
        if not isinstance(check, dict):
            return f"checks[{index}] must be a mapping of id, command, exit and log"
        extra = sorted(str(k) for k in check if k not in CHECK_KEYS)
        if extra:
            return f"checks[{index}] has fields that are not check fields: {', '.join(extra)}"
        for key in ("id", "command", "log"):
            if not (isinstance(check.get(key), str) and check.get(key)):
                return f"checks[{index}].{key} must be text"
        if not _is_int(check.get("exit")):
            return f"checks[{index}].exit must be the exit code, a whole number"
    return None


def _mapping_entries(value: Any, problems: list[str]) -> list[dict[str, Any]]:
    """The mapping's entries as a list, whichever of the two accepted forms it has."""
    if isinstance(value, dict):
        entries: list[Any] = []
        for key, entry in value.items():
            if isinstance(entry, dict):
                entries.append({"id": key, **{k: v for k, v in entry.items() if k != "id"}})
            elif isinstance(entry, str):
                entries.append({"id": key, "status": entry, "evidence": ""})
            else:
                entries.append(entry)
    elif isinstance(value, list):
        entries = value
    else:
        problems.append(
            "acceptance_mapping must be a mapping of criterion ids, or a list of entries."
        )
        return []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(
                f"acceptance_mapping[{index}] must be a mapping of id, status and evidence."
            )
            continue
        extra = sorted(str(k) for k in entry if k not in MAPPING_KEYS)
        if extra:
            problems.append(
                f"acceptance_mapping[{index}] has fields that are not accepted: {', '.join(extra)}."
            )
        for key in MAPPING_KEYS:
            if key not in entry:
                problems.append(
                    f"acceptance_mapping[{index}].{key} is missing: give every entry an "
                    "id, a status and the evidence."
                )
        for key in ("id", "evidence"):
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                problems.append(
                    f"acceptance_mapping[{index}].{key} must be text, got {type(value).__name__}."
                )
        status = entry.get("status")
        if status not in STATUSES:
            problems.append(
                f"acceptance_mapping[{index}].status must be one of: {', '.join(STATUSES)}."
            )
    return entries


def _scan_secrets(value: Any, path: str = "") -> list[str]:
    """Walk a nested document and return one problem per secret-shaped string."""
    problems: list[str] = []
    if isinstance(value, str):
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(value):
                problems.append(f"{path} contains a credential-shaped string ({name}); remove it.")
                break  # only report once per value
    elif isinstance(value, dict):
        for key, item in value.items():
            problems.extend(_scan_secrets(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            problems.extend(_scan_secrets(item, f"{path}[{index}]"))
    return problems


# ----- acceptance checking -----------------------------------------------------


def check(
    document: Any,
    criteria: list[str] | None = None,
    changed: list[str] | None = None,
    scope: dict | None = None,
) -> list[str]:
    """Every problem with the document, in plain words. Empty means none."""
    if not isinstance(document, dict):
        return ["The report must be a YAML mapping of field names to values."]
    problems: list[str] = []

    # --- secrets (hades #221) ---
    problems.extend(_scan_secrets(document))

    # --- schema version ---
    for key in sorted(str(k) for k in document if k not in KNOWN):
        why = WHY_UNKNOWN.get(key)
        problems.append(
            f"`{key}` is not a report field: remove it" + (f" ({why})." if why else ".")
        )

    version = document.get("schema_version")
    if version is None:
        problems.append('schema_version is missing: write `schema_version: "1.0"`.')
    elif not isinstance(version, str):
        problems.append('schema_version must be text: write it quoted, `schema_version: "1.0"`.')
    elif not VERSION.fullmatch(version):
        problems.append('schema_version must be the format version "1.0", not the schema\'s name.')

    summary = document.get("summary")
    if summary is None:
        problems.append("summary is missing: say in a few sentences what you changed and why.")
    elif not (isinstance(summary, str) and summary.strip()):
        problems.append("summary must be text saying what you changed and why.")

    self_review = document.get("self_review")
    if self_review is None:
        problems.append(
            "self_review is missing: name the documentation updated, map every acceptance "
            "criterion with evidence, and list anything knowingly left out and why."
        )
    elif not isinstance(self_review, dict):
        problems.append(
            "self_review must be a mapping of documentation, acceptance_criteria and omissions."
        )
    else:
        for key in sorted(str(k) for k in self_review if k not in SELF_REVIEW_KEYS):
            problems.append(f"self_review.{key} is not a field: remove it.")
        for name in ("documentation", "omissions"):
            if name not in self_review:
                problems.append(
                    f"self_review.{name} is missing: write a list, `[]` when there are none."
                )
            elif not _is_str_list(self_review[name]):
                problems.append(f"self_review.{name} must be a list of text items.")
        if self_review.get("documentation") == []:
            problems.append(
                "self_review.documentation must say where docs changed or why none changed."
            )
        for name in ("documentation", "omissions"):
            notes = self_review.get(name)
            if isinstance(notes, list) and any(
                isinstance(note, str) and not note.strip() for note in notes
            ):
                problems.append(f"self_review.{name} notes must not be blank.")
        if "acceptance_criteria" not in self_review:
            problems.append(
                "self_review.acceptance_criteria is missing: map every acceptance criterion with evidence."
            )
        else:
            reviewed = _mapping_entries(self_review["acceptance_criteria"], problems)
            if any(
                not str(entry.get("evidence") or "").strip()
                for entry in reviewed
                if isinstance(entry, dict)
            ):
                problems.append(
                    "self_review.acceptance_criteria must give evidence for every acceptance criterion."
                )
            ids = [
                entry.get("id")
                for entry in reviewed
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            ]
            if len(ids) != len(set(ids)):
                problems.append(
                    "self_review.acceptance_criteria must map each criterion exactly once."
                )
            reviewed_ids = {
                str(entry.get("id"))
                for entry in reviewed
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            }
            if criteria is not None:
                for criterion in criteria:
                    if criterion not in reviewed_ids:
                        problems.append(
                            f"acceptance criterion {criterion} has no entry in self_review.acceptance_criteria."
                        )
                for extra in sorted(reviewed_ids - set(criteria)):
                    problems.append(
                        f"self_review.acceptance_criteria has {extra}, which is not an acceptance criterion "
                        "of this contract: remove it."
                    )

    if "acceptance_mapping" not in document:
        problems.append(
            "acceptance_mapping is missing: give each acceptance criterion an entry with "
            "its id, a status and the evidence."
        )
    else:
        entries = _mapping_entries(document["acceptance_mapping"], problems)
        mapped = {str(e.get("id")) for e in entries if isinstance(e.get("id"), str)}
        if criteria is not None:
            for criterion in criteria:
                if criterion not in mapped:
                    problems.append(
                        f"acceptance criterion {criterion} has no entry in acceptance_mapping."
                    )
            for extra in sorted(mapped - set(criteria)):
                problems.append(
                    f"acceptance_mapping has {extra}, which is not an acceptance "
                    "criterion of this contract (verification ids never go here): "
                    "remove it."
                )

    pull_request = document.get("proposed_pull_request")
    if pull_request is None:
        problems.append(
            "proposed_pull_request is missing: give it a title and a body (closes is optional)."
        )
    elif not isinstance(pull_request, dict):
        problems.append("proposed_pull_request must be a mapping with a title and a body.")
    else:
        for key in sorted(str(k) for k in pull_request if k not in PULL_REQUEST_KEYS):
            problems.append(
                f"proposed_pull_request.{key} is not a field: remove it (Crucible "
                "sets the pull request's base and head itself)."
            )
        title = pull_request.get("title")
        if not (isinstance(title, str) and title):
            problems.append("proposed_pull_request.title must be a one-line title, as text.")
        else:
            clean_title = " ".join(title.split())
            if not clean_title:
                problems.append("proposed_pull_request.title must be a one-line title, as text.")
            elif "\n" in title.strip() or "\r" in title.strip():
                problems.append("a pull request title is one line")
            else:
                if CLOSING_RE.search(clean_title):
                    problems.append(
                        "the proposed title carries a closing keyword; only "
                        "deliverables[].closes may close an issue (23)"
                    )
                if MENTION_RE.search(clean_title):
                    problems.append(
                        "the proposed title carries an at-mention; a mention "
                        "notifies people and can trigger the external reviewer "
                        "under Crucible's identity (23)"
                    )
        if not isinstance(pull_request.get("body"), str):
            problems.append("proposed_pull_request.body must be text.")
        if "closes" in pull_request and not _is_str_list(pull_request["closes"]):
            problems.append("proposed_pull_request.closes must be a list of issue references.")

    for name in LIST_FIELDS:
        if name not in document:
            problems.append(f"{name} is missing: write a list, `[]` when there are none.")
        elif not _is_str_list(document[name]):
            problems.append(f"{name} must be a list of text items, `[]` when there are none.")

    dispositions = document.get("finding_dispositions", [])
    if not isinstance(dispositions, list):
        problems.append("finding_dispositions must be a list.")
    else:
        for index, item in enumerate(dispositions):
            at = f"finding_dispositions[{index}]"
            if not isinstance(item, dict):
                problems.append(f"{at} must be a mapping.")
                continue
            extra = sorted(str(k) for k in item if k not in DISPOSITION_KEYS)
            if extra:
                problems.append(f"{at} has fields that are not accepted: {', '.join(extra)}.")
            if not (
                isinstance(item.get("review_comment_id"), str) and item.get("review_comment_id")
            ):
                problems.append(f"{at}.review_comment_id must be text.")
            disposition = item.get("disposition")
            if disposition not in ("fixed", "declined"):
                problems.append(f"{at}.disposition must be fixed or declined.")
            required = "commit" if disposition == "fixed" else "reason"
            if disposition in ("fixed", "declined") and not (
                isinstance(item.get(required), str) and item.get(required)
            ):
                problems.append(f"{at}.{required} is required for a {disposition} finding.")

    for name in FACT_FIELDS:
        if document.get(name) is None:
            continue
        why = _fact_problem(name, document[name])
        if why:
            problems.append(
                f"{name} is optional, because Crucible fills it from its own "
                f"evidence, but as written it is not valid: {why}. Fix it or remove it."
            )

    # --- out-of-scope changes (hades #227) ---
    if changed is not None and scope is not None:
        allowed = scope.get("allowed_paths", [])
        prohibited = scope.get("prohibited_paths", [])
        for path in sorted(changed):
            if (prohibited and _matches_any(path, prohibited)) or (
                allowed and not _matches_any(path, allowed)
            ):
                problems.append(f"you changed {path}, which this task may not touch; revert it.")

    return problems


# ----- contract helpers --------------------------------------------------------


def _criteria(contract_path: Path) -> list[str] | None:
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = contract.get("acceptance_criteria") if isinstance(contract, dict) else None
    if not isinstance(items, list):
        return None
    return [str(c.get("id")) for c in items if isinstance(c, dict) and c.get("id")]


def _load(path: Path) -> tuple[Any, str | None]:
    text = path.read_text(encoding="utf-8")
    if yaml is None:
        try:
            return json.loads(text), None
        except ValueError:
            return None, "no YAML parser here, and the report is not JSON either"
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as exc:
        return None, f"the report is not valid YAML: {exc}"


def _compute_scope(contract_path: Path) -> tuple[list[str] | None, dict | None]:
    """Read the contract, compute changed files, and return (changed, scope).

    Returns (None, None) when git is unavailable or the diff cannot be computed.
    """
    try:
        contract_text = contract_path.read_text(encoding="utf-8")
        contract = json.loads(contract_text)
    except (OSError, ValueError):
        return None, None

    if not isinstance(contract, dict):
        return None, None

    repository = contract.get("repository", {})
    if not isinstance(repository, dict):
        repository = {}

    base_ref = repository.get("base_ref", "main")
    scope = contract.get("scope", {})
    if not isinstance(scope, dict):
        scope = {}

    try:
        result = subprocess.run(
            ["git", "merge-base", "HEAD", f"origin/{base_ref}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None, None
        base = result.stdout.strip()

        diff_result = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        ls_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if diff_result.returncode != 0 or ls_result.returncode != 0:
            return None, None
        changed: list[str] = [p for p in diff_result.stdout.strip().splitlines() if p] + [
            p for p in ls_result.stdout.strip().splitlines() if p
        ]
        return changed, scope
    except (OSError, subprocess.TimeoutExpired):
        return None, None


# ----- main entry-point --------------------------------------------------------


def main(argv: list[str]) -> int:
    usage = "usage: crucible-report check <report.yaml> [--contract <contract.json>]"
    if len(argv) < 2 or argv[0] != "check":
        print(usage, file=sys.stderr)
        return 2
    if yaml is None and not os.environ.get(REEXEC_MARK) and os.access(HERMES_PYTHON, os.X_OK):
        # The system Python has no YAML parser; the Hermes venv's has PyYAML. -I keeps
        # the venv's site-packages to this one process and ignores the environment.
        os.environ[REEXEC_MARK] = "1"
        os.execv(HERMES_PYTHON, [HERMES_PYTHON, "-I", os.path.abspath(__file__), *argv])
    report = Path(argv[1])
    contract = Path(DEFAULT_CONTRACT)
    rest = argv[2:]
    if rest:
        if len(rest) != 2 or rest[0] != "--contract":
            print(usage, file=sys.stderr)
            return 2
        contract = Path(rest[1])
    if not report.is_file():
        print(f"{report}: no such file. Write the report there first.")
        return 1
    document, error = _load(report)
    if error:
        print(f"{report}: 1 problem")
        print(f"- {error}")
        return 1
    criteria = _criteria(contract)
    problems = check(document, criteria)

    # Out-of-scope check when git is available and a contract is readable.
    scope_changed, scope_data = _compute_scope(contract)
    if scope_changed is not None and scope_data is not None:
        scope_problems = check(document, criteria, changed=scope_changed, scope=scope_data)
        scope_only = [p for p in scope_problems if p not in problems]
        problems.extend(scope_only)

    if not problems:
        print(
            f"{report}: no problems. Crucible fills {', '.join(FACT_FIELDS)} from its own evidence."
        )
        if criteria is None:
            print(
                f"note: no contract at {contract}, so acceptance criteria coverage is not checked"
            )
        if scope_changed is None:
            print("note: git is unavailable or the scope check could not run")
        return 0
    print(f"{report}: {len(problems)} problem{'s' if len(problems) != 1 else ''}")
    for problem in problems:
        print(f"- {problem}")
    print(f"Fix each one and run `crucible-report check {report}` again before you exit 0.")

    # Notes after problem list (so they don't interfere with structured output).
    if criteria is None:
        print(f"note: no contract at {contract}, so acceptance criteria coverage is not checked")
    if scope_changed is None:
        print("note: git is unavailable or the scope check could not run")

    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
