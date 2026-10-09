"""FDY-0618 (hades #488, #556, #559): what a no_secrets failure tells people.

Every secret-shaped value here is built at run time; none is written out.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.execution import collected, scripts
from crucible.adapters.execution.collected import read_outputs, scan_changed_content
from crucible.adapters.execution.scripts import collector_script
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.corrections import _with_no_secrets_guidance, no_secrets_guidance
from crucible.application.evidence import (
    _scanner_findings,
    _transcript_windows,
    record_collection_evidence,
)
from crucible.application.gates import _secret_guidance, evidence_items
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.entities import (
    Artifact,
    Attempt,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    Task,
)
from crucible.domain.gates import EvidenceItem, GateInput, GateResult, no_secrets
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.domain.secret_findings import (
    COMMAND_INPUT_LIMIT,
    NO_SECRETS_ADVICE,
    TRANSCRIPT_WINDOW_ARTIFACT,
    no_secrets_correction,
    transcript_command,
)
from crucible.domain.secret_fixtures import (
    declarations,
    parse_gitleaks_config,
    parse_gitleaksignore,
)
from crucible.domain.secrets import match_line, redact_excerpt, redact_line
from crucible.ports.execution import CollectedArtifact, CollectedOutputs, LaunchSpec
from tests.collector_tools import collector_env
from tests.fixtures import contract_document

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
GIT_USER = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
# Built at run time (CONTRIBUTING, rules for workers): never a literal.
KEY = "gh" + "p_" + "A" * 36
OTHER = "gh" + "p_" + "B" * 36
FIXTURE = "gh" + "p_" + "F" * 36
AWS = "AK" + "IA" + "Q" * 16
SECRETS = (KEY, OTHER, FIXTURE, AWS)


def _no_value(text: str) -> None:
    for value in SECRETS:
        assert value not in text


# Collector fixtures, as the other collector tests run the real script.


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *GIT_USER, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _repository(tmp_path: Path, base: dict[str, str], change: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, {"README.md": "base\n", **base})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "crucible/test")
    _write(repo, change)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


def _collect(tmp_path: Path, repo: Path, report: dict[str, str] | None = None) -> Path:
    worker_report = tmp_path / "worker-report"
    output = tmp_path / "output"
    worker_report.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    _write(worker_report, report or {})
    (output / "prepared-base.txt").write_text(_git(repo, "rev-parse", "main") + "\n")
    script = (
        collector_script(base_ref="main", work_branch="crucible/test", size_cap_bytes=1 << 20)
        .replace("/crucible/report", str(worker_report))
        .replace("/crucible/repo", str(repo))
        .replace("/crucible/out", str(output))
    )
    result = subprocess.run(
        ["sh", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        env=collector_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    return output


def _read(tmp_path: Path, output: Path) -> Any:
    spec = LaunchSpec(
        attempt_id="A1",
        task_id="T1",
        external_id="X1",
        role="implement",
        harness="test",
        model="test",
        image="test",
        timeout_seconds=1,
        contract={"repository": {"base_ref": "main", "work_branch": "crucible/test"}},
    )
    return read_outputs(
        output,
        tmp_path / "verify",
        spec=spec,
        bundle_verified=False,
        collector_exit=0,
        verifications=(),
        tail_bytes=1024,
    )


def _outputs(collected: Any) -> CollectedOutputs:
    return CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        diff_paths=collected.diff_paths,
        diff_findings=collected.diff_findings,
        diff_unscanned=collected.diff_unscanned,
        artifacts=collected.artifacts,
        secret_declarations=collected.secret_declarations,
    )


def _gate(findings: list[dict[str, Any]], scanned: int = 2) -> Any:
    item = EvidenceItem(
        id=1,
        kind="scanner_result",
        source="crucible",
        verified=True,
        payload={
            "findings": findings,
            "scanned": [f"input{i}" for i in range(scanned)],
            "diff_scanned": True,
        },
    )
    return no_secrets(GateInput(contract={}, policy={}, head_sha=None, evidence=(item,)))


# AC1: input, line, rule and a redacted excerpt for every match.


def test_the_excerpt_is_the_first_four_and_last_three_characters_and_the_length() -> None:
    assert redact_excerpt(KEY) == "ghp_...AAA (40 chars)"
    assert redact_excerpt(AWS) == "AKIA...QQQ (20 chars)"


def test_every_secret_on_a_line_is_redacted_in_its_excerpt() -> None:
    matches = match_line(f"a = '{KEY}'; b = '{AWS}'", path="diff:x.py", line=7)
    assert [(m.path, m.line, m.pattern) for m in matches] == [
        ("diff:x.py", 7, "github_token"),
        ("diff:x.py", 7, "aws_access_key"),
    ]
    for match in matches:
        assert "[ghp_...AAA (40 chars)]" in match.context
        assert "[AKIA...QQQ (20 chars)]" in match.context
        _no_value(repr(match))


def test_a_diff_match_names_its_path_and_new_file_line(tmp_path: Path) -> None:
    base = "one\ntwo\nthree\nfour\nfive\nsix\nseven\n"
    changed = "one\ntwo\nthree\nfour\nfive\n" + f"token = '{KEY}'\n" + "six\nseven\n"
    repo = _repository(tmp_path, {"src/app.py": base}, {"src/app.py": changed})
    findings, _ = scan_changed_content(_collect(tmp_path, repo))
    assert findings is not None
    assert [(m.path, m.line, m.pattern, m.excerpt) for m in findings] == [
        ("diff:src/app.py", 6, "github_token", "ghp_...AAA (40 chars)")
    ]
    assert findings[0].context == "token = '[ghp_...AAA (40 chars)]'"


def test_the_gate_detail_names_input_line_rule_and_excerpt_of_every_match(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        {},
        {"src/app.py": f"x = 1\ntoken = '{KEY}'\n", "src/aws.py": f"key = '{AWS}'\n"},
    )
    output = _collect(tmp_path, repo, report={"notes.txt": f"plain\nsaw {OTHER}\n"})
    findings = _scanner_findings(_outputs(_read(tmp_path, output)), None)
    outcome = _gate(findings)
    assert outcome.result is GateResult.FAIL
    for expected in (
        "diff:src/app.py line 2, rule github_token, value ghp_...AAA (40 chars), "
        "in: token = '[ghp_...AAA (40 chars)]'",
        "diff:src/aws.py line 1, rule aws_access_key, value AKIA...QQQ (20 chars)",
        "artifact:report/notes.txt line 2, rule github_token, value ghp_...BBB (40 chars), "
        "in: saw [ghp_...BBB (40 chars)]",
    ):
        assert expected in outcome.detail
    assert "secret pattern matched at 3 place(s)" in outcome.detail
    _no_value(outcome.detail)
    _no_value(json.dumps(findings))


def test_an_old_finding_without_a_line_still_reads() -> None:
    outcome = _gate([{"where": "diff:x", "pattern": "github_token", "excerpt": "ghp...AAA"}])
    assert outcome.result is GateResult.FAIL
    assert "diff:x, rule github_token, value ghp...AAA" in outcome.detail


# AC2: a transcript match keeps a redacted window as an attempt artifact.


def _transcript() -> bytes:
    lines = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "cat config/app.env"}}
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": KEY}]}},
        {"type": "user", "message": {"content": f"AWS_KEY={AWS}"}},
        {"type": "assistant", "message": {"content": "next"}},
        {"type": "assistant", "message": {"content": "later"}},
        {"type": "assistant", "message": {"content": "much later"}},
        {"type": "assistant", "message": {"content": "outside the window"}},
    ]
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()


class _Evidence:
    def __init__(self) -> None:
        self.rows: list[EvidenceRecord] = []

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        record.id = len(self.rows) + 1
        self.rows.append(record)
        return record

    def list_for_attempt(self, attempt_id: str) -> list[EvidenceRecord]:
        return [r for r in self.rows if r.attempt_id == attempt_id]

    def list_for_task(self, task_id: str) -> list[EvidenceRecord]:
        return [r for r in self.rows if r.task_id == task_id]


class _Artifacts:
    def __init__(self) -> None:
        self.rows: dict[str, Artifact] = {}

    def add(self, artifact: Artifact) -> None:
        self.rows[artifact.id] = artifact

    def find_by_sha256(self, sha256: str, attempt_id: str | None) -> Artifact | None:
        return next(
            (a for a in self.rows.values() if a.sha256 == sha256 and a.attempt_id == attempt_id),
            None,
        )


def _task() -> Task:
    return Task(
        id="T1",
        external_id="X1",
        principal_id="P1",
        project="hades",
        title="t",
        state=TaskState.RUNNING,
        contract_version=1,
        policy_name="default",
        policy_version=1,
        repository_id="R1",
        created_at=NOW,
        updated_at=NOW,
    )


def _attempt() -> Attempt:
    return Attempt(
        id="A1",
        execution_id="E1",
        task_id="T1",
        number=1,
        state=AttemptState.RUNNING,
        created_at=NOW,
        exit_code=0,
    )


def _record(tmp_path: Path, outputs: CollectedOutputs) -> tuple[Any, Path]:
    uow: Any = SimpleNamespace(
        evidence=_Evidence(), artifacts=_Artifacts(), events=SimpleNamespace(append=lambda e: e)
    )
    store = tmp_path / "store"
    record_collection_evidence(
        uow,
        SimpleNamespace(now=lambda: NOW),
        DiskArtifactStore(store),
        attempt=_attempt(),
        task=_task(),
        outputs=outputs,
        claim=None,
        claim_parsed_ok=False,
        parse_errors=[],
    )
    return uow, store


def _transcript_outputs(content: bytes, **extra: Any) -> CollectedOutputs:
    return CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        diff_findings=(),
        artifacts=(
            CollectedArtifact(
                name="report/transcript.jsonl",
                type="run_evidence",
                content=content,
                content_type="text/plain",
            ),
        ),
        **extra,
    )


def test_a_transcript_match_names_the_command_and_keeps_a_redacted_window(
    tmp_path: Path,
) -> None:
    uow, store = _record(tmp_path, _transcript_outputs(_transcript()))
    windows = [a for a in uow.artifacts.rows.values() if a.filename == TRANSCRIPT_WINDOW_ARTIFACT]
    assert len(windows) == 1
    assert windows[0].type == "secret_scan_window"
    # The transcript itself holds the value, so the store refused it.
    assert not any(a.filename == "report/transcript.jsonl" for a in uow.artifacts.rows.values())
    text = (store / windows[0].path).read_text(encoding="utf-8")
    assert "line 3, rule github_token, command: cat config/app.env" in text
    assert "> 3: " in text and "  1: " in text and "  6: " in text
    assert "  7: " not in text.split("line 4,")[0]
    # The other secret-shaped string in the window is redacted too.
    assert "[ghp_...AAA (40 chars)]" in text
    assert "[AKIA...QQQ (20 chars)]" in text
    assert "outside the window" not in text.split("line 4,")[0]
    _no_value(text)
    for path in store.rglob("*"):
        if path.is_file():
            _no_value(path.read_bytes().decode("utf-8", "replace"))
    present = [r for r in uow.evidence.rows if r.payload.get("role") == "secret_scan_window"]
    assert present and present[0].artifact_id == windows[0].id
    scanner = next(r for r in uow.evidence.rows if r.kind == "scanner_result")
    first = scanner.payload["findings"][0]
    assert first["where"] == "artifact:report/transcript.jsonl"
    assert first["line"] == 3
    assert first["command"] == "cat config/app.env"
    assert first["window"] == TRANSCRIPT_WINDOW_ARTIFACT
    # One finding per match, not a second one for the store's refusal.
    assert [f["line"] for f in scanner.payload["findings"]] == [3, 4]
    outcome = no_secrets(
        GateInput(contract={}, policy={}, head_sha=None, evidence=evidence_items(uow, "A1", "T1"))
    )
    assert outcome.result is GateResult.FAIL
    assert "artifact:report/transcript.jsonl line 3, rule github_token" in outcome.detail
    assert "printed by the command: cat config/app.env" in outcome.detail
    assert f"redacted window kept as artifact {TRANSCRIPT_WINDOW_ARTIFACT}" in outcome.detail


def test_no_window_is_kept_when_the_transcript_is_clean(tmp_path: Path) -> None:
    clean = b'{"type": "system"}\n'
    assert _transcript_windows(_transcript_outputs(clean), []) is None
    uow, _ = _record(tmp_path, _transcript_outputs(clean))
    assert not any(a.filename == TRANSCRIPT_WINDOW_ARTIFACT for a in uow.artifacts.rows.values())


def test_a_long_transcript_line_is_cut_in_the_window() -> None:
    lines = [json.dumps({"output": "x" * 5000 + KEY})]
    window = redact_line(lines[0], 400)
    assert len(window) <= 405
    _no_value(window)


# AC3: repository-declared fixtures and allowlist entries are honoured.

GITLEAKS_TOML = """title = "test"

[allowlist]
paths = ['''^fixtures/''']
"""


def test_declared_fingerprint_and_allowlisted_path_are_skipped_in_the_diff(
    tmp_path: Path,
) -> None:
    base = {
        ".gitleaksignore": "# the test fixture\ntests/fixture.py:crucible-github-token:2\n",
        ".gitleaks.toml": GITLEAKS_TOML,
    }
    change = {
        "tests/fixture.py": f"x = 1\nfake = '{KEY}'\nother = '{OTHER}'\n",
        "fixtures/key.txt": f"{KEY}\n",
        "src/app.py": f"token = '{KEY}'\n",
    }
    output = _collect(tmp_path, _repository(tmp_path, base, change))
    findings, _ = scan_changed_content(output)
    assert findings is not None
    assert sorted((m.path, m.line) for m in findings) == [
        ("diff:src/app.py", 1),
        ("diff:tests/fixture.py", 3),
    ]
    assert not any(m.advisory for m in findings)


def test_a_declaration_the_worker_adds_does_not_allow_its_own_match(tmp_path: Path) -> None:
    change = {
        ".gitleaksignore": "src/app.py:crucible-github-token:1\n",
        ".gitleaks.toml": GITLEAKS_TOML.replace("^fixtures/", "^src/"),
        "src/app.py": f"token = '{KEY}'\n",
    }
    output = _collect(tmp_path, _repository(tmp_path, {}, change))
    findings, _ = scan_changed_content(output)
    assert findings is not None
    assert [(m.path, m.line, m.advisory) for m in findings] == [("diff:src/app.py", 1, False)]


def test_a_declared_fixture_value_is_advisory_wherever_it_appears(tmp_path: Path) -> None:
    base = {".gitleaks.toml": GITLEAKS_TOML, "fixtures/token.txt": f"{FIXTURE}\n"}
    change = {"docs/example.md": f"Run with `{FIXTURE}`.\n"}
    transcript = json.dumps({"type": "tool_result", "content": FIXTURE}) + "\n"
    output = _collect(
        tmp_path, _repository(tmp_path, base, change), report={"transcript.jsonl": transcript}
    )
    collected = _read(tmp_path, output)
    assert collected.secret_declarations is not None
    assert [(m.path, m.advisory) for m in collected.diff_findings] == [
        ("diff:docs/example.md", True)
    ]
    uow, _ = _record(tmp_path, _outputs(collected))
    scanner = next(r for r in uow.evidence.rows if r.kind == "scanner_result")
    assert {f["where"]: f.get("advisory") for f in scanner.payload["findings"]} == {
        "diff:docs/example.md": True,
        "artifact:report/transcript.jsonl": True,
    }
    outcome = no_secrets(
        GateInput(contract={}, policy={}, head_sha=None, evidence=evidence_items(uow, "A1", "T1"))
    )
    assert outcome.result is GateResult.PASS, outcome.detail
    assert "2 advisory match(es) of a fixture value the repository declares" in outcome.detail
    assert len(outcome.findings) == 2
    assert all(f.startswith("advisory, a fixture value") for f in outcome.findings)
    assert any("diff:docs/example.md line 1" in f for f in outcome.findings)
    assert any("artifact:report/transcript.jsonl line 1" in f for f in outcome.findings)
    _no_value(outcome.detail + " ".join(outcome.findings))


def test_a_new_value_beside_a_declared_fixture_still_blocks(tmp_path: Path) -> None:
    base = {".gitleaks.toml": GITLEAKS_TOML, "fixtures/token.txt": f"{FIXTURE}\n"}
    change = {"docs/example.md": f"fixture `{FIXTURE}`, real `{KEY}`\n"}
    output = _collect(tmp_path, _repository(tmp_path, base, change))
    findings = _scanner_findings(_outputs(_read(tmp_path, output)), None)
    assert [f.get("advisory", False) for f in findings] == [True, False]
    outcome = _gate(findings)
    assert outcome.result is GateResult.FAIL
    assert "secret pattern matched at 1 place(s)" in outcome.detail
    assert outcome.findings and "ghp_...FFF" in outcome.findings[0]


def test_gitleaksignore_parsing_keeps_tree_fingerprints_only() -> None:
    commit = "a" * 40
    parsed = parse_gitleaksignore(
        "# comment\n\n"
        "tests/a.py:crucible-github-token:3\n"
        f"{commit}:tests/a.py:crucible-github-token:3\n"
        "weird:path.py:generic-api-key:10\n"
        "not a fingerprint\n"
    )
    assert parsed == {"tests/a.py:crucible-github-token:3", "weird:path.py:generic-api-key:10"}


def test_gitleaks_allowlists_are_read_as_gitleaks_reads_them() -> None:
    config = """
[[allowlists]]
targetRules = ["crucible-github-token"]
paths = ['''^docs/''']

[[allowlists]]
condition = "AND"
paths = ['''^tests/''']
stopwords = ["bbbb"]
"""
    declared = declarations("", config)
    line = "x"
    assert declared.allowed("docs/a.md", "github_token", 1, KEY, line)
    assert not declared.allowed("docs/a.md", "aws_access_key", 1, AWS, line)
    assert declared.allowed("tests/a.py", "github_token", 1, OTHER, line)
    assert not declared.allowed("tests/a.py", "github_token", 1, KEY, line)
    assert not declared.allowed("src/a.py", "github_token", 1, OTHER, line)
    # An AND allowlist with a stopword does not allow a whole path.
    assert not declared.path_allowed("tests/a.py")
    assert parse_gitleaks_config("not toml [") == ()


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not on PATH")
def test_the_scanner_and_make_scan_agree_on_what_is_allowed(tmp_path: Path) -> None:
    """`make scan-tree` runs gitleaks from inside a copy of the tree with `--source .`;
    the scanner, given the same files, allows exactly what gitleaks allows."""
    root = Path(__file__).resolve().parents[2]
    rules = (root / ".gitleaks.toml").read_text(encoding="utf-8")
    config = rules + "\n[allowlist]\npaths = ['''^fixtures/''']\nstopwords = ['bbbb']\n"
    ignore = "tests/fixture.py:crucible-github-token:2\n"
    files = {
        "tests/fixture.py": f"x = 1\nfake = '{KEY}'\nother = '{KEY}'\n",
        "fixtures/key.txt": f"{KEY}\n",
        "src/app.py": f"token = '{KEY}'\nword = '{OTHER}'\n",
    }
    tree = tmp_path / "tree"
    _write(tree, {**files, ".gitleaks.toml": config, ".gitleaksignore": ignore})
    report = tmp_path / "gitleaks.json"
    subprocess.run(
        [
            "gitleaks",
            "detect",
            "--no-git",
            "--no-banner",
            "--redact",
            "--source",
            ".",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        cwd=tree,
        capture_output=True,
        check=False,
    )
    reported = {
        (f["File"], f["StartLine"])
        for f in json.loads(report.read_text(encoding="utf-8"))
        if f["RuleID"].startswith("crucible-")
    }
    declared = declarations(ignore, config)
    scanned = set()
    for name, content in files.items():
        if declared.path_allowed(name):
            continue
        for number, line in enumerate(content.splitlines(), start=1):

            def skip(rule: str, value: str, whole: str, name: str = name, n: int = number) -> bool:
                return declared.allowed(name, rule, n, value, whole)

            if match_line(line, path=name, line=number, skip=skip):
                scanned.add((name, number))
    assert scanned == reported == {("tests/fixture.py", 3), ("src/app.py", 1)}
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    assert '(cd "$$T" && gitleaks detect --no-git --redact --no-banner --source .)' in makefile


# AC4: the composed correction says what produced the match and how to avoid it.


def test_the_correction_names_the_file_line_rule_and_the_advice() -> None:
    text = no_secrets_correction(
        [
            {
                "where": "diff:tests/test_a.py",
                "pattern": "github_token",
                "excerpt": "ghp_...AAA (40 chars)",
                "line": 12,
                "context": "TOKEN = '[ghp_...AAA (40 chars)]'",
            }
        ]
    )
    assert "the file `tests/test_a.py` you changed, line 12 (rule github_token" in text
    assert "TOKEN = '[ghp_...AAA (40 chars)]'" in text
    assert "angle-bracket placeholder such as `<github-token>`" in text
    assert "Build a fake value at run time" in text
    assert "`grep -l` or `grep -c`" in text
    assert text.endswith(NO_SECRETS_ADVICE)


def test_the_correction_names_the_transcript_command(tmp_path: Path) -> None:
    findings = _scanner_findings(_transcript_outputs(_transcript()), None)
    text = no_secrets_correction(findings)
    assert (
        "the transcript (report/transcript.jsonl) line 3, from the command "
        "`cat config/app.env` (rule github_token" in text
    )
    assert f"kept as {TRANSCRIPT_WINDOW_ARTIFACT}" in text
    _no_value(text)


def test_advisory_matches_compose_no_correction() -> None:
    assert no_secrets_correction([{"where": "diff:a", "pattern": "x", "advisory": True}]) == ""


def _uow_with_failed_scan(findings: list[dict[str, Any]], result: str = "fail") -> Any:
    execution = Execution(
        id="E1",
        task_id="T1",
        role=ExecutionRole.IMPLEMENT,
        contract_version=1,
        harness="h",
        model="m",
        effort=None,
        provider="docker",
        image="i",
        policy_snapshot={},
        state=ExecutionState.SUCCEEDED,
        max_attempts=1,
        retry_on=[],
        timeout_seconds=1,
        created_at=NOW,
    )
    scanner = EvidenceRecord(
        id=1,
        attempt_id="A1",
        task_id="T1",
        kind="scanner_result",
        observed_at=NOW,
        source="crucible",
        verified=True,
        payload={"findings": findings},
    )
    return SimpleNamespace(
        executions=SimpleNamespace(list_for_task=lambda _t: [execution]),
        attempts=SimpleNamespace(list_for_execution=lambda _e: [_attempt()]),
        gate_results=SimpleNamespace(
            list_for_attempt=lambda _a: [SimpleNamespace(gate="no_secrets", result=result)]
        ),
        evidence=SimpleNamespace(list_for_attempt=lambda _a: [scanner]),
    )


DIFF_FINDING = {
    "where": "diff:src/app.py",
    "pattern": "github_token",
    "excerpt": "ghp_...AAA (40 chars)",
    "line": 1,
}


def test_a_correction_after_a_no_secrets_failure_carries_hades_guidance() -> None:
    uow = _uow_with_failed_scan([DIFF_FINDING])
    guidance = no_secrets_guidance(uow, _task())
    assert "the file `src/app.py` you changed, line 1 (rule github_token" in guidance
    contract = TaskContractV1.model_validate(
        contract_document(
            correction={
                "of_version": 1,
                "reason": "pre_pr_gates",
                "addresses": [],
                "instructions": "Remove the token.",
                "request_internal_review": False,
            }
        )
    )
    guided, changed = _with_no_secrets_guidance(uow, _task(), contract)
    assert changed
    assert guided.correction is not None
    assert guided.correction.instructions == f"Remove the token.\n\n{guidance}"
    # Words that already carry it are left as they are.
    again, changed_again = _with_no_secrets_guidance(uow, _task(), guided)
    assert not changed_again and again.correction == guided.correction


def test_no_guidance_when_the_scan_passed() -> None:
    assert no_secrets_guidance(_uow_with_failed_scan([DIFF_FINDING], "pass"), _task()) == ""


def test_the_gate_failure_wake_carries_the_composed_correction() -> None:
    item = EvidenceItem(
        id=1,
        kind="scanner_result",
        source="crucible",
        verified=True,
        payload={"findings": [DIFF_FINDING]},
    )
    text = _secret_guidance(GateInput(contract={}, policy={}, head_sha=None, evidence=(item,)))
    assert "Hades adds this to that correction's instructions" in text
    assert "the file `src/app.py` you changed, line 1" in text
    assert _secret_guidance(GateInput(contract={}, policy={}, head_sha=None, evidence=())) == ""


@pytest.mark.timeout(5)
def test_collected_text_does_not_wait_for_a_fifo_or_follow_a_link(tmp_path: Path) -> None:
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    link = tmp_path / "link"
    link.symlink_to(pipe)
    for path in (pipe, link):
        assert collected.text(path) == ""
        with pytest.raises(OSError):
            collected._read_regular(path, 1024)


def test_artifact_read_is_bounded_before_allocation(tmp_path: Path) -> None:
    output = tmp_path / "output"
    report = output / "report"
    report.mkdir(parents=True)
    transcript = report / "transcript.jsonl"
    # A sparse file costs little disk space but would force the old reader to allocate
    # the entire 64 MiB before slicing it down to 4 MiB.
    with transcript.open("wb") as handle:
        handle.write(b"plain transcript\n")
        handle.seek(64 * 1024 * 1024 - 1)
        handle.write(b"\n")
    tracemalloc.start()
    try:
        result = _read(tmp_path, output)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(result.artifacts) == 1
    assert len(result.artifacts[0].content) == collected.ARTIFACT_LIMIT
    assert peak < 3 * collected.ARTIFACT_LIMIT


@pytest.mark.parametrize("budget", ["DIFF_SCAN_LIMIT", "DIFF_LINE_LIMIT", "DIFF_FINDING_LIMIT"])
def test_diff_budget_exhaustion_is_explicitly_unscanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    (tmp_path / "diff.patch").write_text(
        "diff --git a/app b/app\n+++ b/app\n@@ -0,0 +1,2 @@\n" + f"+{KEY}\n+{OTHER}\n"
    )
    monkeypatch.setattr(collected, budget, 1)
    _, unscanned = scan_changed_content(tmp_path)
    assert unscanned == ("diff.patch",)


def test_command_attribution_bounds_json_and_tolerates_deep_output() -> None:
    oversized = json.dumps({"command": "x" * COMMAND_INPUT_LIMIT})
    deep = '{"command":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    lines = [json.dumps({"command": "grep -l pattern config"}), oversized, deep]
    assert transcript_command(lines, 3) == "grep -l pattern config"


def test_fixture_search_has_a_deadline_and_disables_lazy_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scripts, "SECRET_DECLARATION_SECONDS", 1)
    monkeypatch.setattr(scripts, "named_secret_pattern_expressions", lambda: (("test", "test"),))
    git = tmp_path / "git"
    git.write_text(
        '#!/bin/sh\ncase " $* " in\n'
        '  *" grep "*) printf "%s" "$GIT_NO_LAZY_FETCH" > "$OUT/lazy-fetch"; sleep 60;;\n'
        "  *) exit 1;;\nesac\n"
    )
    git.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "OUT": str(tmp_path)}
    subprocess.run(
        ["sh", "-c", scripts._secret_declarations_script()],
        env=env,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert (tmp_path / "lazy-fetch").read_text() == "1"
