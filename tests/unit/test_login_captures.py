"""hades #173: each harness's real login output, captured from its CLI under a pty, is
what the login path is tested against (tests/login_captures.py says how the captures
were taken). Both paths are fed the same bytes: the Docker and local logins read the
CLI's raw terminal output straight into `_consume`, and the Kubernetes login runs the
driver script, whose rendered log lines `_consume` then reads."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from crucible.adapters.execution.kubernetes import _LOGIN_CODE_SCRIPT, _LOGIN_DRIVER
from crucible.application.admin.login import (
    ENTER_PAUSE_SECONDS,
    FLOWS,
    QUIET_PROMPT_SECONDS,
    LoginFlow,
    LoginSession,
    _consume,
    normalize_code,
    render_line,
)
from tests.login_captures import capture, replay_script, replay_to_exit
from tests.wait import wait_until

CLAUDE_URL_START = (
    "https://claude.com/cai/oauth/authorize?code=true"
    "&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code"
)
AGY_URL_START = "https://accounts.google.com/o/oauth2/auth?access_type=offline"
CLAUDE_PROMPT = "Paste code here if prompted >"
AGY_PROMPT = "Or, paste the authorization code here and press Enter:"


def feed_raw(name: str, harness: str, tmp_path: Path, chunk: int = 97) -> LoginSession:
    """The capture's bytes, read in odd-sized pieces as a pty hands them over."""
    flow = FLOWS[harness]
    session = LoginSession(harness=harness, started_at=0.0, guidance=flow.guidance)
    session.state = "waiting_for_operator"
    data = capture(name)
    buffer = ""
    for start in range(0, len(data), chunk):
        buffer += data[start : start + chunk].decode("utf-8", "replace")
        buffer = _consume(buffer, flow, None, tmp_path, session, None)
    return session


def assert_rendered(session: LoginSession) -> None:
    for line in session.lines:
        assert "\x1b" not in line and "\r" not in line and "\x07" not in line, line


# ----- the raw bytes, as the Docker and local logins read them ---------------------------


def test_claude_code_capture_shows_the_words_the_url_and_its_prompt(tmp_path: Path) -> None:
    session = feed_raw("claude_code", "claude_code", tmp_path)
    assert_rendered(session)
    shown = [line.strip() for line in session.lines]
    # Ink's words were separated by cursor-column moves, not spaces.
    assert "Welcome to Claude Code v2.1.280" in shown
    assert "Browser didn't open? Use the url below to sign in (c to copy)" in shown
    assert session.url is not None and session.url.startswith(CLAUDE_URL_START)
    # The OSC 8 hyperlink's visible text is the URL, once, on its own line.
    assert session.url in shown
    assert session.prompt == CLAUDE_PROMPT
    assert session.state == "waiting_for_code"
    assert session.code is None


def test_agy_capture_reaches_its_prompt_and_starts_its_60_second_clock(tmp_path: Path) -> None:
    before = time.time()
    session = feed_raw("agy", "agy", tmp_path)
    assert_rendered(session)
    shown = [line.strip() for line in session.lines]
    assert "Authentication required. Please visit the URL to log in:" in shown
    assert "Waiting for authentication (timeout 60s)..." in shown
    assert session.url is not None and session.url.startswith(AGY_URL_START)
    assert session.prompt == AGY_PROMPT
    assert session.state == "waiting_for_code"
    assert session.code_wait_ends_at is not None
    # Shown a few seconds early rather than late: the service sees the prompt after AGY
    # printed it.
    assert before + 54 <= session.code_wait_ends_at <= time.time() + 55
    assert session.as_dict()["code_wait_ends_at"].endswith("+00:00")


def test_agy_running_out_of_time_says_so_in_plain_words(tmp_path: Path) -> None:
    session = feed_raw("agy_timeout", "agy", tmp_path)
    assert session.error == FLOWS["agy"].timed_out_message
    assert "60 seconds" in (session.error or "")
    # AGY has stopped reading: the code box goes, a code is refused, and silence does not
    # bring the box back.
    assert session.state == "waiting_for_operator"
    assert session.accept_code("too-late") is False
    session.notice_waiting(FLOWS["agy"], now=session.last_output_at + 60)
    assert session.state == "waiting_for_operator"


def test_codex_capture_shows_the_url_and_device_code_and_never_asks_for_one(
    tmp_path: Path,
) -> None:
    session = feed_raw("codex", "codex", tmp_path)
    assert_rendered(session)
    assert session.url == "https://auth.openai.com/codex/device"
    assert session.code == "TEST-C0DE9"
    assert session.state == "waiting_for_operator"
    session.last_output_at -= QUIET_PROMPT_SECONDS * 2
    session.notice_waiting(FLOWS["codex"])
    assert session.state == "waiting_for_operator"


def test_render_line_keeps_what_a_terminal_would_show() -> None:
    assert render_line("\x1b[31mWelcome\x1b[9Gto\x1b[12GClaude\r\r") == "Welcome to Claude"
    assert render_line("spinner 1\rspinner 2\rdone\r\r") == "done"
    assert (
        render_line(
            "\x1b]8;id=1;https://x.invalid/a\x07\x1b[37mhttps://x.invalid/a\x1b[39m\x1b]8;;\x07"
        )
        == "https://x.invalid/a"
    )
    assert render_line("\x1b(B\x0f\x1b[2Kplain\x1b7") == "plain"


# ----- the Kubernetes driver, replaying each capture through a real pty ------------------


needs_pty_tools = pytest.mark.skipif(
    shutil.which("script") is None or shutil.which("bash") is None,
    reason="needs bash and util-linux script, which every Debian worker image carries",
)


def run_driver(
    tmp_path: Path,
    harness: str,
    stand_in: str,
    *,
    code: str | None,
    login_dir: str | None = None,
    token_pattern: str | None = None,
) -> tuple[str, LoginSession]:
    """The login Pod's driver with `stand_in` as the CLI, its log read the way the
    service reads it; `code` is pasted through the exec script once the session asks."""
    flow = FLOWS[harness]
    control = tmp_path / "control"
    env = {
        **os.environ,
        "CRUCIBLE_LOGIN_DIR": login_dir or str(tmp_path / "login"),
        "CRUCIBLE_LOGIN_CONTROL": str(control),
        "TERM": "xterm",
    }
    if flow.captures_token:
        env["CRUCIBLE_LOGIN_TOKEN_PATTERN"] = token_pattern or flow.token_pattern
        env["CRUCIBLE_LOGIN_TOKEN_FILE"] = flow.token_file
    process = subprocess.Popen(
        ["bash", "-c", _LOGIN_DRIVER, "crucible-login", "bash", "-c", stand_in],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    output = bytearray()
    lock = threading.Lock()

    def pump() -> None:
        stream = process.stdout
        assert stream is not None
        while chunk := os.read(stream.fileno(), 4096):
            with lock:
                output.extend(chunk)

    threading.Thread(target=pump, daemon=True).start()
    session = LoginSession(harness=harness, started_at=0.0)
    session.state = "waiting_for_operator"
    consumed = 0
    buffer = ""
    try:

        def login_finished() -> bool:
            nonlocal buffer, code, consumed
            with lock:
                text = bytes(output).decode("utf-8", "replace")
            fresh, consumed = text[consumed:], len(text)
            kept = [
                line
                for line in fresh.splitlines(keepends=True)
                if not line.startswith("crucible-login.exit=")
            ]
            buffer = _consume(buffer + "".join(kept), flow, None, tmp_path, session, None)
            if session.state == "waiting_for_code" and code is not None:
                subprocess.run(
                    ["sh", "-c", _LOGIN_CODE_SCRIPT],
                    input=(code + "\n").encode(),
                    env=env,
                    check=True,
                )
                session.state = "waiting_for_operator"
                code = None
            return "crucible-login.exit=" in text

        wait_until(login_finished, timeout=30, describe=f"{harness} login driver to exit")
    finally:
        process.kill()
        process.wait()
    with lock:
        return bytes(output).decode("utf-8", "replace"), session


@needs_pty_tools
def test_the_driver_captures_a_token_drawn_without_a_newline_and_ends_the_login(
    tmp_path: Path,
) -> None:
    token = "faketok-" + "A" * 40
    stand_in = (
        "echo 'Visit https://claude.com/cai/oauth/authorize?code=true'; "
        "printf ' Paste code here if prompted > '; "
        "IFS= read -r code; "
        "printf '\\r\\033[1A\\033[2KYour OAuth token (valid for 1 year):"
        f"\\r\\033[1B\\033[2K{token}"
        "\\r\\033[1B\\033[2KStore this token securely.\\r'; "
        "sleep 60"
    )
    started = time.monotonic()
    text, _session = run_driver(
        tmp_path,
        "claude_code",
        stand_in,
        code="test-code",
        token_pattern=r"(faketok-[A-Za-z0-9]{20,})",
    )
    assert time.monotonic() - started < 20
    token_path = tmp_path / "login" / "oauth-token"
    assert token_path.read_text() == token + "\n"
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert token not in text
    assert "[captured to oauth-token]" in text
    assert "crucible-login.exit=0" in text


@needs_pty_tools
def test_the_driver_brings_claude_codes_url_and_prompt_through_and_enter_submits(
    tmp_path: Path,
) -> None:
    pasted = "the-pasted-code#with-state"
    text, session = run_driver(tmp_path, "claude_code", replay_script("claude_code"), code=pasted)
    assert "crucible-login.exit=0" in text, text
    assert session.url is not None and session.url.startswith(CLAUDE_URL_START), text
    assert "\n" + session.url + "\n" in text
    assert "Welcome to Claude Code v2.1.280" in text
    assert session.prompt == CLAUDE_PROMPT
    # Raw mode ends the input on a carriage return only: every character arrived, and
    # the read ended, which a newline would not have done.
    assert f"stand-in read {len(pasted)} characters" in text
    # The prompt redrawn with the code in it reaches the log masked.
    assert "Paste code here [pasted code]" in text
    # Code is sent first, sleep 1, then only the carriage return:
    assert 'printf "%s" "$c" > "$d/in"' in _LOGIN_CODE_SCRIPT
    assert "sleep 1" in _LOGIN_CODE_SCRIPT
    assert _LOGIN_CODE_SCRIPT.endswith('printf "\\r" > "$d/in"')
    assert 'printf "%s\\r" "$c" > "$d/in"' not in _LOGIN_CODE_SCRIPT
    assert pasted not in text
    assert "\x1b" not in text and "\r" not in text


@needs_pty_tools
@pytest.mark.skipif(os.geteuid() == 0, reason="the Pod's user does not own its home; root does")
def test_the_driver_brings_agys_prompt_through_and_never_shows_a_chmod_failure(
    tmp_path: Path,
) -> None:
    pasted = "4/0AVG7fiQ-not-a-real-code"
    # AGY's login directory is the home the Pod mounts, which its user does not own;
    # "/" stands in for that here.
    text, session = run_driver(tmp_path, "agy", replay_script("agy"), code=pasted, login_dir="/")
    assert "crucible-login.exit=0" in text, text
    assert "chmod" not in text and "Operation not permitted" not in text, text
    assert session.url is not None and session.url.startswith(AGY_URL_START)
    assert session.prompt == AGY_PROMPT
    assert f"stand-in read {len(pasted)} characters" in text
    # AGY echoes what is typed; the driver shows the mask instead.
    assert "[pasted code]" in text and pasted not in text


@needs_pty_tools
def test_the_driver_shows_codexs_url_and_code_and_it_finishes_on_its_own(
    tmp_path: Path,
) -> None:
    text, session = run_driver(tmp_path, "codex", replay_script("codex"), code=None)
    assert "crucible-login.exit=0" in text, text
    assert session.url == "https://auth.openai.com/codex/device"
    assert session.code == "TEST-C0DE9"
    assert session.prompt is None


@needs_pty_tools
def test_the_driver_reports_agys_own_timeout(tmp_path: Path) -> None:
    text, session = run_driver(tmp_path, "agy", replay_to_exit("agy_timeout", 1), code=None)
    assert "crucible-login.exit=1" in text, text
    assert session.error == FLOWS["agy"].timed_out_message


@needs_pty_tools
def test_the_driver_shows_an_unfinished_line_once_the_cli_goes_quiet(tmp_path: Path) -> None:
    """A prompt the patterns do not know, left without a newline, still reaches the
    operator once the CLI has been quiet for a few seconds."""
    stand_in = (
        "echo 'Visit https://example.invalid/sign-in'; printf 'Your answer please'; "
        "IFS= read -r -t 8 code; exit 0"
    )
    text, _session = run_driver(tmp_path, "codex", stand_in, code=None)
    assert "\nYour answer please\n" in text, text
    assert "crucible-login.exit=0" in text


@needs_pty_tools
def test_the_driver_shows_claude_codes_oauth_error_drawn_with_cursor_moves(
    tmp_path: Path,
) -> None:
    """Claude Code 2.1.280 answers a refused code with "OAuth error ... Press Enter to
    retry", drawn with cursor moves and carriage returns and no newline, so its last
    segment renders empty. The operator sees the error (hades #173); the bytes are the
    ones it printed in the worker image on 2026-09-29."""
    stand_in = (
        "echo 'Visit https://claude.com/cai/oauth/authorize?code=true'; "
        "printf ' Paste code here if prompted > '; IFS= read -r -t 8 code; "
        "printf '\\r\\033[1C\\033[4A\\033[95mOAuth error: Request failed with status "
        "code 400\\033[39m\\033[K\\r\\033[2B\\033[K\\r\\033[1C\\033[1B\\033[97mPress "
        "\\033[1mEnter\\033[22m to retry.\\r\\033[1B\\033[39m\\033[K\\r\\033[1B\\033[K\\r"
        "\\033[1A'; sleep 6; exit 1"
    )
    text, _session = run_driver(tmp_path, "claude_code", stand_in, code="refused-code#state")
    assert "OAuth error: Request failed with status code 400" in text, text
    assert "Press Enter to retry." in text, text
    assert "refused-code" not in text


@needs_pty_tools
def test_the_driver_never_shows_a_token_that_arrives_in_two_pieces(tmp_path: Path) -> None:
    """A quiet partial line is shown, unless it may be the start of a token: the token's
    first half waits for the rest, and the whole token is captured and masked."""
    first, rest = "sk-ant-oat01-", "A" * 40
    stand_in = (
        "echo 'Visit https://claude.com/cai/oauth/authorize?code=true'; "
        "printf ' Paste code here if prompted > '; IFS= read -r -t 8 code; "
        f"printf '{first}'; sleep 5; printf '{rest}\\n'; exit 0"
    )
    text, _session = run_driver(tmp_path, "claude_code", stand_in, code="good-code#state")
    assert first not in text, text
    assert "[captured to oauth-token]" in text, text
    assert "crucible-login.exit=0" in text


# ----- the code box for a prompt nobody has captured -------------------------------------


def test_a_cli_quiet_after_its_url_is_waiting_for_the_code(tmp_path: Path) -> None:
    flow = LoginFlow(
        harness="stand-in",
        argv=("stand-in",),
        image_binary="/usr/local/bin/stand-in",
        directory_env="HOME",
        directory_subdir="",
        pastes_code=True,
        captures_token=False,
        token_pattern="",
        token_file="",
        window="",
        prompt_pattern=r"^never matches$",
    )
    session = LoginSession(harness="stand-in", started_at=0.0)
    session.state = "waiting_for_operator"
    _consume("Open https://example.invalid/auth to sign in\n", flow, None, tmp_path, session, None)
    session.notice_waiting(flow)
    assert session.state == "waiting_for_operator"
    session.notice_waiting(flow, now=session.last_output_at + QUIET_PROMPT_SECONDS + 0.1)
    assert session.state == "waiting_for_code"
    assert session.prompt
    # After a code the CLI's own prompt decides; silence alone no longer asks again.
    assert session.accept_code("abc")
    session.state = "waiting_for_operator"
    session.notice_waiting(flow, now=session.last_output_at + QUIET_PROMPT_SECONDS * 10)
    assert session.state == "waiting_for_operator"


def test_a_cli_that_printed_no_url_is_not_waiting_for_a_code(tmp_path: Path) -> None:
    session = LoginSession(harness="agy", started_at=0.0)
    session.state = "waiting_for_operator"
    _consume("Starting up\n", FLOWS["agy"], None, tmp_path, session, None)
    session.notice_waiting(FLOWS["agy"], now=session.last_output_at + 60)
    assert session.state == "waiting_for_operator"


# ----- what the operator pastes -------------------------------------------------------


def test_agys_code_is_taken_from_the_redirect_the_browser_could_not_load() -> None:
    agy = FLOWS["agy"]
    assert normalize_code(agy, "  4/0AVG7fiQabc  ") == "4/0AVG7fiQabc"
    assert normalize_code(agy, "4%2F0AVG7fiQabc") == "4/0AVG7fiQabc"
    assert (
        normalize_code(
            agy, "http://localhost:38123/oauth-callback?state=xyz&code=4%2F0AVG7fiQabc&scope=a+b"
        )
        == "4/0AVG7fiQabc"
    )
    assert normalize_code(agy, "state=xyz&code=4%2F0AVG7fiQabc") == "4/0AVG7fiQabc"
    # Claude Code's code is pasted as the page shows it, `#` and all.
    assert normalize_code(FLOWS["claude_code"], " abc%2Fdef#state ") == "abc%2Fdef#state"


def test_a_pasted_code_the_cli_echoes_is_masked_on_every_path(tmp_path: Path) -> None:
    """The Docker and local logins read the CLI's terminal directly, where AGY echoes
    what is typed; the pasted code is masked there as the Kubernetes driver masks it."""
    session = feed_raw("agy", "agy", tmp_path)
    assert session.accept_code("4/0AVG7fiQ-secretish")
    _consume("4/0AVG7fiQ-secretish\r\n", FLOWS["agy"], None, tmp_path, session, None)
    assert session.lines[-1] == "[pasted code]"
    assert not any("4/0AVG7fiQ-secretish" in line for line in session.lines)


def test_the_code_script_sends_enter_a_second_after_the_code() -> None:
    """The Kubernetes login code script writes the code to $d/in, sleeps one second,
    then writes only the carriage return (hades #173)."""
    # Must contain: code to $d/in, then sleep 1, then only \\r to $d/in
    assert 'printf "%s" "$c" > "$d/in"' in _LOGIN_CODE_SCRIPT
    assert "sleep 1" in _LOGIN_CODE_SCRIPT
    assert 'printf "\\r" > "$d/in"' in _LOGIN_CODE_SCRIPT
    # Must NOT write the code and \\r in one printf
    assert 'printf "%s\\r" "$c" > "$d/in"' not in _LOGIN_CODE_SCRIPT
    # ENTER_PAUSE_SECONDS must be at least 1.0 so the pause matches the sleep
    assert ENTER_PAUSE_SECONDS >= 1.0


def test_a_second_code_during_the_enter_pause_is_refused() -> None:
    """During the one-second Enter pause the session is already ``waiting_for_operator``,
    so a second pasted code is refused by ``accept_code`` (hades #173, P2 review)."""
    flow = FLOWS["claude_code"]
    session = LoginSession(harness="claude_code", started_at=0.0, guidance=flow.guidance)
    session.state = "waiting_for_operator"
    _consume(
        "Welcome to Claude Code v2.1.280\n"
        "https://claude.com/cai/oauth/authorize?code=true\n"
        " Paste code here if prompted >\n",
        flow,
        None,
        Path("/tmp"),
        session,
        None,
    )
    assert session.state == "waiting_for_code"

    # First code accepted while waiting_for_code
    assert session.accept_code("first-code") is True

    # Simulate: the login path writes the code, sets state to
    # waiting_for_operator immediately, then sleeps ENTER_PAUSE_SECONDS before
    # sending Enter.  The accept_code check must fail once state is
    # waiting_for_operator (i.e. the state has already changed before the sleep
    # starts).
    session.state = "waiting_for_operator"
    assert session.accept_code("second-code-during-pause") is False

    # After the sleep the path sends Enter but the session is already done.
    assert session.state == "waiting_for_operator"
    # Verify the constant is non-zero so a sleep actually happens.
    assert ENTER_PAUSE_SECONDS >= 1.0
