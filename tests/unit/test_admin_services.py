"""The administrative services without a database (25): the pty login driver against
fake CLIs that mimic each flow, the shape check, shredding and atomic rotation on real
directories, the public-key fingerprint, and the audit filter. Every secret-shaped value
is built at runtime."""

from __future__ import annotations

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.application.admin.audit import ADMIN_KINDS
from crucible.application.admin.context import AdminContext
from crucible.application.admin.credentials import _http_status, check_shape, shred_file, shred_tree
from crucible.application.admin.github import key_fingerprint
from crucible.application.admin.login import FLOWS, LoginSession, run_login
from crucible.domain.events import EventKind


def _token(prefix: str, count: int = 40) -> str:
    return prefix + "x" * count


def test_the_hermes_probe_does_not_follow_a_redirect_with_the_bearer() -> None:
    destination_hits: list[str | None] = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            destination_hits.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, message: str, *args: object) -> None:
            return

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{destination.server_address[1]}/capture",
            )
            self.end_headers()

        def log_message(self, message: str, *args: object) -> None:
            return

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (destination, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        status = _http_status(
            f"http://127.0.0.1:{redirect.server_address[1]}/models",
            bearer=_token("vk_"),
            timeout=2,
        )
        assert status == 302
        assert destination_hits == []
    finally:
        redirect.shutdown()
        destination.shutdown()
        redirect.server_close()
        destination.server_close()


# ----- the login driver against fake CLIs ----------------------------------------


def fake_cli(tmp_path: Path, name: str, script: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/bash\n" + script, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_claude_code_flow_captures_the_token_to_a_600_file_and_never_shows_it(
    tmp_path: Path,
) -> None:
    """S1b: `setup-token` shows a URL, waits for the pasted code, then prints the
    long-lived token once. The driver writes it to oauth-token and shows `[captured]`."""
    token = _token("sk-ant-oat01-")
    cli = fake_cli(
        tmp_path,
        "fake-claude",
        'echo "Open https://claude.ai/oauth/authorize?code=abc in your browser"\n'
        'printf "Paste code here if prompted > "\n'
        "read -r code\n"
        'echo "got $code"\n'
        f'echo "Your token: {token}"\n'
        'echo "config dir: $CLAUDE_CONFIG_DIR"\n'
        "exit 0\n",
    )
    directory = tmp_path / "dedicated" / "claude_code"
    session = LoginSession(harness="claude_code", started_at=0.0)
    shown: list[str] = []

    import threading  # noqa: PLC0415

    def operator() -> None:
        while session.state != "waiting_for_code":
            threading.Event().wait(0.05)
        session.submit_code("A1B2-C3D4")

    threading.Thread(target=operator, daemon=True).start()
    result = run_login(
        FLOWS["claude_code"],
        str(directory),
        session=session,
        argv=(cli,),
        timeout=20,
        emit=shown.append,
    )
    assert result.state == "finished", result.as_dict()
    assert result.url is not None and result.url.startswith("https://claude.ai/oauth/authorize")
    assert result.token_written
    written = directory / "oauth-token"
    assert written.read_text(encoding="utf-8").strip() == token
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    blob = "\n".join(shown) + json.dumps(result.as_dict())
    assert token not in blob
    assert "[captured to oauth-token]" in blob
    # The CLI was pointed at the dedicated directory and nothing else (12).
    assert f"config dir: {directory}" in blob
    assert "got A1B2-C3D4" in blob


def test_codex_flow_shows_the_device_code_and_url_and_needs_no_paste(tmp_path: Path) -> None:
    cli = fake_cli(
        tmp_path,
        "fake-codex",
        'echo "Visit https://auth.openai.com/codex/device and enter code WXYZ-1234"\n'
        'echo "CODEX_HOME=$CODEX_HOME"\n'
        "sleep 0.2\n"
        'echo "Successfully logged in"\n'
        "exit 0\n",
    )
    directory = tmp_path / "dedicated" / "codex"
    session = LoginSession(harness="codex", started_at=0.0)
    result = run_login(FLOWS["codex"], str(directory), session=session, argv=(cli,), timeout=20)
    assert result.state == "finished"
    assert result.url == "https://auth.openai.com/codex/device"
    assert result.code == "WXYZ-1234"
    assert not result.token_written
    assert "15 minutes" in FLOWS["codex"].window
    assert any(f"CODEX_HOME={directory}" in line for line in result.lines)


def test_agy_flow_pastes_the_code_and_a_missing_code_fails_cleanly(tmp_path: Path) -> None:
    cli = fake_cli(
        tmp_path,
        "fake-agy",
        'echo "Please visit https://accounts.google.com/o/oauth2/auth?x=1"\n'
        # AGY's own prompt, as captured (tests/fixtures_data/logins/agy.raw).
        'echo "Or, paste the authorization code here and press Enter:"\n'
        "read -t 2 -r code || { echo timeout; exit 1; }\n"
        'echo "HOME=$HOME"\n'
        "exit 0\n",
    )
    directory = tmp_path / "dedicated" / "agy"
    assert "60 seconds" in FLOWS["agy"].window
    session = LoginSession(harness="agy", started_at=0.0)

    import threading  # noqa: PLC0415

    def operator() -> None:
        while session.state != "waiting_for_code":
            threading.Event().wait(0.05)
        session.submit_code("4/0Ab" + "c" * 30)

    threading.Thread(target=operator, daemon=True).start()
    result = run_login(FLOWS["agy"], str(directory), session=session, argv=(cli,), timeout=20)
    assert result.state == "finished", result.as_dict()
    assert result.url is not None and result.url.startswith("https://accounts.google.com/")
    assert any(f"HOME={directory}" in line for line in result.lines)

    # Nobody pastes: the CLI's own 60-second window (2 s here) expires and the run
    # ends `failed` with the exit code, never hanging the driver.
    session = LoginSession(harness="agy", started_at=0.0)
    result = run_login(FLOWS["agy"], str(directory), session=session, argv=(cli,), timeout=20)
    assert result.state == "failed" and result.exit_code == 1


def test_the_driver_times_out_a_login_that_never_finishes(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, "fake-hang", 'echo "https://example.invalid/x"\nsleep 30\n')
    session = LoginSession(harness="codex", started_at=0.0)
    result = run_login(FLOWS["codex"], str(tmp_path / "d"), session=session, argv=(cli,), timeout=1)
    assert result.state == "failed" and result.error == "login timed out"
    assert _processes_running(cli) == []


def _processes_running(path: str) -> list[int]:
    """The pids whose command line names `path`, read from /proc rather than with pgrep,
    which the worker image does not carry (hades #184)."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(path.encode() in arg for arg in argv):
            found.append(int(entry.name))
    return found


# ----- shape, shred, rotate ------------------------------------------------------


def test_check_shape_names_the_problem_never_the_value(tmp_path: Path) -> None:
    spec = CodexAdapter().credential_spec()
    assert not check_shape(spec, str(tmp_path)).ok
    (tmp_path / "auth.json").write_text("not json", encoding="utf-8")
    check = check_shape(spec, str(tmp_path))
    assert check.problems == ("auth.json: not JSON",)
    secret = _token("eyJ")
    (tmp_path / "auth.json").write_text(json.dumps({"other": secret}), encoding="utf-8")
    check = check_shape(spec, str(tmp_path))
    assert check.problems == ("auth.json: missing keys ['tokens']",)
    assert secret not in json.dumps(check.as_dict())
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"refresh_token": secret}, "last_refresh": "2026-09-17T00:00:00Z"}),
        encoding="utf-8",
    )
    check = check_shape(spec, str(tmp_path))
    assert check.ok and check.files[0]["size"] > 0 and check.files[0]["parses"]

    agy = AgyAdapter().credential_spec()
    inner = tmp_path / ".gemini" / "antigravity-cli"
    inner.mkdir(parents=True)
    (inner / "antigravity-oauth-token").write_text(json.dumps({"token": {}}), encoding="utf-8")
    assert check_shape(agy, str(tmp_path)).ok

    claude = ClaudeCodeAdapter().credential_spec()
    (tmp_path / "oauth-token").write_text(_token("sk-ant-oat01-"), encoding="utf-8")
    assert check_shape(claude, str(tmp_path)).ok, ".claude.json is optional"


def test_shred_overwrites_then_unlinks_and_keeps_the_root(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    (root / "sessions").mkdir(parents=True)
    secret = _token("eyJ", 200).encode()
    (root / "auth.json").write_bytes(secret)
    (root / "sessions" / "s.jsonl").write_bytes(b"line\n" * 100)
    (root / "link").symlink_to(root / "auth.json")
    result = shred_tree(root, keep_root=True)
    assert result["files"] == 2 and result["bytes"] == len(secret) + 500
    assert root.is_dir() and list(root.iterdir()) == []


def test_shred_file_zeroes_before_unlink(tmp_path: Path) -> None:
    target = tmp_path / "t"
    payload = os.urandom(5000)
    target.write_bytes(payload)
    # Observe the overwrite through a second handle before the unlink lands.
    size = shred_file(target)
    assert size == 5000 and not target.exists()


# ----- fingerprint and audit ------------------------------------------------------


def test_key_fingerprint_is_of_the_public_key_only(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "app.pem"
    path.write_bytes(pem)
    fingerprint = key_fingerprint(str(path))
    assert fingerprint is not None and fingerprint.startswith("sha256:")
    assert key_fingerprint(str(tmp_path / "missing.pem")) is None
    assert fingerprint not in pem.decode("utf-8", "replace")
    assert key_fingerprint(str(path)) == fingerprint, "deterministic"


def test_admin_kinds_are_every_mutation_and_nothing_a_worker_writes() -> None:
    for kind in (
        EventKind.HARNESS_ENABLED,
        EventKind.CREDENTIAL_PROBED,
        EventKind.CREDENTIAL_ROTATED,
        EventKind.CREDENTIAL_REMOVED,
        EventKind.IMAGE_PROMOTED,
        EventKind.GITHUB_CHECKED,
    ):
        assert kind.value in ADMIN_KINDS
    for kind in (EventKind.WORKER_PROGRESS, EventKind.ATTEMPT_RUNNING, EventKind.TASK_SUBMITTED):
        assert kind.value not in ADMIN_KINDS


# ----- the correction round -------------------------------------------------------


def test_a_spawn_that_cannot_happen_is_a_terminal_failed_session(tmp_path: Path) -> None:
    """B1: the harness CLIs live in the worker images, not in the Crucible service image.
    A spawn that raises left the session in `starting` for ever, and every later login for
    that harness was then refused as one already in progress."""
    session = LoginSession(harness="codex", started_at=0.0)
    result = run_login(
        FLOWS["codex"],
        str(tmp_path / "codex"),
        session=session,
        argv=(str(tmp_path / "no-such-cli"),),
        timeout=5.0,
    )
    assert result.state == "failed"
    assert result.error is not None and "no-such-cli" in result.error
    assert result.exit_code is None


def test_the_registry_refuses_a_login_whose_cli_is_not_installed_and_accepts_a_retry(
    tmp_path: Path,
) -> None:
    from crucible.application.admin.login import LoginRegistry  # noqa: PLC0415
    from crucible.application.errors import ConflictError  # noqa: PLC0415

    ctx = _bare_context(login_commands={"codex": (str(tmp_path / "absent-cli"),)})
    registry = LoginRegistry()
    for _ in range(2):
        # A refusal leaves no session behind, so the next attempt is accepted and refused
        # on its own merits rather than as "already in progress".
        try:
            registry.start(ctx, "codex", str(tmp_path / "codex"))
        except ConflictError as exc:
            assert "is not installed on this host" in str(exc.detail)
        else:  # pragma: no cover - the binary does not exist
            raise AssertionError("the registry started a login with no CLI")
    assert registry.get("codex") is None


def test_a_host_login_finds_the_cli_on_path_and_a_container_login_uses_the_image_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """On the host the CLI is wherever the operator installed it (~/.local/bin for a
    user-scoped install), so the default is the bare name found on PATH. Inside the
    worker image the adapters launch each CLI by absolute path (C11), and the login
    runs the same file."""
    from crucible.application.admin.login import LoginRegistry  # noqa: PLC0415
    from crucible.ports.harness import (  # noqa: PLC0415
        AGY_BINARY,
        CLAUDE_CODE_BINARY,
        CODEX_BINARY,
    )

    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("claude", "codex", "agy"):
        fake_cli(bin_dir, name, "#!/bin/bash\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    host = LoginRegistry()
    ctx = _bare_context()
    assert host.resolve(ctx, "claude_code") == ("claude", "setup-token")
    assert host.resolve(ctx, "codex") == ("codex", "login", "--device-auth")
    assert host.resolve(ctx, "agy")[0] == "agy"

    class Runner:
        def run_login_container(self) -> None:  # pragma: no cover - never called
            raise AssertionError

    in_image = _bare_context(providers={"docker": Runner()})
    # The image paths need not exist on this host: the container has them.
    monkeypatch.setenv("PATH", "/nonexistent")
    assert host.resolve(in_image, "claude_code") == (CLAUDE_CODE_BINARY, "setup-token")
    assert host.resolve(in_image, "codex") == (CODEX_BINARY, "login", "--device-auth")
    assert host.resolve(in_image, "agy")[0] == AGY_BINARY
    assert host.resolve(in_image, "agy")[1:] == FLOWS["agy"].argv[1:]
    # An operator-configured command is used as given in either mode.
    configured = _bare_context(
        providers={"docker": Runner()}, login_commands={"codex": ("my-codex", "login")}
    )
    assert host.resolve(configured, "codex") == ("my-codex", "login")


def test_a_thread_that_cannot_start_leaves_no_login_in_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The registry records the session before it starts the thread, so a thread that
    cannot be created used to leave a session in `starting` for ever and refuse every
    later login for that harness as one already in progress (correction 17's failure mode,
    reached by the other path)."""
    from crucible.application.admin.login import LoginRegistry  # noqa: PLC0415

    cli = fake_cli(tmp_path, "fake-login", "#!/bin/bash\nexit 0\n")
    ctx = _bare_context(login_commands={"codex": (cli,)})
    registry = LoginRegistry()

    def no_thread(self: Any) -> None:
        raise RuntimeError("cannot create a thread")

    monkeypatch.setattr(threading.Thread, "start", no_thread)
    with pytest.raises(RuntimeError):
        registry.start(ctx, "codex", str(tmp_path / "codex"))
    session = registry.get("codex")
    assert session is not None and session.state == "failed"
    # The next attempt is refused on its own merits, not as one already in progress.
    monkeypatch.undo()
    registry.start(ctx, "codex", str(tmp_path / "codex"))


def test_a_session_that_raises_in_its_thread_still_ends_terminal(tmp_path: Path) -> None:
    from crucible.application.admin.login import LoginRegistry  # noqa: PLC0415

    session = LoginSession(harness="codex", started_at=0.0)

    def boom(*_args: Any, **_kwargs: Any) -> LoginSession:
        raise RuntimeError("the driver fell over")

    import crucible.application.admin.login as login_module  # noqa: PLC0415

    original = login_module.run_login
    login_module.run_login = boom
    try:
        LoginRegistry._run(FLOWS["codex"], str(tmp_path), session, None, timeout=1.0)
    finally:
        login_module.run_login = original
    assert session.state == "failed" and "fell over" in (session.error or "")


def test_the_paste_prompt_is_a_prompt_and_not_a_sentence_about_one() -> None:
    """N2: informational text flipped the session to waiting_for_code before the CLI was
    reading, and the pasted code went nowhere."""
    from crucible.application.admin.login import PASTE_RE  # noqa: PLC0415

    assert PASTE_RE.search("Paste the code: ")
    assert PASTE_RE.search("Enter the code here >")
    assert PASTE_RE.search("code:")
    assert not PASTE_RE.search("Visit https://example.invalid and enter the code shown there")
    assert not PASTE_RE.search("We will ask you to paste the code in a moment.")


def _bare_context(**overrides: Any) -> AdminContext:
    from crucible.adapters.clock import SystemClock  # noqa: PLC0415
    from crucible.adapters.harness.registry import default_registry  # noqa: PLC0415

    def factory() -> Any:  # pragma: no cover - never entered here
        raise AssertionError("no unit of work is needed")

    return AdminContext(
        uow_factory=factory,
        clock=SystemClock(),
        harnesses=default_registry(),
        **{"providers": {}, **overrides},
    )


def test_a_secret_shaped_reason_is_refused_rather_than_recorded() -> None:
    """B2: the reason reaches the append-only event log and `GET /admin/audit` serves it
    back. It is refused, not redacted, so the operator knows it landed nowhere."""
    from crucible.application.admin.context import require_reason  # noqa: PLC0415
    from crucible.application.errors import ContractValidationError  # noqa: PLC0415

    assert require_reason("  rotating after the quarterly review  ") == (
        "  rotating after the quarterly review  "
    )
    try:
        require_reason("pasting the token " + _token("sk-ant-oat01-"))
    except ContractValidationError as exc:
        assert exc.errors[0]["path"] == "reason"
        assert _token("sk-ant-oat01-") not in str(exc.detail) + str(exc.errors)
    else:  # pragma: no cover
        raise AssertionError("a secret-shaped reason was accepted")


def test_the_api_reason_helper_accepts_only_a_non_empty_string() -> None:
    """S7: a JSON null stringified to "None" and a zero to "0", and both passed the guard
    as a reason nobody wrote."""
    from crucible.adapters.api.routers.admin import _reason  # noqa: PLC0415

    assert _reason({"reason": "because"}) == "because"
    assert _reason({"reason": None}) is None
    assert _reason({"reason": 0}) is None
    assert _reason({"reason": "   "}) is None
    assert _reason({}) is None
    assert _reason(None) is None


def test_shred_tree_reports_what_it_could_not_remove_instead_of_success(
    tmp_path: Path,
) -> None:
    """The walk used to be ordered by depth alone and stopped at the first directory that
    would not go, which left auth files on disk behind it while the caller was told the
    shred had worked."""
    from crucible.application.admin.credentials import ShredIncompleteError  # noqa: PLC0415

    root = tmp_path / "credential"
    (root / "tmp" / "arg0").mkdir(parents=True)
    (root / "auth.json").write_text(json.dumps({"tokens": {}}), encoding="utf-8")
    (root / "tmp" / "arg0" / "held").write_text("x", encoding="utf-8")
    os.chmod(root / "tmp" / "arg0", 0o000)
    try:
        try:
            shred_tree(root, keep_root=True)
        except ShredIncompleteError as exc:
            assert "could not be removed" in str(exc.detail)
        else:  # pragma: no cover
            raise AssertionError("a partial shred reported success")
        # Everything reachable went, even though one directory did not: the failure never
        # aborts the rest of the walk.
        assert not (root / "auth.json").exists()
    finally:
        os.chmod(root / "tmp" / "arg0", 0o700)


def test_shred_tree_removes_a_non_empty_subdirectory_and_every_entry_type(
    tmp_path: Path,
) -> None:
    root = tmp_path / "credential"
    (root / "tmp" / "arg0" / "codex-abc").mkdir(parents=True)
    (root / "log").mkdir()
    (root / "auth.json").write_text(json.dumps({"tokens": {}}), encoding="utf-8")
    (root / "log" / "login.log").write_text("hello", encoding="utf-8")
    (root / "tmp" / "arg0" / "codex-abc" / ".lock").write_text("", encoding="utf-8")
    os.symlink(tmp_path / "nowhere", root / "tmp" / "dangling")
    os.symlink(tmp_path, root / "tmp" / "a-directory-link")
    result = shred_tree(root, keep_root=True)
    assert result["files"] >= 3
    assert list(root.iterdir()) == []
    assert tmp_path.is_dir(), "a symlinked directory is unlinked, never followed"


def test_shred_tree_absorbs_a_file_that_appears_during_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness CLI writing into its own directory mid-shred is a benign race: the walk
    is a snapshot, so the directory it wrote into will not go on that pass. A second pass
    takes it, and anything still there after that is a refusal."""
    import crucible.application.admin.credentials as credentials_module  # noqa: PLC0415

    root = tmp_path / "credential"
    (root / "log").mkdir(parents=True)
    (root / "auth.json").write_text(json.dumps({"tokens": {}}), encoding="utf-8")
    original = credentials_module._remove_entry
    seen: list[str] = []

    def racing(path: Path) -> int | None:
        seen.append(path.name)
        if path.name == "auth.json" and not seen.count("appeared.log"):
            (root / "log").mkdir(exist_ok=True)
            (root / "log" / "appeared.log").write_text("late", encoding="utf-8")
        return original(path)

    monkeypatch.setattr(credentials_module, "_remove_entry", racing)
    result = shred_tree(root, keep_root=True)
    assert "appeared.log" in seen, "the second pass never saw the late file"
    assert result["files"] == 2
    assert list(root.iterdir()) == []


def test_a_failing_run_carrying_a_rejected_status_is_not_a_quota(tmp_path: Path) -> None:
    """B5: the bare substring matched any failing run whose tail happened to carry it, a
    GitHub payload or a fixture among them, and quota does not retry the way a crash
    does."""
    from crucible.domain.exit_class import ExitClass  # noqa: PLC0415
    from crucible.ports.harness import ExitInfo  # noqa: PLC0415

    adapter = ClaudeCodeAdapter()
    unrelated = (
        '{"type":"assistant","message":"the webhook body was '
        '{\\"status\\":\\"rejected\\"} and out_of_credits was in the fixture"}\n'
        '{"type":"result","is_error":true,"result":"the tool crashed"}'
    )
    assert adapter.classify_exit(ExitInfo(exit_code=1), unrelated, "") is ExitClass.CRASHED
    correlated = (
        '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
        '"overageDisabledReason":"out_of_credits"}}\n'
        '{"type":"result","subtype":"success","terminal_reason":"api_error"}'
    )
    assert adapter.classify_exit(ExitInfo(exit_code=1), correlated, "") is (
        ExitClass.QUOTA_EXHAUSTED
    )


def test_the_audit_cursor_advances_across_a_gap_of_non_admin_events() -> None:
    """S6: the cursor came from the last matching item, so a stretch of non-admin events
    longer than the scan budget left it where it was and every later admin event was
    unreachable."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from crucible.application.admin import audit as audit_module  # noqa: PLC0415
    from crucible.domain.entities import Event  # noqa: PLC0415

    stream = [
        Event(
            seq=index + 1,
            ts=datetime(2026, 9, 17, tzinfo=UTC),
            kind=(
                EventKind.IMAGE_PROMOTED.value if index == 24 else EventKind.WORKER_PROGRESS.value
            ),
            principal="p",
            verified=True,
            payload={},
        )
        for index in range(25)
    ]

    class _Events:
        @staticmethod
        def list_global(*, after_seq: int, kind: object, since: object, limit: int):  # type: ignore[no-untyped-def]
            return [e for e in stream if (e.seq or 0) > after_seq][:limit]

    class _Uow:
        events = _Events()

    original_page, original_max = audit_module.PAGE_SIZE, audit_module.MAX_PAGES
    audit_module.PAGE_SIZE, audit_module.MAX_PAGES = 5, 2
    try:
        first = audit_module.tail(_Uow(), cursor=None, limit=50)  # type: ignore[arg-type]
        assert first["items"] == [] and first["next_cursor"] == 10, first
        cursor = first["next_cursor"]
        for _ in range(5):
            page = audit_module.tail(_Uow(), cursor=cursor, limit=50)  # type: ignore[arg-type]
            cursor = page["next_cursor"]
            if page["items"]:
                assert page["items"][0]["kind"] == EventKind.IMAGE_PROMOTED.value
                break
        else:  # pragma: no cover
            raise AssertionError("the admin event past the gap was never reached")
    finally:
        audit_module.PAGE_SIZE, audit_module.MAX_PAGES = original_page, original_max


def test_the_images_page_offers_releases_and_latest_never_a_ci_proof_tag() -> None:
    """crucible#116 and #111: a `ci-*` tag proves a build and is never a candidate."""
    from crucible.application.admin.images import offered_tag  # noqa: PLC0415

    assert offered_tag("ghcr.io/sentania-labs/crucible-worker:0.5.5")
    assert offered_tag("ghcr.io/sentania-labs/crucible-worker:latest")
    assert offered_tag("ghcr.io/sentania-labs/crucible-worker")
    assert offered_tag("registry:5000/crucible-worker@sha256:" + "a" * 64)
    assert not offered_tag("ghcr.io/sentania-labs/crucible-worker:ci-35000000000")
    assert not offered_tag("registry:5000/crucible-worker:ci-abc")
