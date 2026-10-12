"""FDY-0630: harness attribution stays in Hades, not GitHub co-authors."""

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from crucible.adapters.execution.scripts import commit_msg_hook
from crucible.adapters.github import normalize
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.qwen_code import QwenCodeAdapter
from crucible.domain.publication import body_sha256
from crucible.ports.github import InstallationToken
from crucible.ports.harness import LaunchContext
from tests.unit.test_issue_337_auto_merge import BODY, TITLE
from tests.unit.test_issue_337_auto_merge import setup as merge_setup


def test_claude_settings_disable_commit_and_pr_attribution() -> None:
    settings = json.loads(ClaudeCodeAdapter().credential_spec().templates["settings.json"])
    assert settings["attribution"]["commit"] == ""
    assert settings["attribution"]["pr"] == ""


def test_qwen_launch_disables_attribution_without_a_credential(tmp_path: Path) -> None:
    launch = QwenCodeAdapter().build_launch(
        LaunchContext(
            attempt_id="attempt",
            model="model",
            effort=None,
            timeout_seconds=60,
            identity_mount=str(tmp_path / "identity"),
            report_mount=str(tmp_path / "report"),
            repo_mount=str(tmp_path),
            endpoint="local",
            endpoint_url="https://gateway.invalid/v1",
        )
    )
    # Execute the real launch prelude, substituting only the model-calling wrapper.
    assert launch.argv[:2] == ("/bin/sh", "-c")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, pathlib, sys\n"
        "path = pathlib.Path(os.environ['QWEN_CODE_SYSTEM_SETTINGS_PATH'])\n"
        "print(json.dumps({'settings': json.loads(path.read_text()), 'args': sys.argv[1:]}))\n"
        "path.unlink()\n"
    )
    result = subprocess.run(
        [*launch.argv[:4], "python3", str(probe), *launch.argv[5:]],
        text=True,
        capture_output=True,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["settings"]["general"]["gitCoAuthor"] == {"commit": False, "pr": False}
    assert observed["args"] == list(launch.argv[5:])
    assert launch.env_from_files == {}


def test_git_commit_runs_the_installed_hook(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    hook = tmp_path / ".git" / "hooks" / "commit-msg"
    hook.write_text(commit_msg_hook(trailer="Crucible-Attempt", value="task/attempt"))
    hook.chmod(0o755)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=worker",
            "-c",
            "user.email=worker@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "Fix behavior\n\nCo-Authored-By: Claude <claude@example.invalid>",
        ],
        cwd=tmp_path,
        check=True,
    )
    result = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "co-authored-by" not in result.lower()
    assert "Crucible-Attempt: task/attempt" in result


@pytest.mark.parametrize(
    "coauthors",
    [
        "Co-Authored-By: Claude <claude@example.invalid>\n",
        "co-authored-by: worker <worker@example.invalid>\n"
        "CO-AUTHORED-BY: model <model@example.invalid>\n",
        "\tCo-AuThOrEd-By : model <model@example.invalid>\r\n",
        "",
    ],
)
def test_hook_strips_coauthors_and_preserves_attempt(tmp_path: Path, coauthors: str) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    message = tmp_path / "message with spaces"
    message.write_text(
        "Fix behavior\n\nKeep this prose about Co-Authored-By trailers.\n\n"
        "---\nDetails below divider.\n\n"
        + coauthors
        + "Signed-off-by: Reviewer <reviewer@example.invalid>\n"
    )
    hook = tmp_path / "commit-msg"
    attempt = "task/attempt harness=claude_code model=example"
    hook.write_text(commit_msg_hook(trailer="Crucible-Attempt", value=attempt))
    for _ in range(2):
        subprocess.run(["sh", str(hook), str(message)], cwd=tmp_path, check=True)
    result = message.read_text()
    assert not any(
        line.lstrip().lower().startswith("co-authored-by") for line in result.splitlines()
    )
    assert "Keep this prose about Co-Authored-By trailers." in result
    assert "---\nDetails below divider." in result
    assert "Signed-off-by: Reviewer <reviewer@example.invalid>" in result
    assert result.count(f"Crucible-Attempt: {attempt}") == 1


@pytest.mark.parametrize(
    "message",
    [
        "",
        "Evidence\n\nCo-Authored-By: model <model@example.invalid>\n"
        "co-authored-by: worker <worker@example.invalid>\nCloses #602\n",
    ],
)
def test_squash_request_carries_the_given_text_without_coauthors(message: str) -> None:
    """The adapter sends the title and message it is given and never reads the live
    pull request for them; a co-author line is dropped as a backstop."""
    transport = Mock(spec=RestTransport)
    transport.get.return_value = {
        "title": "Edited on GitHub",
        "body": "Edited on GitHub",
        "number": 7,
        "state": "closed",
        "merged": True,
        "merged_at": "2026-10-11T12:00:00-05:00",
        "merged_by": {"login": "hades[bot]"},
    }
    transport.request.return_value = (200, {"merged": True, "sha": "merge"}, {})
    token = InstallationToken(
        "<installation-token>", expires_at=datetime.now(UTC), repository="o/r"
    )
    result = RestGitHubClient(Mock(), transport).merge_pull_request(
        token,
        repository="o/r",
        number=7,
        expected_head_sha="certified-head",
        commit_title="Fix behavior",
        commit_message=message,
    )
    transport.request.assert_called_once_with(
        "PUT",
        "/repos/o/r/pulls/7/merge",
        bearer=token.reveal(),
        body={
            "merge_method": "squash",
            "sha": "certified-head",
            "commit_title": "Fix behavior",
            "commit_message": "Evidence\n\nCloses #602\n" if message else "",
        },
    )
    # The only read is the post-merge one that supplies the merger and time.
    transport.get.assert_called_once()
    assert result.merged_by == "hades[bot]"


def test_normalized_pull_request_carries_the_body_with_lf_endings() -> None:
    ref = normalize.pull_request({"number": 7, "body": "Summary\r\n\r\nDetail\r\n"})
    assert ref.body == "Summary\n\nDetail\n"
    assert normalize.pull_request({"number": 7, "body": None}).body == ""


@pytest.mark.asyncio
async def test_merge_sends_the_published_title_and_verified_body() -> None:
    coordinator, github, uow, _task, plan = merge_setup()

    assert await coordinator._merge_one(plan) is True

    assert github.merge_texts == [(TITLE, BODY)]
    assert plan.body_sha256 == body_sha256(BODY)
    assert uow.pull_requests.get.return_value.merged_by == "hades[bot]"


@pytest.mark.parametrize(
    ("field", "value", "cause"),
    [
        ("title", "Edited title", "pull request title changed since Hades published it"),
        ("body", BODY + "\nCloses #1\n", "pull request body changed since Hades published it"),
        (
            "body",
            BODY + "Co-authored-by: someone <someone@example.invalid>\n",
            "pull request body changed since Hades published it",
        ),
    ],
)
@pytest.mark.asyncio
async def test_edited_pull_request_text_is_refused_not_merged(
    field: str, value: str, cause: str
) -> None:
    """A collaborator's edit after publication never reaches main: the live text must
    match the stored title and body hash, else the merge is refused with a wake."""
    coordinator, github, uow, _task, plan = merge_setup()
    setattr(github, field, value)

    assert await coordinator._merge_one(plan) is False

    assert github.merge_calls == []
    pull_request = uow.pull_requests.get.return_value
    assert pull_request.merge_refusal_cause is not None
    assert pull_request.merge_refusal_cause.startswith(cause)
    wake = uow.wakes.add.call_args.args[0]
    assert wake.reason == "ready_for_merge"
    assert cause in wake.payload["summary"]


@pytest.mark.asyncio
async def test_pull_request_without_a_recorded_body_hash_is_not_merged() -> None:
    """A row that never recorded what Hades published cannot be verified, so it waits
    for a person rather than copying unverified text onto main."""
    coordinator, github, uow, _task, plan = merge_setup()
    plan = replace(plan, body_sha256="")

    assert await coordinator._merge_one(plan) is False

    assert github.merge_calls == []
    assert "body changed since Hades published it" in (
        uow.pull_requests.get.return_value.merge_refusal_cause or ""
    )
