"""hades #196: detect flaky tests from recorded run data and dedup their issues.

No network: `detect_flaky_tests` and `plan_issue_updates` take recorded run data and
an in-memory JUnit lookup, never a live GitHub call (tools/ci/flakes.py).
"""

from __future__ import annotations

from tools.ci import flakes

UNIT_XML_WITH_ONE_FAILURE = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="2" failures="1" errors="0">
    <testcase classname="tests.unit.test_widget" name="test_passes" time="0.01"/>
    <testcase classname="tests.unit.test_widget" name="test_connect_times_out" time="1.2">
      <failure message="AssertionError">assert False</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parse_junit_failures_names_only_the_failing_and_erroring_cases() -> None:
    assert flakes.parse_junit_failures(UNIT_XML_WITH_ONE_FAILURE) == [
        "tests/unit/test_widget.py::test_connect_times_out"
    ]


def test_detect_flaky_tests_names_the_failing_test_and_the_flake_rate() -> None:
    # Run 42: the "test" job failed on attempt 1 and passed on attempt 2, same commit
    # (the run id). Run 43 is a plain green run with no rerun.
    runs = [
        flakes.RecordedRun(
            run_id=42,
            jobs=(
                flakes.JobAttempt("test", 1, "failure"),
                flakes.JobAttempt("test", 2, "success"),
            ),
        ),
        flakes.RecordedRun(run_id=43, jobs=(flakes.JobAttempt("test", 1, "success"),)),
    ]

    def junit_lookup(run_id: int, job_name: str) -> list[str]:
        assert (run_id, job_name) == (42, "test")
        return flakes.parse_junit_failures(UNIT_XML_WITH_ONE_FAILURE)

    findings = flakes.detect_flaky_tests(runs, junit_lookup)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.test_name == "tests/unit/test_widget.py::test_connect_times_out"
    assert finding.occurrences == 1
    assert finding.jobs == ("test",)
    assert finding.run_ids == (42,)
    # 1 occurrence over 2 scanned runs: 50 reruns per 100 runs.
    assert finding.rate_per_100 == 50.0


def test_detect_flaky_tests_ignores_a_job_that_only_failed_or_only_passed() -> None:
    runs = [
        # Failed on attempt 1, never retried: not a flake, just a failure.
        flakes.RecordedRun(run_id=1, jobs=(flakes.JobAttempt("test", 1, "failure"),)),
        # Passed outright: nothing to detect.
        flakes.RecordedRun(run_id=2, jobs=(flakes.JobAttempt("test", 1, "success"),)),
        # Failed attempt 1, failed again on attempt 2: still broken, not a flake.
        flakes.RecordedRun(
            run_id=3,
            jobs=(
                flakes.JobAttempt("test", 1, "failure"),
                flakes.JobAttempt("test", 2, "failure"),
            ),
        ),
    ]

    def junit_lookup(run_id: int, job_name: str) -> list[str]:
        raise AssertionError("no job in this fixture flaked; the lookup must not run")

    assert flakes.detect_flaky_tests(runs, junit_lookup) == []


def test_plan_issue_updates_updates_a_tracked_test_and_creates_for_a_new_one() -> None:
    tracked = flakes.FlakeFinding(
        test_name="tests/unit/test_widget.py::test_connect_times_out",
        occurrences=3,
        jobs=("test",),
        run_ids=(42, 43, 44),
        rate_per_100=3.0,
    )
    new = flakes.FlakeFinding(
        test_name="tests/e2e/test_login.py::test_login_retries",
        occurrences=1,
        jobs=("e2e",),
        run_ids=(50,),
        rate_per_100=1.0,
    )
    existing_issues = [
        flakes.OpenIssue(
            number=101,
            title=flakes.issue_title(tracked.test_name),
            labels=("flaky",),
        ),
        # Same title text, but not labelled flaky: must not be matched as tracking it.
        flakes.OpenIssue(number=102, title=flakes.issue_title(new.test_name), labels=()),
    ]

    plans = flakes.plan_issue_updates(existing_issues, [tracked, new], total_runs=100)

    assert len(plans) == 2
    by_test = {plan.test_name: plan for plan in plans}
    assert by_test[tracked.test_name].action == "update"
    assert by_test[tracked.test_name].number == 101
    assert by_test[new.test_name].action == "create"
    assert by_test[new.test_name].number is None


def test_plan_issue_updates_is_one_issue_per_test_name_not_a_duplicate() -> None:
    # The same test flaked in two different scan runs; an issue for it already exists.
    finding = flakes.FlakeFinding(
        test_name="tests/unit/test_widget.py::test_connect_times_out",
        occurrences=5,
        jobs=("test",),
        run_ids=(1, 2, 3, 4, 5),
        rate_per_100=5.0,
    )
    existing_issues = [
        flakes.OpenIssue(number=7, title=flakes.issue_title(finding.test_name), labels=("flaky",))
    ]

    first_plans = flakes.plan_issue_updates(existing_issues, [finding], total_runs=100)
    second_plans = flakes.plan_issue_updates(existing_issues, [finding], total_runs=100)

    assert [plan.action for plan in first_plans] == ["update"]
    assert [plan.number for plan in first_plans] == [7]
    # Scanning again (next week) with the issue still open still updates #7, never a
    # second issue for the same test.
    assert first_plans == second_plans
