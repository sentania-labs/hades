#!/usr/bin/python3
"""Run one Codex app-server turn using a file-delivered access token.

This program deliberately has no way to accept a token in argv or the environment.
The supervisor owns the login and atomically replaces the mounted JSON document.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO

AUTH_FAILURE = "Your ChatGPT session has expired: access token refresh was not propagated"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--last-message", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort")
    parser.add_argument("--refresh-wait-seconds", type=float, default=90.0)
    parser.add_argument("--codex-binary", default="/usr/local/bin/codex")
    return parser.parse_args()


def _token(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("token file is not an object")
    if set(document) != {"access_token", "account_id", "expires_at"}:
        raise ValueError("token file has an unexpected shape")
    if not all(isinstance(document[key], str) and document[key] for key in document):
        raise ValueError("token file fields must be non-empty strings")
    return document


class Host:
    def __init__(self, args: argparse.Namespace, prompt: str) -> None:
        self.args = args
        self.prompt = prompt
        self.token_path = Path(args.token_file)
        self.current = _token(self.token_path)
        self.next_id = 1
        self.pending: dict[int, str] = {}
        self.thread_id = ""
        self.last_message = ""
        self.failed = False
        self.process = subprocess.Popen(
            [
                args.codex_binary,
                "app-server",
                "--disable",
                "plugins",
                "-c",
                "check_for_update_on_startup=false",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.input: TextIO = self.process.stdout
        self.output: TextIO = self.process.stdin
        self.transcript = Path(args.transcript).open("w", encoding="utf-8")  # noqa: SIM115

    def send(self, method: str, params: dict[str, Any]) -> None:
        request_id = self.next_id
        self.next_id += 1
        self.pending[request_id] = method
        self._write({"id": request_id, "method": method, "params": params})

    def _write(self, document: dict[str, Any]) -> None:
        self.output.write(json.dumps(document, separators=(",", ":")) + "\n")
        self.output.flush()

    def start(self) -> None:
        self.send(
            "initialize",
            {
                "clientInfo": {"name": "crucible-codex-host", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )

    def response(self, document: dict[str, Any]) -> None:
        request_id = document.get("id")
        if not isinstance(request_id, int):
            return
        method = self.pending.pop(request_id, "")
        if "error" in document:
            self.failed = True
            return
        result = document.get("result") or {}
        if method == "initialize":
            self._write({"method": "initialized", "params": {}})
            self.send(
                "account/login/start",
                {
                    "type": "chatgptAuthTokens",
                    "accessToken": self.current["access_token"],
                    "chatgptAccountId": self.current["account_id"],
                },
            )
        elif method == "account/login/start":
            params: dict[str, Any] = {
                "cwd": self.args.cwd,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "model": self.args.model,
            }
            if self.args.effort:
                params["config"] = {"model_reasoning_effort": self.args.effort}
            self.send("thread/start", params)
        elif method == "thread/start":
            thread = result.get("thread") if isinstance(result, dict) else None
            candidate = thread.get("id") if isinstance(thread, dict) else result.get("threadId")
            if not isinstance(candidate, str) or not candidate:
                self.failed = True
                return
            self.thread_id = candidate
            self.send(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": self.prompt}],
                },
            )

    def refresh(self, document: dict[str, Any]) -> None:
        rejected = self.current["access_token"]
        deadline = time.monotonic() + max(self.args.refresh_wait_seconds, 0)
        while True:
            candidate = _token(self.token_path)
            if candidate["access_token"] != rejected:
                self.current = candidate
                self._write(
                    {
                        "id": document["id"],
                        "result": {
                            "accessToken": candidate["access_token"],
                            "chatgptAccountId": candidate["account_id"],
                        },
                    }
                )
                return
            if time.monotonic() >= deadline:
                self.transcript.write(
                    json.dumps({"type": "turn.failed", "error": {"message": AUTH_FAILURE}})
                    + "\n"
                )
                self.transcript.flush()
                print(AUTH_FAILURE, file=sys.stderr)
                self.failed = True
                self._write(
                    {"id": document["id"], "error": {"code": -32001, "message": AUTH_FAILURE}}
                )
                return
            time.sleep(0.25)

    def notification(self, document: dict[str, Any]) -> bool:
        stored = dict(document)
        method = document.get("method")
        if isinstance(method, str):
            stored.setdefault("type", method.replace("/", "."))
        self.transcript.write(json.dumps(stored, separators=(",", ":")) + "\n")
        self.transcript.flush()
        params = document.get("params") or {}
        if method in ("item/completed", "item/updated") and isinstance(params, dict):
            item = params.get("item") or {}
            if isinstance(item, dict) and item.get("type") in ("agentMessage", "assistant_message"):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    self.last_message = text
        if method in ("turn/completed", "turn/failed"):
            if method == "turn/failed":
                self.failed = True
            return True
        return False

    def run(self) -> int:
        self.start()
        done = False
        try:
            for line in self.input:
                try:
                    document = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(document, dict):
                    continue
                if (
                    document.get("method") == "account/chatgptAuthTokens/refresh"
                    and "id" in document
                ):
                    self.refresh(document)
                elif "method" in document and "id" not in document:
                    done = self.notification(document)
                elif "id" in document:
                    self.response(document)
                if done or self.failed:
                    break
        finally:
            Path(self.args.last_message).write_text(self.last_message, encoding="utf-8")
            self.transcript.close()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        return 1 if self.failed or not done else 0


def main() -> int:
    args = _arguments()
    prompt = sys.stdin.read()
    try:
        return Host(args, prompt).run()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"Not logged in: Codex credential host failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
