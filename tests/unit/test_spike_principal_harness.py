"""The principal harness spike script (hades #208, work item 1) parses its arguments.

The experiments themselves need the worker image's CLIs and a mounted credential; this
tests cover argument parsing and the credential, kill-trigger and deadline regressions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "spikes" / "principal_harness.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("principal_harness", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The dataclass decorator looks its module up in sys.modules.
    sys.modules["principal_harness"] = module
    spec.loader.exec_module(module)
    return module


def test_defaults_point_at_the_worker_mounts() -> None:
    module = _load()
    args = module.build_parser().parse_args(["claude-resume"])
    assert args.experiment == "claude-resume"
    assert args.model == module.DEFAULT_MODEL
    assert args.token_file == Path("/home/worker/.claude/oauth-token")
    assert args.codex_home == Path("/home/worker/.codex")
    assert args.out == Path("/tmp/principal-harness-spike")


def test_every_experiment_is_a_choice_and_overrides_parse(tmp_path: Path) -> None:
    module = _load()
    parser = module.build_parser()
    for name in module.EXPERIMENTS:
        assert parser.parse_args([name]).experiment == name
    args = parser.parse_args(
        [
            "--out",
            str(tmp_path),
            "--model",
            "claude-opus-5-5",
            "--codex-model",
            "gpt-5-codex",
            "--token-file",
            str(tmp_path / "token"),
            "--codex-home",
            str(tmp_path / "codex"),
            "all",
        ]
    )
    assert args.out == tmp_path
    assert args.model == "claude-opus-5-5"
    assert args.codex_model == "gpt-5-codex"
    assert args.token_file == tmp_path / "token"
    assert args.codex_home == tmp_path / "codex"
    assert args.experiment == "all"


def test_unknown_experiment_is_refused() -> None:
    module = _load()
    with pytest.raises(SystemExit):
        module.build_parser().parse_args(["claude-interactive"])


def test_token_env_is_read_from_the_file_only(tmp_path: Path) -> None:
    module = _load()
    token = tmp_path / "oauth-token"
    token.write_text("sk-ant-oat01-example\n", encoding="utf-8")
    env = module.claude_env(tmp_path / "config", token)
    assert env[module.TOKEN_ENV] == "sk-ant-oat01-example"
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")
    assert "CLAUDECODE" not in env
    assert module.TOKEN_ENV not in module.claude_env(tmp_path, tmp_path / "missing")


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("operation", ["call", "drain"])
def test_rpc_silent_peer_deadline_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: bool, operation: str
) -> None:
    module = _load()
    peer = tmp_path / "silent-peer"
    peer.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "sys.stderr.write('x' * 100000)\n"
        "sys.stderr.flush()\n"
        + ("sys.stdout.write('{'); sys.stdout.flush()\n" if partial else "")
        + "time.sleep(60)\n"
    )
    peer.chmod(0o700)
    monkeypatch.setattr(module, "CODEX", str(peer))
    with module.AppServer(tmp_path, tmp_path, tmp_path / "rpc.jsonl") as server:
        start = time.monotonic()
        if operation == "call":
            assert server.call("initialize", {}, timeout=0.1)["timeout"]
        else:
            assert server.drain(("turn/completed",), timeout=0.1)["timed_out"]
        assert time.monotonic() - start < 2
    assert server.process.poll() is not None
    assert not server.poller._thread.is_alive()


@pytest.mark.parametrize("filename", ["access-token.json", "auth.json"])
def test_codex_probe_keeps_mounted_secrets_out_of_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    module = _load()
    mount = tmp_path / "mount"
    mount.mkdir()
    token = {"access_token": "fixture-access-secret", "account_id": "fixture-account"}
    document = (
        {**token, "expires_at": "2099-01-01T00:00:00Z"}
        if filename == "access-token.json"
        else {"tokens": {**token, "refresh_token": "fixture-refresh-secret"}}
    )
    (mount / filename).write_text(json.dumps(document))
    (mount / "config.toml").write_text("# fixture-config-must-not-copy")
    peer = tmp_path / "rpc-peer"
    peer.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if 'id' not in request: continue\n"
        "    result = {}\n"
        "    if request['method'] == 'account/login/start':\n"
        "        assert request['params']['type'] == 'chatgptAuthTokens'\n"
        "        assert request['params']['accessToken'] == 'fixture-access-secret'\n"
        "        result = request['params']  # deliberately echo secrets back\n"
        "    print(json.dumps({'id': request['id'], 'result': result}), flush=True)\n"
    )
    peer.chmod(0o700)
    monkeypatch.setattr(module, "CODEX", str(peer))
    out = tmp_path / "out"
    args = module.build_parser().parse_args(
        ["--out", str(out), "--codex-home", str(mount), "codex-probe"]
    )
    report = module.codex_probe(args)
    assert [entry["status"] for entry in report["external_auth"]] == ["accepted"] * 3
    assert "fixture-access-secret" not in json.dumps(report)
    for path in out.rglob("*"):
        if path.is_file():
            assert path.name not in ("auth.json", "access-token.json", "config.toml")
            assert b"fixture-" not in path.read_bytes()
    assert json.loads((mount / filename).read_text()) == document


@pytest.mark.parametrize("completed", [False, True])
def test_claude_kill_waits_for_text_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed: bool
) -> None:
    module = _load()
    delta = {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "One"}},
    }
    hosts = []

    class Host:
        def __init__(self, argv: list[str], *_: object) -> None:
            assert "--include-partial-messages" in argv
            self.started = 0
            self.poller = SimpleNamespace(sample=lambda: 0, peak_kb=0)
            self.process = SimpleNamespace(returncode=None)
            self.killed = False
            hosts.append(self)

        def send(self, _: str) -> None:
            pass

        def wait_for(self, kind: str, **_: object) -> tuple[list[dict[str, object]], float]:
            return [{"type": kind}], 0

        def events(self, **_: object) -> object:
            yield {"type": "stream_event", "event": {"type": "message_start"}}
            yield {"type": "assistant"} if completed else delta
            pytest.fail("read past the first text delta before killing")

        def kill(self) -> None:
            self.killed = True
            self.process.returncode = -9

    monkeypatch.setattr(module, "StreamJsonProcess", Host)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    measured = module.Measured([], 0, 0, 0, 0, "")
    monkeypatch.setattr(module, "run_measured", lambda *a, **kw: (measured, []))
    args = module.build_parser().parse_args(["--out", str(tmp_path), "claude-long-lived"])
    report = module.claude_long_lived(args)
    assert hosts[0].killed
    assert report["killed_mid_turn"]["trigger"] == (
        "no_delta_observed" if completed else "text_delta"
    )
    assert report["killed_mid_turn"]["delta"] == (
        None if completed else {"type": "text_delta", "text": "One"}
    )
    assert report["killed_mid_turn"]["complete_assistant_seen"] == completed
    assert not report["killed_mid_turn"]["result_seen"]
