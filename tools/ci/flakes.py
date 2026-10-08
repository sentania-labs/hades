"""Detect and track flaky tests from recorded CI run data (hades #196).

A job that fails on attempt 1 and passes on a later attempt at the same commit is a
flake: nothing about the code changed between attempts, only the result did. CI keeps
JUnit XML from the `test`, `e2e` and `e2e-kind` jobs as artifacts (`.github/workflows/
ci.yml`); this script scans recent runs of that workflow, reads the failing tests from
each flaky job's attempt-1 artifact, and opens or updates one GitHub issue per flaky
test, labelled `flaky`, with the flake rate (reruns per 100 runs). It also writes a
weekly summary issue with the overall rate.

The detection and issue-dedup logic (`detect_flaky_tests`, `plan_issue_updates`) is
pure: it takes recorded run data and a JUnit lookup callable, never touches the
network itself, and is what tests/unit/test_issue_196_flaky_test_tracking.py proves.
Only `main` and the `fetch_*` / `apply_*` functions below it talk to GitHub, through
the `gh` CLI the runner already carries (the same tool `images-digest.yml` uses).

    flakes.py scan --repo owner/repo [--workflow ci.yml] [--lookback 100] [--apply]

Without --apply this only prints the report. `make flakes` runs this locally (set
REPO=owner/repo, and CRUCIBLE_FLAKES_APPLY=1 to file or update issues); the weekly
flakes.yml workflow passes --apply and github.repository.

Never mark a test flaky in code as a fix (CONTRIBUTING.md): a test that flakes gets
fixed, or rewritten, not quietly retried. This script is the record of that, not a
substitute for it.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

FLAKY_LABEL = "flaky"
SUMMARY_LABEL = "flaky-summary"
SUMMARY_TITLE = "Flaky test tracking: weekly summary"


@dataclass(frozen=True)
class JobAttempt:
    """One job's conclusion on one attempt of one CI run."""

    job_name: str
    attempt: int
    conclusion: str


@dataclass(frozen=True)
class RecordedRun:
    """One CI run: its id and every job attempt recorded for it.

    A rerun (`run_attempt > 1`) stays on the same run id and the same commit, so the
    run id alone is "the same commit" for flake purposes: GitHub never changes a run's
    commit between attempts.
    """

    run_id: int
    jobs: tuple[JobAttempt, ...]


JunitLookup = Callable[[int, str], Sequence[str]]


@dataclass(frozen=True)
class FlakeFinding:
    """A test that failed on attempt 1 and passed on a later attempt, at least once."""

    test_name: str
    occurrences: int
    jobs: tuple[str, ...]
    run_ids: tuple[int, ...]
    rate_per_100: float


def find_flaky_job_runs(runs: Iterable[RecordedRun]) -> list[tuple[RecordedRun, str]]:
    """Every (run, job_name) where attempt 1 failed and a later attempt passed."""
    found: list[tuple[RecordedRun, str]] = []
    for run in runs:
        by_job: dict[str, list[JobAttempt]] = defaultdict(list)
        for job in run.jobs:
            by_job[job.job_name].append(job)
        for job_name, attempts in by_job.items():
            first = next((a for a in attempts if a.attempt == 1), None)
            if first is None or first.conclusion != "failure":
                continue
            if any(a.attempt > 1 and a.conclusion == "success" for a in attempts):
                found.append((run, job_name))
    return found


def detect_flaky_tests(
    runs: Sequence[RecordedRun], junit_lookup: JunitLookup
) -> list[FlakeFinding]:
    """Flaky tests over recorded run data, named with their flake rate.

    For every (run, job) that flaked, reads the failing tests from that job's attempt-1
    JUnit artifact through `junit_lookup(run_id, job_name)`. The rate is reruns per 100
    of the runs scanned, per the issue's own framing ("3 runs needed a rerun" out of the
    last 100).
    """
    total_runs = len(runs) or 1
    occurrences: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for run, job_name in find_flaky_job_runs(runs):
        for test_name in junit_lookup(run.run_id, job_name):
            occurrences[test_name].append((run.run_id, job_name))
    findings = []
    for test_name, hits in sorted(occurrences.items()):
        run_ids = tuple(sorted({run_id for run_id, _ in hits}))
        jobs = tuple(sorted({job_name for _, job_name in hits}))
        rate = round(len(hits) / total_runs * 100, 2)
        findings.append(FlakeFinding(test_name, len(hits), jobs, run_ids, rate))
    return findings


def overall_rerun_rate(runs: Sequence[RecordedRun]) -> float:
    """Reruns per 100 runs: the share of scanned runs where any job needed attempt > 1."""
    total_runs = len(runs) or 1
    reran = sum(1 for run in runs if any(job.attempt > 1 for job in run.jobs))
    return round(reran / total_runs * 100, 2)


def parse_junit_failures(xml_bytes: bytes) -> list[str]:
    """The pytest node ids of every failing or erroring `<testcase>` in a JUnit report."""
    root = ElementTree.fromstring(xml_bytes)
    failing = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            failing.append(_node_id(case.get("classname") or "", case.get("name") or ""))
    return failing


def _node_id(classname: str, name: str) -> str:
    """`tests.unit.test_foo`, `test_bar` -> `tests/unit/test_foo.py::test_bar`.

    pytest's default (xunit2) JUnit report has no `file` attribute, only the dotted
    `classname`; this assumes a plain module path with no test class, which is this
    repository's convention (plain functions, no unittest-style classes under tests/).
    """
    if not classname:
        return name
    return f"{classname.replace('.', '/')}.py::{name}"


@dataclass(frozen=True)
class OpenIssue:
    """An open GitHub issue, as much of it as the dedup logic needs."""

    number: int
    title: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class IssueUpdate:
    """What to do about one flaky test's tracking issue."""

    action: str
    test_name: str
    number: int | None
    title: str
    body: str


def issue_title(test_name: str) -> str:
    return f"flaky: {test_name}"


def render_issue_body(finding: FlakeFinding, total_runs: int) -> str:
    return (
        f"`{finding.test_name}` failed on attempt 1 and passed on a later attempt at "
        f"the same commit, {finding.occurrences} time(s) in the last {total_runs} CI "
        f"runs scanned ({finding.rate_per_100:.2f} reruns per 100 runs).\n\n"
        f"Jobs: {', '.join(finding.jobs)}\n"
        f"Runs: {', '.join(str(run_id) for run_id in finding.run_ids)}\n\n"
        "Fix the test or rewrite its wait; do not mark it flaky in code (CONTRIBUTING.md).\n"
    )


def plan_issue_updates(
    existing: Sequence[OpenIssue], findings: Sequence[FlakeFinding], total_runs: int
) -> list[IssueUpdate]:
    """One action per flaky test: update its open `flaky` issue, or create one.

    Matches by title (`flaky: <test name>`) among open issues labelled `flaky`, so a
    test already tracked is updated once, never filed a second time.
    """
    by_title = {issue.title: issue for issue in existing if FLAKY_LABEL in issue.labels}
    plans = []
    for finding in findings:
        title = issue_title(finding.test_name)
        body = render_issue_body(finding, total_runs)
        match = by_title.get(title)
        if match is not None:
            plans.append(IssueUpdate("update", finding.test_name, match.number, title, body))
        else:
            plans.append(IssueUpdate("create", finding.test_name, None, title, body))
    return plans


def render_weekly_summary(findings: Sequence[FlakeFinding], runs: Sequence[RecordedRun]) -> str:
    lines = [
        f"Scanned the last {len(runs)} CI runs.",
        f"Flake rate: {overall_rerun_rate(runs):.2f} reruns per 100 runs.",
        "",
    ]
    if findings:
        lines.append("| test | occurrences | rate per 100 runs |")
        lines.append("| --- | --- | --- |")
        lines.extend(
            f"| `{finding.test_name}` | {finding.occurrences} | {finding.rate_per_100:.2f} |"
            for finding in findings
        )
    else:
        lines.append("No flaky tests detected.")
    return "\n".join(lines) + "\n"


# Everything below here talks to GitHub through the `gh` CLI and is not exercised by
# tests/unit/test_issue_196_flaky_test_tracking.py (no network, per the issue).


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, text=True, capture_output=True).stdout


def _gh_bytes(*args: str) -> bytes:
    return subprocess.run(["gh", *args], check=True, capture_output=True).stdout


def fetch_recent_runs(repo: str, workflow: str, lookback: int) -> list[dict[str, Any]]:
    per_page = max(1, min(lookback, 100))
    raw = _gh("api", f"repos/{repo}/actions/workflows/{workflow}/runs?per_page={per_page}")
    runs: list[dict[str, Any]] = json.loads(raw)["workflow_runs"]
    return runs[:lookback]


def fetch_attempt_jobs(repo: str, run_id: int, attempt: int) -> list[dict[str, Any]]:
    raw = _gh("api", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs")
    jobs: list[dict[str, Any]] = json.loads(raw)["jobs"]
    return jobs


def fetch_attempt_artifacts(repo: str, run_id: int, attempt: int) -> list[dict[str, Any]]:
    raw = _gh("api", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/artifacts")
    artifacts: list[dict[str, Any]] = json.loads(raw)["artifacts"]
    return artifacts


def build_recorded_run(repo: str, run: dict[str, Any]) -> RecordedRun:
    run_id = int(run["id"])
    attempts = int(run.get("run_attempt") or 1)
    jobs = [
        JobAttempt(
            job_name=str(job["name"]), attempt=attempt, conclusion=str(job.get("conclusion") or "")
        )
        for attempt in range(1, attempts + 1)
        for job in fetch_attempt_jobs(repo, run_id, attempt)
    ]
    return RecordedRun(run_id=run_id, jobs=tuple(jobs))


def artifact_name_for_job(job_name: str) -> str | None:
    """The attempt-1 JUnit artifact `ci.yml` uploads for a given job name."""
    if job_name in {"test", "e2e"}:
        return f"junit-{job_name}"
    match = re.fullmatch(r"e2e-kind \((\d+)\)", job_name)
    return f"junit-e2e-kind-{match.group(1)}" if match else None


def make_junit_lookup(repo: str) -> JunitLookup:
    def lookup(run_id: int, job_name: str) -> list[str]:
        artifact_name = artifact_name_for_job(job_name)
        if artifact_name is None:
            return []
        artifacts = fetch_attempt_artifacts(repo, run_id, 1)
        match = next(
            (a for a in artifacts if a["name"] == artifact_name and not a.get("expired")), None
        )
        if match is None:
            return []
        archive_bytes = _gh_bytes("api", f"repos/{repo}/actions/artifacts/{match['id']}/zip")
        failing: list[str] = []
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for name in archive.namelist():
                if name.endswith(".xml"):
                    failing.extend(parse_junit_failures(archive.read(name)))
        return failing

    return lookup


def fetch_open_issues(repo: str, label: str) -> list[OpenIssue]:
    raw = _gh("api", f"repos/{repo}/issues?state=open&labels={label}&per_page=100")
    payload: list[dict[str, Any]] = json.loads(raw)
    return [
        OpenIssue(
            number=int(issue["number"]),
            title=str(issue["title"]),
            labels=tuple(str(issue_label["name"]) for issue_label in issue["labels"]),
        )
        for issue in payload
    ]


def _issue_number_from_url(url: str) -> int:
    return int(url.strip().rsplit("/", 1)[-1])


def apply_issue_update(repo: str, plan: IssueUpdate) -> int:
    if plan.action == "update":
        assert plan.number is not None
        _gh("issue", "edit", str(plan.number), "--repo", repo, "--body", plan.body)
        return plan.number
    url = _gh(
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        plan.title,
        "--body",
        plan.body,
        "--label",
        FLAKY_LABEL,
    )
    return _issue_number_from_url(url)


def apply_summary(repo: str, body: str) -> int:
    existing = next(
        (i for i in fetch_open_issues(repo, SUMMARY_LABEL) if i.title == SUMMARY_TITLE), None
    )
    if existing is not None:
        _gh("issue", "edit", str(existing.number), "--repo", repo, "--body", body)
        return existing.number
    url = _gh(
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        SUMMARY_TITLE,
        "--body",
        body,
        "--label",
        SUMMARY_LABEL,
    )
    return _issue_number_from_url(url)


def _scan(args: argparse.Namespace) -> int:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print(
            "flakes.py: --repo owner/repo is required (or set GITHUB_REPOSITORY)", file=sys.stderr
        )
        return 2
    raw_runs = fetch_recent_runs(repo, args.workflow, args.lookback)
    runs = [build_recorded_run(repo, run) for run in raw_runs]
    findings = detect_flaky_tests(runs, make_junit_lookup(repo))
    summary = render_weekly_summary(findings, runs)
    print(summary)
    if not args.apply:
        print("flakes.py: dry run, pass --apply to file or update issues")
        return 0
    for plan in plan_issue_updates(fetch_open_issues(repo, FLAKY_LABEL), findings, len(runs)):
        number = apply_issue_update(repo, plan)
        print(f"flakes.py: {plan.action} issue #{number} for {plan.test_name}")
    summary_number = apply_summary(repo, summary)
    print(f"flakes.py: weekly summary issue #{summary_number}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan")
    scan_parser.add_argument("--repo", default=None)
    scan_parser.add_argument("--workflow", default="ci.yml")
    scan_parser.add_argument("--lookback", type=int, default=100)
    scan_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "scan":
        return _scan(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
