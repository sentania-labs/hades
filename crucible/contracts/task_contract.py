"""TaskContractV1 (05). Intra-document validation lives here; rules that need the
registry (repository, policy, provider) live in the application layer."""

from __future__ import annotations

import hashlib
import json
import posixpath
import shlex
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from crucible.contracts.common import StrictModel, check_major_version
from crucible.domain.exit_class import ExitClass
from crucible.domain.refs import ref_problem
from crucible.domain.secrets import find_secrets


class HarnessName(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    AGY = "agy"
    HERMES = "hermes"
    QWEN_CODE = "qwen_code"
    # The e2e tier's harness (18): a script implementing the adapter's launch contract,
    # with no model and no credential. It is a real harness name because a contract has
    # to be able to name it, and the provider path it exercises is the real one.
    SCRIPT_HARNESS = "script-harness"


class TaskTier(StrEnum):
    """Foundry assigns the tier; the routing policy bounds what it may then name (05b)."""

    TRIVIAL = "trivial"
    STANDARD = "standard"
    COMPLEX = "complex"


class ProviderName(StrEnum):
    FAKE = "fake"
    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    HOSTPROCESS = "hostprocess"


class RepositoryRef(StrictModel):
    name: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    work_branch: str = Field(min_length=1)

    @field_validator("base_ref", "work_branch")
    @classmethod
    def _usable_ref(cls, value: str) -> str:
        """A ref reaches a command line in the preparer, the collector and the
        publisher. It is quoted everywhere it is used, and it is also refused here if
        it is not a plain ref: defence in depth, not either one alone."""
        problem = ref_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class Scope(StrictModel):
    allowed_paths: list[str] = Field(min_length=1)
    prohibited_paths: list[str]
    may_add_dependencies: bool
    may_modify_ci: bool

    @field_validator("allowed_paths", "prohibited_paths")
    @classmethod
    def _valid_globs(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not pattern or pattern.startswith("/") or pattern != pattern.strip():
                raise ValueError(f"invalid glob {pattern!r}")
            if ".." in posixpath.normpath(pattern).split("/"):
                raise ValueError(f"invalid glob {pattern!r}: parent traversal")
            if pattern.count("[") != pattern.count("]") or pattern.count("{") != pattern.count("}"):
                raise ValueError(f"invalid glob {pattern!r}: unbalanced brackets")
        if len(set(value)) != len(value):
            raise ValueError("duplicate glob")
        return value

    @model_validator(mode="after")
    def _no_full_overlap(self) -> Scope:
        overlap = set(self.allowed_paths) & set(self.prohibited_paths)
        if overlap:
            raise ValueError(f"allowed_paths and prohibited_paths overlap: {sorted(overlap)}")
        return self


class ContextRef(StrictModel):
    kind: Literal["issue", "doc", "pr", "url", "file"]
    ref: str = Field(min_length=1)


class ProjectInstruction(StrictModel):
    kind: Literal["file", "skill"]
    ref: str = Field(min_length=1)


class AcceptanceCriterionCheck(StrictModel):
    """hades #449: an executable check on an acceptance criterion, the shape of a
    `required_verification` command. Foundry writes it when it scopes; Crucible's
    verifier re-runs it from the collected tree and the `acceptance_checks` gate judges
    the exit, blocking on a lab-local pool and advisory elsewhere."""

    command: str = Field(min_length=1)
    expect_exit: int = 0

    @field_validator("command")
    @classmethod
    def _runs_in_a_worker(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("is blank")
        program = worker_absent_program(value)
        if program is not None:
            raise ValueError(
                f"runs `{value}`, which needs {program}: no worker image has docker, kind "
                "or kubectl (ADR 0020)"
            )
        return value


class AcceptanceCriterion(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    check: AcceptanceCriterionCheck | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_names_the_criterion(cls, data: Any) -> Any:
        """A malformed check is refused with a reason that names its criterion, so the
        422 says which one rather than only a list index."""
        if not isinstance(data, dict) or data.get("check") is None:
            return data
        try:
            AcceptanceCriterionCheck.model_validate(data["check"])
        except ValidationError as exc:
            reasons = "; ".join(
                ".".join(["check", *(str(p) for p in err["loc"])]) + f": {err['msg']}"
                for err in exc.errors(include_url=False, include_input=False)
            )
            raise ValueError(
                f"criterion {data.get('id')!r} has a malformed check: {reasons}"
            ) from None
        return data


# hades #429: the programs no worker image has and never will (ADR 0020: the worker
# image carries the project's check toolchain, no Docker daemon, no cluster). A required
# check that needs one of them could only ever fail `verification_ran`, so the contract
# is refused at validation and the reason names the program. The tiers that need them
# (the compose smoke, e2e, e2e-kind, image builds and digests) are CI's.
WORKER_ABSENT_PROGRAMS: tuple[str, ...] = ("docker", "kind", "kubectl")

_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_SHELL_PROGRAMS = frozenset({"sh", "bash", "dash", "ash", "ksh", "zsh"})


def _shell_command(words: list[str]) -> str | None:
    """Find the literal command argument of a shell's -c option."""
    index = 0
    while index < len(words):
        option = words[index]
        if option == "--" or not option.startswith("-"):
            return None
        if option == "--command" or (not option.startswith("--") and "c" in option[1:]):
            return words[index + 1] if index + 1 < len(words) else None
        # These options consume an argument before the next shell option.
        index += 2 if option in {"-o", "-O", "--rcfile", "--init-file"} else 1
    return None


def _program_name(word: str) -> str:
    """`/usr/local/bin/docker` and `docker` are the same program; `docker-compose` is
    the Docker CLI too."""
    name = word.rsplit("/", 1)[-1]
    return name.split("-", 1)[0] if name.startswith("docker-") else name


def worker_absent_program(command: str) -> str | None:
    """The program among WORKER_ABSENT_PROGRAMS the command needs, or None.

    Two readings of the command line, both deliberately plain: a word that is one of
    the programs (`docker compose up`, `kubectl apply`, `sudo kind create cluster`),
    and a `make` target whose name carries one as a component (`make deploy-kind`,
    `make e2e-kind`), since a target named for kind runs kind. Flags, assignments and
    paths are not programs: `pytest tests/unit/test_kind.py` and `--kind=x` pass.
    Literal shell -c command strings are inspected at each nesting level; ordinary
    quoted arguments remain data. This does not resolve variables or script files."""
    pending = [command]
    while pending:
        source = pending.pop()
        try:
            lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            words = list(lexer)
        except ValueError:
            words = source.split()
        after_make = False
        for index, word in enumerate(words):
            if word in _SHELL_SEPARATORS:
                after_make = False
                continue
            name = _program_name(word)
            if name in WORKER_ABSENT_PROGRAMS:
                return name
            if name in _SHELL_PROGRAMS:
                nested = _shell_command(words[index + 1 :])
                if nested is not None:
                    pending.append(nested)
            if after_make and not word.startswith("-") and "=" not in word:
                for part in word.replace("_", "-").split("-"):
                    if part in WORKER_ABSENT_PROGRAMS:
                        return part
            after_make = after_make or name == "make"
    return None


class CommandVerification(StrictModel):
    id: str = Field(min_length=1)
    kind: Literal["command"] = "command"
    command: str = Field(min_length=1)
    expect_exit: int = 0

    @model_validator(mode="after")
    def _runs_in_a_worker(self) -> CommandVerification:
        program = worker_absent_program(self.command)
        if program is not None:
            raise ValueError(
                f"required_verification {self.id} runs `{self.command}`, which needs "
                f"{program}: no worker image has docker, kind or kubectl (ADR 0020). The "
                "compose smoke, e2e, e2e-kind and image tiers are CI's, not a check a "
                "worker runs."
            )
        return self


class ArtifactVerification(StrictModel):
    id: str = Field(min_length=1)
    kind: Literal["artifact"]
    path: str = Field(min_length=1)


Verification = CommandVerification | ArtifactVerification


class Constraints(StrictModel):
    prohibited_actions: list[str]
    network: Literal["policy", "none"]


class Deliverable(StrictModel):
    kind: Literal["pull_request", "branch", "artifacts"]
    target: str | None = None
    draft: bool = False
    closes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _target_required(self) -> Deliverable:
        if self.kind in ("pull_request", "branch") and not self.target:
            raise ValueError(f"deliverable kind {self.kind} requires target")
        if self.kind == "artifacts" and self.closes:
            raise ValueError("an artifacts deliverable cannot close issues")
        return self


class Reporting(StrictModel):
    report_schema: Literal["CompletionClaimV1"]
    report_dir: str = Field(pattern=r"^/crucible/report$")
    progress_events: bool


class Escalation(StrictModel):
    conditions: list[str]
    action: str = Field(min_length=1)


class PolicyRef(StrictModel):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)


class OperatorPin(StrictModel):
    harness: HarnessName
    model: str = Field(min_length=1)
    pin_reason: str = Field(min_length=1)


class ExecutionRequest(StrictModel):
    tier: TaskTier
    # C6b: Foundry normally supplies neither. The flat fields remain accepted for the
    # operator bootstrap path described by the task contract; `pin` is the amended
    # specification's equivalent shape. They may not be mixed.
    harness: HarnessName | None = None
    model: str | None = Field(default=None, min_length=1)
    pin_reason: str | None = Field(default=None, min_length=1)
    pin: OperatorPin | None = None
    effort: str | None = None
    provider: ProviderName
    image: str | None = None
    timeout_seconds: int = Field(ge=1)
    # Issue 128: narrows the policy's limits.command_timeout_ms for this task, the
    # timeout each harness runs a shell command under. Absent: the policy default.
    command_timeout_ms: int | None = Field(default=None, ge=1)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _selection_or_pin(self) -> ExecutionRequest:
        if self.image is not None and self.provider is not ProviderName.FAKE:
            raise ValueError("image is derived from the selected harness and must not be supplied")
        flat = self.model is not None or self.harness is not None or self.pin_reason is not None
        if self.pin is not None and flat:
            raise ValueError("pin may not be combined with model, harness, or pin_reason")
        if self.harness is not None and self.model is None:
            raise ValueError("harness without model is not a valid operator pin")
        if self.model is not None and self.harness is None:
            raise ValueError("a pinned model must name its harness")
        if self.model is not None and not self.pin_reason:
            raise ValueError("a pinned model requires pin_reason")
        if self.pin_reason is not None and self.model is None:
            raise ValueError("pin_reason requires a pinned model")
        if (
            self.command_timeout_ms is not None
            and self.command_timeout_ms > self.timeout_seconds * 1000
        ):
            raise ValueError("command_timeout_ms must not exceed timeout_seconds")
        return self

    @property
    def pinned_model(self) -> str | None:
        return self.pin.model if self.pin is not None else self.model

    @property
    def pinned_harness(self) -> HarnessName | None:
        return self.pin.harness if self.pin is not None else self.harness

    @property
    def effective_pin_reason(self) -> str | None:
        return self.pin.pin_reason if self.pin is not None else self.pin_reason


class Lifecycle(StrictModel):
    max_attempts: int = Field(ge=1)
    retry_on: list[ExitClass]
    cleanup: Literal["policy", "keep", "delete"]

    @field_validator("retry_on")
    @classmethod
    def _retry_classes(cls, value: list[ExitClass]) -> list[ExitClass]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate retry_on entry")
        return value


class CorrectionAddress(StrictModel):
    kind: Literal["review_comment", "ci_finding", "internal_review", "acceptance"]
    id: str
    disposition_id: str | None = None


class Correction(StrictModel):
    of_version: int = Field(ge=1)
    reason: Literal[
        "external_review", "ci_certification", "needs_more_work", "pre_pr_gates", "internal_review"
    ]
    addresses: list[CorrectionAddress]
    instructions: str = Field(min_length=1)
    resume_from: Literal["remote_branch", "last_attempt"] = "remote_branch"
    request_internal_review: bool

    @model_validator(mode="before")
    @classmethod
    def _resume_default(cls, value: Any) -> Any:
        if isinstance(value, dict) and "resume_from" not in value:
            value = dict(value)
            value["resume_from"] = (
                "last_attempt" if value.get("reason") == "pre_pr_gates" else "remote_branch"
            )
        return value


class TaskContractV1(StrictModel):
    schema_version: str
    external_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    project: str = Field(min_length=1)
    parent_external_id: str | None
    repository: RepositoryRef
    scope: Scope
    objective: str = Field(min_length=1)
    context: list[ContextRef]
    project_instructions: list[ProjectInstruction]
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    required_verification: list[Verification] = Field(min_length=1)
    constraints: Constraints
    deliverables: list[Deliverable] = Field(min_length=1)
    reporting: Reporting
    escalation: Escalation
    policy: PolicyRef
    execution_request: ExecutionRequest
    lifecycle: Lifecycle
    correction: Correction | None

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return check_major_version(value)

    @model_validator(mode="after")
    def _unique_ids(self) -> TaskContractV1:
        ac_ids = [c.id for c in self.acceptance_criteria]
        if len(set(ac_ids)) != len(ac_ids):
            raise ValueError("acceptance_criteria ids must be unique")
        rv_ids = [v.id for v in self.required_verification]
        if len(set(rv_ids)) != len(rv_ids):
            raise ValueError("required_verification ids must be unique")
        return self

    @model_validator(mode="after")
    def _no_secrets(self) -> TaskContractV1:
        matches = find_secrets(self.model_dump(mode="json"))
        if matches:
            first = matches[0]
            raise ValueError(
                f"secret pattern {first.pattern} matched at {first.path}; "
                "contracts never carry credentials"
            )
        return self

    def external_identity_fields(self) -> tuple[str, str, str, str, int]:
        """What a correction version must keep identical to the version it corrects."""
        return (
            self.external_id,
            self.repository.name,
            self.repository.work_branch,
            self.policy.name,
            self.policy.version,
        )

    @property
    def verification_commands(self) -> list[str]:
        return [v.command for v in self.required_verification if isinstance(v, CommandVerification)]


def contract_sha256(document: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the document as stored."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def correction_narrows(
    previous: TaskContractV1, correction: TaskContractV1
) -> list[dict[str, Any]]:
    """05: a correction version may narrow scope and objective but never widen them, and
    required_verification may not shrink. Returns the problems, empty when it narrows."""
    problems: list[dict[str, Any]] = []
    prev_allowed = set(previous.scope.allowed_paths)
    new_allowed = set(correction.scope.allowed_paths)
    widened = sorted(new_allowed - prev_allowed)
    if widened:
        problems.append(
            {
                "path": "scope.allowed_paths",
                "message": f"a correction may not widen scope; new paths: {widened}",
            }
        )
    dropped_prohibitions = sorted(
        set(previous.scope.prohibited_paths) - set(correction.scope.prohibited_paths)
    )
    if dropped_prohibitions:
        problems.append(
            {
                "path": "scope.prohibited_paths",
                "message": f"a correction may not drop prohibitions: {dropped_prohibitions}",
            }
        )
    for flag in ("may_add_dependencies", "may_modify_ci"):
        if getattr(correction.scope, flag) and not getattr(previous.scope, flag):
            problems.append(
                {"path": f"scope.{flag}", "message": "a correction may not widen scope"}
            )
    prev_checks = {v.id for v in previous.required_verification}
    new_checks = {v.id for v in correction.required_verification}
    missing = sorted(prev_checks - new_checks)
    if missing:
        problems.append(
            {
                "path": "required_verification",
                "message": f"required_verification may not shrink; missing: {missing}",
            }
        )
    if correction.external_identity_fields() != previous.external_identity_fields():
        problems.append(
            {
                "path": "external_id",
                "message": "a correction keeps external_id, repository, and policy identical",
            }
        )
    return problems
