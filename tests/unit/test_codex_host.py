from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.wait import wait_until

HOST = Path(__file__).parents[2] / "images/worker/crucible-codex-host.py"

FAKE = r"""#!/usr/bin/env python3
import json, sys
thread = "thread-fixture"
marker = __MARKER_PATH__
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        assert request["params"]["capabilities"]["experimentalApi"] is True
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    elif method == "account/login/start":
        assert request["params"]["type"] == "chatgptAuthTokens"
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    elif method == "thread/start":
        print(json.dumps({"id": request["id"], "result": {"thread": {"id": thread}}}), flush=True)
    elif method == "turn/start":
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
        refresh = {"id": 99, "method": "account/chatgptAuthTokens/refresh",
                   "params": {"reason": "unauthorized",
                              "previousAccountId": "account-fixture"}}
        open(marker, "w").close()
        print(json.dumps(refresh), flush=True)
    elif request.get("id") == 99:
        assert request["result"]["accessToken"] == "access-new"
        item = {"method": "item/completed", "params": {
            "item": {"type": "agentMessage", "text": "finished"}}}
        print(json.dumps(item), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"threadId": thread}}), flush=True)
        break
"""


def _token(path: Path, access: str) -> None:
    temporary = path.with_suffix(".new")
    temporary.write_text(
        json.dumps(
            {
                "access_token": access,
                "account_id": "account-fixture",
                "expires_at": "2026-10-01T00:00:00+00:00",
            }
        )
    )
    os.replace(temporary, path)


def _command(tmp_path: Path, fake: Path, wait: float) -> list[str]:
    return [
        sys.executable,
        str(HOST),
        "--token-file",
        str(tmp_path / "token.json"),
        "--transcript",
        str(tmp_path / "transcript.jsonl"),
        "--last-message",
        str(tmp_path / "last.md"),
        "--cwd",
        str(tmp_path),
        "--model",
        "fixture-model",
        "--refresh-wait-seconds",
        str(wait),
        "--codex-binary",
        str(fake),
    ]


def test_t_auth_3_host_re_reads_access_token_and_writes_transcript(tmp_path: Path) -> None:
    fake = tmp_path / "fake-codex"
    marker = tmp_path / "refresh-requested"
    fake.write_text(FAKE.replace("__MARKER_PATH__", repr(str(marker))))
    fake.chmod(0o755)
    _token(tmp_path / "token.json", "access-old")
    process = subprocess.Popen(_command(tmp_path, fake, 3), stdin=subprocess.PIPE, text=True)
    assert process.stdin is not None
    process.stdin.write("identity and pointer")
    process.stdin.close()
    wait_until(
        marker.exists,
        timeout=5,
        describe="fake codex to request an access token refresh",
    )
    _token(tmp_path / "token.json", "access-new")
    assert process.wait(timeout=5) == 0
    lines = [json.loads(line) for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert [line["method"] for line in lines] == ["item/completed", "turn/completed"]
    assert (tmp_path / "last.md").read_text() == "finished"


def test_codex_host_bounded_refresh_wait_is_auth_failure(tmp_path: Path) -> None:
    fake = tmp_path / "fake-codex"
    fake.write_text(FAKE.replace("__MARKER_PATH__", repr(str(tmp_path / "refresh-requested"))))
    fake.chmod(0o755)
    _token(tmp_path / "token.json", "access-old")
    result = subprocess.run(
        _command(tmp_path, fake, 0.1),
        input="prompt",
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert "session has expired" in result.stderr
    assert "turn.failed" in (tmp_path / "transcript.jsonl").read_text()
