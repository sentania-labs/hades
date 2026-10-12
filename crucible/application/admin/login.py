"""Credential onboarding (25): each harness's own interactive login, driven headlessly
through a pseudo-terminal, pointed directly at the dedicated Crucible directory.

The driver shows the operator the URL and the code, feeds back the code the operator
pastes, and writes any token the CLI prints once to a file, mode 600, without
displaying it. The operator's daily-use directories are never read, copied or
referenced: the login's home or config variable is the dedicated directory and nothing
else (12). The timing constraints are stated up front: Codex's device code expires in
15 minutes; AGY waits 60 seconds for the pasted code (S1b).

On Kubernetes the login is a Job in the workers namespace (26) and the credential is the
harness Secret the service owns (ADR 0015): nothing is written until the CLI has exited
and the files it wrote pass the shape check, and then the Secret is replaced whole. A
login and an attempt of the same harness never overlap (12): a login refuses while an
attempt holds the credential, and a launch waits while a login runs.
"""

from __future__ import annotations

import contextlib
import errno
import os
import pty
import re
import select
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
    record_refusal,
    refuse_secret_shaped,
)
from crucible.application.admin.credentials import (
    RETIRED_MARK,
    CredentialAdminError,
    check_shape,
    check_shape_files,
    secret_store,
    source_for,
    spec_for,
    stored_files,
)
from crucible.application.errors import ConflictError
from crucible.application.harnesses import credential_holders
from crucible.domain.events import EventKind
from crucible.domain.secrets import redact
from crucible.ports.harness import AGY_BINARY, CLAUDE_CODE_BINARY, CODEX_BINARY
from crucible.ports.repository import UnitOfWork

URL_RE = re.compile(r"https?://[^\s'\"<>]+")
# A device or one-time code the CLI shows for the operator to enter elsewhere.
CODE_RE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,6})\b")
# The generic prompt, for a flow that declares none of its own (a test's stand-in): a
# line that ends in a prompt character. Matching the words alone flipped the session to
# `waiting_for_code` on informational text ("visit the URL and enter the code"), before
# the CLI was reading, and the pasted code went nowhere. The three real harnesses each
# declare the prompt their CLI actually prints (hades #173), read from a capture of it.
PASTE_RE = re.compile(
    r"(?:(?:paste|enter)[^\n]{0,40}(?:code|token)[^\n]{0,40}|code|token)\s*[:>?]\s*$",
    re.IGNORECASE,
)
# The key a terminal sends for Enter. Claude Code reads its input raw, and only a
# carriage return submits there; a newline is taken as more of the code (hades #173). A
# CLI that reads a line in the terminal's normal mode gets a newline from it, as it
# would from a keyboard.
ENTER = "\r"
# How long the Enter waits after the code. Claude Code takes a code and its Enter that
# arrive in one read as a paste, so the carriage return becomes part of the pasted text
# and nothing submits; a separate keypress a moment later submits. Seen on the lab on
# 2026-09-29 with Claude Code 2.1.280 (hades #173).
ENTER_PAUSE_SECONDS = 1.0
# A CLI that has printed its sign-in URL and then nothing for this long is waiting for
# the operator, whatever its prompt says (hades #173).
QUIET_PROMPT_SECONDS = 5.0
# The service sees a prompt a little after the CLI printed it (the driver's one-second
# read, the log poll), so the deadline it shows for a CLI that gives up on its own is
# this much early rather than late (hades #173).
CODE_WAIT_MARGIN_SECONDS = 5

# What a terminal would show for one line of a CLI's output (hades #173). Ink (Claude
# Code) ends every line `\r\r\n` through the pty and separates words with cursor-column
# moves instead of spaces; an OSC 8 hyperlink carries the URL twice, once as the link
# and once as its visible text. The same rules as the login Pod's driver.
_CURSOR_MOVE_RE = re.compile(r"\x1b\[[0-9]*[GC]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESCAPE_RE = re.compile(r"\x1b[()]?[A-Za-z0-9=>]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def render_line(raw: str) -> str:
    """One line as a terminal would show it: every trailing carriage return dropped,
    then only what was drawn after the last one kept; a cursor-column move is a space;
    control sequences go, and an OSC 8 hyperlink leaves its visible text."""
    line = raw.rstrip("\r")
    line = line.rsplit("\r", 1)[-1]
    line = _CURSOR_MOVE_RE.sub(" ", line)
    line = _CSI_RE.sub("", line)
    line = _OSC_RE.sub("", line)
    line = _ESCAPE_RE.sub("", line)
    return _CONTROL_RE.sub("", line)


@dataclass(frozen=True, slots=True)
class LoginFlow:
    """One harness's login as the pty driver runs it."""

    harness: str
    # On the host the CLI is found on PATH, wherever the operator installed it. In the
    # worker image the first word becomes `image_binary`, the absolute path the adapters
    # launch it by (C11), so a login and a launch run the same file.
    argv: tuple[str, ...]
    image_binary: str
    # The variable that points the CLI at the dedicated directory (25 step 2).
    directory_env: str
    # Which subdirectory of the configured path that variable names ("" for the root).
    directory_subdir: str
    # Whether the operator pastes a code back into the CLI, and whether the CLI prints a
    # token once that becomes the credential file.
    pastes_code: bool
    captures_token: bool
    token_pattern: str
    token_file: str
    window: str
    # The prompt this CLI prints when it reads the pasted code, as captured from the CLI
    # itself (tests/fixtures_data/logins), matched on the rendered line. Empty: the
    # generic PASTE_RE.
    prompt_pattern: str = ""
    # How long the CLI itself waits for the code once it has asked, when it gives up on
    # its own (AGY: 60 seconds); 0 when it does not.
    code_wait_seconds: int = 0
    # The line the CLI prints when that wait ran out, and what the operator is told.
    timed_out_pattern: str = ""
    timed_out_message: str = ""
    # The code is copied out of a redirect's address bar (AGY): a pasted address yields
    # its `code` parameter, and a percent-encoded code is decoded.
    code_from_redirect: bool = False
    # What the operator does, in order, shown on the login page.
    guidance: tuple[str, ...] = ()


FLOWS: dict[str, LoginFlow] = {
    "claude_code": LoginFlow(
        harness="claude_code",
        argv=("claude", "setup-token"),
        image_binary=CLAUDE_CODE_BINARY,
        directory_env="CLAUDE_CONFIG_DIR",
        directory_subdir="",
        pastes_code=True,
        captures_token=True,
        token_pattern=r"(sk-ant-[A-Za-z0-9_-]{20,})",
        token_file="oauth-token",
        window=(
            "Claude Code: approve in the browser and paste the code; the long-lived token "
            "is shown by the CLI once and is captured to oauth-token, never displayed"
        ),
        prompt_pattern=r"^\s*Paste code here if prompted\s*>\s*$",
        guidance=(
            "Open the sign-in link and sign in with the Claude account the token is for, "
            "then approve.",
            "The page that follows shows a code. Copy all of it, paste it into the "
            "Authorization code box below and submit.",
            "Claude Code then prints a long-lived token once. Crucible writes it to "
            "oauth-token and never shows it.",
        ),
    ),
    "codex": LoginFlow(
        harness="codex",
        argv=("codex", "login", "--device-auth"),
        image_binary=CODEX_BINARY,
        directory_env="CODEX_HOME",
        directory_subdir="",
        pastes_code=False,
        captures_token=False,
        token_pattern="",
        token_file="",
        window="Codex: the device code expires in 15 minutes; enter it at the URL shown",
        guidance=(
            "Open the sign-in link and sign in with the ChatGPT account Codex is to use.",
            "Enter the one-time code shown below when the page asks for it. It expires "
            "15 minutes after Codex printed it.",
            "There is nothing to paste back: Codex notices the sign-in on its own and "
            "the login finishes by itself.",
        ),
    ),
    "agy": LoginFlow(
        harness="agy",
        argv=("agy", "-p", "Reply with exactly the word OK and nothing else."),
        image_binary=AGY_BINARY,
        directory_env="HOME",
        directory_subdir="",
        pastes_code=True,
        captures_token=False,
        token_pattern="",
        token_file="",
        window=(
            "AGY: the CLI waits 60 seconds for the pasted code; have the browser signed "
            "in before starting"
        ),
        prompt_pattern=r"^\s*Or, paste the authorization code here and press Enter:\s*$",
        code_wait_seconds=60,
        timed_out_pattern=r"authentication timed out",
        timed_out_message=(
            "AGY stopped waiting for the code after its own 60 seconds, so this login "
            "ended. Start the login again; sign in to Google in this browser first so "
            "the code is ready within the minute"
        ),
        code_from_redirect=True,
        guidance=(
            "AGY waits only 60 seconds for the code, counted from when it printed the "
            "link. Sign in to Google in this browser before starting, so the sign-in "
            "is one click.",
            "Open the sign-in link and choose the Google account AGY is to use.",
            "After you sign in, the browser ends on an address that does not load (on "
            "the lab, 2026-09-27, it was a localhost address this browser cannot "
            "reach). That is expected. Copy the whole address from the address bar, or "
            "just the value after code= up to the next &, paste it into the "
            "Authorization code box below and submit.",
            "If the 60 seconds run out, the login ends and says so. Start it again: it "
            "prints a new link, and a code from the old one no longer works.",
        ),
    ),
}


def normalize_code(flow: LoginFlow | None, pasted: str) -> str:
    """The code the CLI reads, from what the operator pasted. For a flow whose code is
    copied from a redirect's address bar (AGY), a whole address yields its `code`
    parameter, and a percent-encoded code (`4%2F0A...` as the address bar shows it) is
    decoded: the CLI refuses the encoded form as a malformed code."""
    code = pasted.strip()
    if flow is None or not flow.code_from_redirect:
        return code
    if "code=" in code:
        query = urlsplit(code).query if "://" in code else code.split("?", 1)[-1]
        found = parse_qs(query).get("code")
        if found:
            return found[0].strip()
    return unquote(code) if "%" in code else code


# A session in one of these states has its outcome decided: nothing the operator sends
# changes it.
_ENDED_STATES = ("finishing", "finished", "failed")


@dataclass(slots=True)
class LoginSession:
    """What an in-progress or finished login shows: never a token."""

    harness: str
    started_at: float
    # starting, waiting_for_operator, waiting_for_code, finishing, finished, failed.
    # `finishing` (Kubernetes): the CLI's part is over and the service is storing what it
    # wrote, deleting the Job and releasing the lock; a cancel or a code is refused.
    state: str = "starting"
    url: str | None = None
    code: str | None = None
    prompt: str | None = None
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    token_written: bool = False
    # Whether the login's auth files were stored in the harness Secret (Kubernetes, ADR
    # 0015). None where the CLI writes straight into the credential directory.
    credential_written: bool | None = None
    error: str | None = None
    cancel_requested: bool = False
    # What the operator does, from the flow, and when the CLI stops waiting for the code
    # (epoch seconds) for a CLI that gives up on its own.
    guidance: tuple[str, ...] = ()
    code_wait_ends_at: float | None = None
    codes_submitted: int = 0
    # When the CLI last printed a line (monotonic), for QUIET_PROMPT_SECONDS.
    last_output_at: float = field(default_factory=time.monotonic)
    _code_from_operator: str | None = None
    # The codes the operator pasted, masked wherever the CLI echoes one back: the
    # Kubernetes driver masks in the Pod, and this is the same for the Docker and local
    # logins, which read the CLI's terminal directly.
    _pasted: list[str] = field(default_factory=list)
    _wake: threading.Event = field(default_factory=threading.Event)
    _state_changed: threading.Event = field(default_factory=threading.Event)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "state": self.state,
            "url": self.url,
            "code": self.code,
            "prompt": self.prompt,
            "output_tail": self.lines[-20:],
            "exit_code": self.exit_code,
            "token_written": self.token_written,
            "credential_written": self.credential_written,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "guidance": list(self.guidance),
            "code_wait_ends_at": (
                datetime.fromtimestamp(self.code_wait_ends_at, UTC).isoformat()
                if self.code_wait_ends_at is not None
                else None
            ),
        }

    def submit_code(self, code: str) -> None:
        self._code_from_operator = code
        self._wake.set()

    def accept_code(self, code: str) -> bool:
        """Hand the operator's code to the login only while it waits for one. The check
        and the hand-over hold the same lock `begin_finishing` takes, so a code is never
        accepted for a CLI that has already exited."""
        with self._guard:
            if self.state != "waiting_for_code":
                return False
            self._code_from_operator = code
            self.codes_submitted += 1
            if code.strip():
                self._pasted.append(code.strip())
        self._wake.set()
        return True

    def notice_waiting(self, flow: LoginFlow, now: float | None = None) -> None:
        """hades #173: a CLI that printed its sign-in URL and then went quiet is waiting
        for the operator, so the code box is shown even when its prompt was not
        recognised. Only before the first code: after one, the CLI's own prompt says
        whether it wants another."""
        if not flow.pastes_code or self.url is None or self.codes_submitted or self.error:
            return
        moment = time.monotonic() if now is None else now
        with self._guard:
            if self.state != "waiting_for_operator":
                return
            if moment - self.last_output_at < QUIET_PROMPT_SECONDS:
                return
            self.state = "waiting_for_code"
            if self.prompt is None:
                self.prompt = "The CLI has shown the sign-in link and is waiting for input."
            self._notify()

    def request_cancel(self) -> str | None:
        """Mark the login cancelled and return the state it was cancelled in, or None
        when its outcome is already decided (`finishing`, `finished`, `failed`) and a
        cancel would be recorded as accepted and then overwritten."""
        with self._guard:
            if self.state in _ENDED_STATES:
                return None
            before = self.state
            self.cancel_requested = True
        self._wake.set()
        return before

    def begin_finishing(self) -> bool:
        """The CLI's part is over: from here the session refuses a cancel and a code, and
        reads `finishing` until the terminal state is set. Returns whether a cancel was
        accepted before, which the outcome then honours."""
        with self._guard:
            self.state = "finishing"
            self._state_changed.set()
            return self.cancel_requested

    def wait_for_change(self, timeout: float) -> bool:
        """Block until the session state changes or the timeout expires."""
        return self._state_changed.wait(timeout)

    def _notify(self) -> None:
        """Signal the poll loop that the state has changed."""
        self._state_changed.set()

    def wait_for_code(self, timeout: float) -> str | None:
        if self._wake.wait(timeout):
            self._wake.clear()
            code, self._code_from_operator = self._code_from_operator, None
            return code
        return None


def run_login(
    flow: LoginFlow,
    directory: str,
    *,
    session: LoginSession,
    argv: tuple[str, ...] | None = None,
    timeout: float = 900.0,
    emit: Callable[[str], None] | None = None,
) -> LoginSession:
    """Drive one login through a pty. `session` receives the URL, the code, and the
    prompts as they appear and supplies the operator's pasted code; the token, when the
    CLI prints one, is written to the credential file and replaced in the record."""
    target = Path(directory)
    if flow.directory_subdir:
        target = target / flow.directory_subdir
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "HOME": str(target),
        flow.directory_env: str(target),
    }
    master, slave = pty.openpty()
    try:
        try:
            process = subprocess.Popen(
                list(argv or flow.argv),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            # The CLI is not on this host. Without this the session stayed `starting` for
            # ever and every later login for the harness was refused as in progress.
            os.close(master)
            session.state = "failed"
            session.exit_code = None
            session.error = f"could not start {(argv or flow.argv)[0]}: {exc.strerror or exc}"
            session._notify()
            return session
    finally:
        os.close(slave)
    token_re = (
        re.compile(flow.token_pattern) if flow.captures_token and flow.token_pattern else None
    )
    buffer = ""
    deadline = time.monotonic() + timeout
    session.state = "waiting_for_operator"
    session._notify()
    try:
        while True:
            if session.cancel_requested:
                session.error = "login cancelled"
                process.kill()
                break
            if time.monotonic() > deadline:
                session.error = "login timed out"
                process.kill()
                break
            ready, _, _ = select.select([master], [], [], 0.25)
            if master in ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        chunk = b""
                    else:
                        raise
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                buffer = _consume(buffer, flow, token_re, target, session, emit)
            elif process.poll() is not None:
                buffer = _consume(buffer + "\n", flow, token_re, target, session, emit)
                break
            session.notice_waiting(flow)
            if session.state == "waiting_for_code":
                code = session.wait_for_code(0.25)
                if code is not None and session.error is None:
                    os.write(master, code.strip().encode("utf-8"))
                    session.state = "waiting_for_operator"
                    session._notify()
                    time.sleep(ENTER_PAUSE_SECONDS)
                    os.write(master, ENTER.encode("utf-8"))
        process.wait(timeout=5)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
    session.exit_code = process.returncode
    session.state = "finished" if process.returncode == 0 and not session.error else "failed"
    if session.state == "failed" and session.error is None:
        session.error = f"the login command exited {process.returncode}"
    session._notify()
    return session


def _consume(
    buffer: str,
    flow: LoginFlow,
    token_re: re.Pattern[str] | None,
    target: Path,
    session: LoginSession,
    emit: Callable[[str], None] | None,
) -> str:
    """Handle every complete line in the buffer; keep the partial tail (a prompt).

    Each line is rendered as a terminal would show it first (`render_line`): the Docker
    and local logins read the CLI's raw terminal output, and the Kubernetes driver's
    already rendered lines come through unchanged."""
    lines = buffer.split("\n")
    tail = lines.pop()
    for raw in lines:
        _line(render_line(raw), flow, token_re, target, session, emit)
    if tail:
        shown = render_line(tail)
        if _prompt_re(flow).search(shown):
            _line(shown, flow, token_re, target, session, emit)
            return ""
    return tail


def _prompt_re(flow: LoginFlow) -> re.Pattern[str]:
    return re.compile(flow.prompt_pattern, re.IGNORECASE) if flow.prompt_pattern else PASTE_RE


def _line(
    line: str,
    flow: LoginFlow,
    token_re: re.Pattern[str] | None,
    target: Path,
    session: LoginSession,
    emit: Callable[[str], None] | None,
) -> None:
    shown = line
    if token_re is not None:
        match = token_re.search(line)
        if match:
            _write_token(target / flow.token_file, match.group(1))
            session.token_written = True
            shown = line.replace(match.group(1), "[captured to " + flow.token_file + "]")
    for pasted in session._pasted:
        shown = shown.replace(pasted, "[pasted code]")
    shown = redact(shown)
    session.lines.append(shown)
    session.last_output_at = time.monotonic()
    session._notify()
    if emit is not None:
        emit(shown)
    url = URL_RE.search(shown)
    if url and session.url is None:
        session.url = url.group(0)
    code = CODE_RE.search(shown)
    if code and session.code is None and not flow.pastes_code:
        session.code = code.group(1)
    if flow.timed_out_pattern and re.search(flow.timed_out_pattern, shown, re.IGNORECASE):
        session.error = session.error or flow.timed_out_message or shown.strip()
        # The CLI has stopped reading: no code box, and a code is refused rather than
        # handed to a CLI on its way out.
        with session._guard:
            if session.state == "waiting_for_code":
                session.state = "waiting_for_operator"
        return
    if flow.pastes_code and _prompt_re(flow).search(shown):
        session.prompt = shown.strip()
        if flow.code_wait_seconds and session.code_wait_ends_at is None:
            session.code_wait_ends_at = (
                time.time() + flow.code_wait_seconds - CODE_WAIT_MARGIN_SECONDS
            )
        session.state = "waiting_for_code"
        session._notify()


def _write_token(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.chmod(path, 0o600)


# ----- the service ------------------------------------------------------------


def flows_for(ctx: AdminContext) -> dict[str, LoginFlow]:
    """The login flows this deployment knows: the three harnesses', plus any a test
    registers for a stand-in harness."""
    return {**FLOWS, **ctx.login_flows}


class LoginRegistry:
    """The in-progress logins of this process, one per harness (25: the API form
    returns the URL and polls for completion)."""

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}
        self._threads: dict[str, threading.Thread] = {}

    def get(self, harness: str) -> LoginSession | None:
        return self._sessions.get(harness)

    def resolve(self, ctx: AdminContext, harness: str) -> tuple[str, ...]:
        """Every refusal `start` can raise, raised before anything on disk has moved.

        The caller retires the credential that is at the configured path, so a refusal
        that only surfaced inside `start` left the credential renamed aside with no login
        running and the retention sweep free to shred it."""
        existing = self._sessions.get(harness)
        if existing is not None and existing.state == "finishing":
            raise ConflictError(
                f"the last login for {harness} has ended and is still cleaning up its Job "
                "and lock; retry once it reads finished or failed"
            )
        if existing is not None and existing.state not in ("finished", "failed"):
            raise ConflictError(f"a login for {harness} is already in progress")
        flow = flows_for(ctx)[harness]
        runner = self.container_runner(ctx) or self.job_runner(ctx)
        # An operator-configured command is used as given, in either mode.
        argv = tuple(ctx.login_commands.get(harness) or ())
        if not argv:
            argv = flow.argv if runner is None else (flow.image_binary, *flow.argv[1:])
        if runner is None and shutil.which(argv[0]) is None and not Path(argv[0]).exists():
            # The login drives the harness's own CLI, and only the worker images carry
            # the three; the Crucible service image carries none (13). Refusing here is
            # the difference between a clear message and a session that never finishes.
            raise ConflictError(
                f"{argv[0]} is not installed on this host, so the {harness} login cannot "
                "run here; run `crucible admin credentials login` in local mode on a host "
                f"that has {argv[0]}"
            )
        return argv

    @staticmethod
    def container_runner(ctx: AdminContext) -> Any | None:
        return next(
            (
                provider
                for provider in ctx.providers.values()
                if callable(getattr(provider, "run_login_container", None))
            ),
            None,
        )

    @staticmethod
    def job_runner(ctx: AdminContext) -> Any | None:
        """The provider that runs a login as a Job and stores it in a Secret (26, ADR
        0015), when this deployment keeps its credentials that way."""
        store = secret_store(ctx)
        if store is None or not callable(getattr(store, "run_login_job", None)):
            return None
        return store

    def start(
        self,
        ctx: AdminContext,
        harness: str,
        directory: str,
        *,
        image: str | None = None,
        accept: Callable[[Mapping[str, bytes]], Sequence[str]] | None = None,
        holder: str = "",
    ) -> LoginSession:
        argv = self.resolve(ctx, harness)
        flow = flows_for(ctx)[harness]
        runner = self.container_runner(ctx)
        job = self.job_runner(ctx) if runner is None else None
        if (runner is not None or job is not None) and not image:
            raise ConflictError(f"no promoted worker image is available for {harness}")
        lock = None
        if job is not None:
            if image is None or accept is None:
                raise ConflictError(f"the {harness} login needs an image and a store check")
            # The check in `resolve` sees this process's logins only; the lock is what
            # every api replica sees. Taken before anything is registered, so a refusal
            # leaves no session behind.
            lock = self._take_lock(job, harness, holder, ctx.login_timeout_seconds)
            if lock is not None:
                accept = partial(_accept_while_locked, job, lock, accept)
        session = LoginSession(harness=harness, started_at=time.time(), guidance=flow.guidance)
        self._sessions[harness] = session
        if job is not None:
            assert image is not None and accept is not None
            session.credential_written = False
            thread = threading.Thread(
                target=self._run_job,
                args=(job, flow, image, session, argv, accept, lock),
                kwargs={"timeout": ctx.login_timeout_seconds},
                daemon=True,
                name=f"login-{harness}",
            )
        elif runner is not None:
            assert image is not None
            thread = threading.Thread(
                target=self._run_container,
                args=(runner, flow, image, directory, session, argv),
                kwargs={"timeout": ctx.login_timeout_seconds},
                daemon=True,
                name=f"login-{harness}",
            )
        else:
            thread = threading.Thread(
                target=self._run,
                args=(flow, directory, session, argv),
                kwargs={"timeout": ctx.login_timeout_seconds},
                daemon=True,
                name=f"login-{harness}",
            )
        self._threads[harness] = thread
        try:
            thread.start()
        except Exception as exc:
            # A registered session that never reaches a terminal state refuses every later
            # login for the harness as one already in progress (correction 17), and a
            # thread that could not be created is exactly that case.
            self._threads.pop(harness, None)
            if lock is not None:
                _release_lock(job, lock)
            session.error = f"the login thread could not be started: {type(exc).__name__}"
            session.state = "failed"
            raise
        deadline = time.monotonic() + 5.0
        while session.state == "starting" and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if session.state == "failed":
            raise ConflictError(session.error or f"the {harness} login failed to start")
        return session

    @staticmethod
    def _take_lock(job: Any, harness: str, holder: str, timeout: int) -> Any | None:
        """The harness's login lock across every api replica, from a provider that has
        one (Kubernetes). The Docker provider runs in one api process by design, so the
        in-memory check in `resolve` is its whole lock."""
        acquire = getattr(job, "acquire_login_lock", None)
        if not callable(acquire):
            return None
        who = f"{holder or 'the admin service'} on {socket.gethostname()}"
        try:
            return acquire(harness, holder=who, timeout=timeout)
        except Exception as exc:
            raise ConflictError(str(exc)) from exc

    @staticmethod
    def _run(
        flow: LoginFlow,
        directory: str,
        session: LoginSession,
        argv: tuple[str, ...] | None,
        *,
        timeout: float,
    ) -> None:
        """Whatever happens in the thread, the session ends in a terminal state: a
        session stuck in `starting` blocks every later login for that harness."""
        try:
            run_login(flow, directory, session=session, argv=argv, timeout=timeout)
        except Exception as exc:  # the thread has nowhere to raise
            session.state = "failed"
            if session.error is None:
                session.error = f"the login driver failed: {type(exc).__name__}: {exc}"

    @staticmethod
    def _run_job(
        provider: Any,
        flow: LoginFlow,
        image: str,
        session: LoginSession,
        argv: tuple[str, ...],
        accept: Callable[[Mapping[str, bytes]], Sequence[str]],
        lock: Any | None = None,
        *,
        timeout: int,
    ) -> None:
        failure: str | None = None
        try:
            import asyncio  # noqa: PLC0415

            asyncio.run(
                provider.run_login_job(
                    flow=flow,
                    image=image,
                    session=session,
                    argv=argv,
                    timeout=timeout,
                    accept=accept,
                    lock=lock,
                )
            )
        except Exception as exc:
            failure = f"the login Job failed: {type(exc).__name__}: {exc}"
        finally:
            # The provider releases the lock before it ends the session. When it raised
            # or was interrupted first, the lock is released here, still before the
            # session reads failed: an operator who retries at once is not refused by
            # this login's own lock. A session that never reaches a terminal state
            # refuses every later login.
            if failure is not None or session.state not in ("finished", "failed"):
                if lock is not None:
                    _release_lock(provider, lock)
                session.error = failure or session.error or "the login Job ended without a result"
                session.state = "failed"

    @staticmethod
    def _run_container(
        runner: Any,
        flow: LoginFlow,
        image: str,
        directory: str,
        session: LoginSession,
        argv: tuple[str, ...],
        *,
        timeout: int,
    ) -> None:
        try:
            import asyncio  # noqa: PLC0415

            asyncio.run(
                runner.run_login_container(
                    flow=flow,
                    image=image,
                    directory=directory,
                    session=session,
                    argv=argv,
                    timeout=timeout,
                )
            )
        except Exception as exc:
            session.state = "failed"
            session.error = f"the login container failed: {type(exc).__name__}: {exc}"


def _release_lock(provider: Any, lock: Any) -> None:
    """Best effort: a lock that could not be deleted expires on its own."""
    with contextlib.suppress(Exception):
        provider.release_login_lock(lock)


def _accept_while_locked(
    provider: Any,
    lock: Any,
    accept: Callable[[Mapping[str, bytes]], Sequence[str]],
    files: Mapping[str, bytes],
) -> list[str]:
    """`accept`, and the lock is still this login's at the moment of the write: a login
    that outlived its lock may have been overtaken by another replica's."""
    problems = list(accept(files))
    held = getattr(provider, "login_lock_held", None)
    if callable(held) and not held(lock):
        problems.append(
            f"the {lock.harness} login lock expired and another login took it over, so "
            "this login's files were not stored"
        )
    return problems


def start_login(
    ctx: AdminContext,
    uow: UnitOfWork,
    registry: LoginRegistry,
    *,
    principal: str,
    harness: str,
    reason: str | None,
    replace: bool = False,
) -> dict[str, Any]:
    """25 steps 1 to 3: the dedicated directory, the CLI's own login pointed at it, the
    URL and code for the operator. The windows are stated before anything runs.

    A login writes into the configured directory, so an existing credential that still
    passes the shape check is not overwritten silently: `replace` retires it the way
    rotate does (renamed aside, shredded by the retention sweep) before the CLI runs.

    Nothing on disk moves until every precondition that can refuse has been checked, and
    a start that fails after the retire puts the credential back at its configured path.
    The harness CLI is absent from the service image (13), so the executable check alone
    used to rename a valid credential aside and then refuse, leaving the harness with no
    credential at its configured path and the retained copy eligible for the retention
    sweep."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials login {harness}"
    )
    flows = flows_for(ctx)
    if harness not in flows:
        raise ConflictError(f"harness {harness!r} has no interactive login flow")
    spec = spec_for(ctx, harness)
    job_runner = getattr(registry, "job_runner", lambda _ctx: None)(ctx)
    refuse_while_held(uow, harness, job_runner)
    container_runner = getattr(registry, "container_runner", lambda _ctx: None)(ctx)
    if job_runner is not None and container_runner is None:
        return _start_job_login(
            ctx,
            uow,
            registry,
            principal=principal,
            harness=harness,
            reason=reason,
            replace=replace,
        )
    source = source_for(ctx, harness)
    flow = flows[harness]
    # Every refusal first: the harness is known, the credential spec and directory are
    # configured, the CLI exists, no login is already running, the directory can be
    # written, and `replace` is set when a credential is there to be replaced. The event
    # this call ends with refuses a secret-shaped payload, so its one configured field is
    # scanned here too rather than after the credential has moved.
    argv = registry.resolve(ctx, harness)
    refuse_secret_shaped(" ".join(argv), field="login command")
    # Whether the existing directory is being replaced decides what has to be writable,
    # so it is decided first: a replacement renames the directory away and the CLI creates
    # a fresh one, and only the parent is written. An operator who protects a credential
    # directory read-only on purpose is entitled to replace it.
    replaceable = _check_replaceable(spec, source, harness=harness, replace=replace)
    _check_writable(source, harness=harness, reuse=not replaceable)
    image = None
    runner = getattr(registry, "container_runner", lambda _ctx: None)(ctx)
    if runner is not None:
        image = promoted_image(uow, harness)
    retired = (
        _retire_existing(ctx, uow, source, principal=principal, harness=harness, reason=reason)
        if replaceable
        else None
    )
    try:
        if runner is not None:
            Path(source.path).mkdir(parents=True, exist_ok=True, mode=0o700)
            session = registry.start(ctx, harness, source.path, image=image)
        else:
            session = registry.start(ctx, harness, source.path)
    except Exception as exc:
        if retired is None:
            raise
        restored = _restore_retired(
            ctx, source, retired, principal=principal, harness=harness, failure=type(exc).__name__
        )
        raise CredentialAdminError(
            f"the {harness} login could not start ({type(exc).__name__}) after the existing "
            + (
                "credential had been retired, so it was put back at its configured path"
                if restored
                else f"credential had been retired, and it could not be put back: it is at "
                f"{retired} and the configured path is not the credential. Move it back or "
                "rotate a prepared directory in before the retention sweep shreds it"
            )
            + f": {exc}"
        ) from exc
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_LOGIN_STARTED,
        principal=principal,
        reason=reason,
        before=None,
        after=None,
        harness=harness,
        window=flow.window,
        command=list(argv),
        image=image,
        retained_as=retired,
    )
    return {
        "harness": harness,
        "window": flow.window,
        "retained_as": retired,
        **session.as_dict(),
    }


def promoted_image(uow: UnitOfWork, harness: str) -> str:
    """The harness's own default worker image (ADR 0018), which is what a launch of it
    would use too, so a login runs it."""
    default = uow.harness_images.get(harness)
    if default is None:
        raise ConflictError(
            f"no worker image is promoted for {harness}; promote one on Images first"
        )
    return default.reference


def refuse_while_held(uow: UnitOfWork, harness: str, store: Any | None = None) -> None:
    """12: a login replaces the credential, and an attempt or a credential probe that
    holds a copy of the one it replaces would sync a refresh of a superseded session
    back over it. So a login waits until nothing of the harness holds the credential."""
    holders = credential_holders(uow, harness)
    if holders:
        raise ConflictError(
            f"an attempt of {harness} holds its credential ({', '.join(holders[:3])}); a "
            "login would replace it underneath that attempt. Start the login once the "
            "attempt has been collected"
        )
    probes = _probes_holding(store, harness)
    if probes:
        raise ConflictError(
            f"a credential probe of {harness} holds its credential ({', '.join(probes[:3])}); "
            "start the login once the probe has finished"
        )


def _probes_holding(store: Any | None, harness: str) -> list[str]:
    listing = getattr(store, "probes_holding", None)
    if not callable(listing):
        return []
    try:
        return list(listing(harness))
    except Exception as exc:
        raise ConflictError(
            f"whether a credential probe of {harness} is running could not be read: {exc}"
        ) from exc


def _start_job_login(
    ctx: AdminContext,
    uow: UnitOfWork,
    registry: LoginRegistry,
    *,
    principal: str,
    harness: str,
    reason: str,
    replace: bool,
) -> dict[str, Any]:
    """25 steps 1 to 3 on Kubernetes: the login runs as a Job (26) and its files go
    into the harness Secret the service owns (ADR 0015).

    Nothing is retired up front. The Secret is only replaced once the CLI has exited and
    its files pass the shape check, so a login that is cancelled, times out, or whose
    files fail the check leaves the credential exactly as it was, and there is no
    retained copy to shred. Files that pass are stored whatever the CLI's exit code, as
    a Docker login leaves what the CLI wrote. A credential that still passes the shape
    check is still not replaced without `replace`, the Docker rule."""
    flow = flows_for(ctx)[harness]
    spec = spec_for(ctx, harness)
    store = registry.job_runner(ctx)
    if store is None:
        raise ConflictError("no provider on this deployment runs a login Job")
    argv = registry.resolve(ctx, harness)
    refuse_secret_shaped(" ".join(argv), field="login command")
    current = stored_files(store, harness)
    if current and check_shape_files(spec, current).ok and not replace:
        raise ConflictError(
            f"the {harness} credential in the Secret {store.credential_secret(harness)} "
            "already passes the shape check; a login would replace it. Pass replace to "
            "replace it once the new login's files pass the shape check"
        )
    image = promoted_image(uow, harness)
    session = registry.start(
        ctx,
        harness,
        "",
        image=image,
        accept=partial(_accept_login, ctx, harness),
        holder=principal,
    )
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_LOGIN_STARTED,
        principal=principal,
        reason=reason,
        before=None,
        after=None,
        harness=harness,
        window=flow.window,
        command=list(argv),
        image=image,
        retained_as=None,
        secret=store.credential_secret(harness),
    )
    return {
        "harness": harness,
        "window": flow.window,
        "retained_as": None,
        "secret": store.credential_secret(harness),
        **session.as_dict(),
    }


def _accept_login(ctx: AdminContext, harness: str, files: Mapping[str, bytes]) -> list[str]:
    """Whether a Kubernetes login's files may replace the Secret: they pass the shape
    check, and no attempt of the harness has come to hold the credential while the
    login ran (12). Checked at the moment of the write, which is the moment it matters.
    Problems are named; nothing is ever quoted."""
    problems = list(check_shape_files(spec_for(ctx, harness), files).problems)
    with ctx.uow_factory() as uow:
        holders = credential_holders(uow, harness)
    try:
        holders += _probes_holding(secret_store(ctx), harness)
    except ConflictError as exc:
        problems.append(str(exc))
    if holders:
        problems.append(
            f"{', '.join(holders[:3])} came to hold the {harness} credential while the "
            "login ran, so the new one was not stored; run the login again once that "
            "has finished"
        )
    return problems


def _check_writable(source: Any, *, harness: str, reuse: bool) -> None:
    """What the login has to be able to write, and only that.

    The parent is written in both paths: the retire renames the directory inside it and
    the CLI creates the new directory there. The directory itself is only written when
    the login will reuse it, which is when there is nothing at the configured path to
    retire. A replacement renames it away and never writes into it, so a credential
    directory an operator deliberately holds read-only is still replaceable.

    `os.access` answers for the real uid, so this is the clear message rather than a
    guarantee: the rename itself is still the authority, which is why a failed start is
    restored."""
    current = Path(source.path)
    parent = current.parent
    if not parent.is_dir():
        raise CredentialAdminError(
            f"the credential root {parent} for harness {harness!r} does not exist, so the "
            f"login has nowhere to write (credentials.{harness}.path)"
        )
    if not os.access(parent, os.W_OK | os.X_OK):
        raise CredentialAdminError(
            f"the credential root {parent} for harness {harness!r} is not writable by the "
            "Crucible service user, so the login cannot create or retire the directory"
        )
    if reuse and current.is_dir() and not os.access(current, os.W_OK | os.X_OK):
        raise CredentialAdminError(
            f"the {harness} credential directory {current} is not writable by the "
            "Crucible service user, so the login cannot write into it; a login that "
            "replaces an existing credential renames the directory aside instead and "
            "does not need it writable"
        )


def _check_replaceable(spec: Any, source: Any, *, harness: str, replace: bool) -> bool:
    """Whether a credential that still passes the shape check is at the configured path,
    and therefore has to be retired before the login writes over it. Refuses when one is
    there and `replace` was not given. Moves nothing: the shape check parses the named
    auth files and no value leaves it (12)."""
    current = Path(source.path)
    if not current.is_dir() or not check_shape(spec, source.path).ok:
        return False
    if not replace:
        raise ConflictError(
            f"the {harness} credential already passes the shape check; a login would "
            "overwrite it. Pass replace to retain and replace it, or use rotate to swap "
            "in a prepared directory"
        )
    return True


def _retire_existing(
    ctx: AdminContext,
    uow: UnitOfWork,
    source: Any,
    *,
    principal: str,
    harness: str,
    reason: str,
) -> str | None:
    """Move the existing credential aside under rotate's retained name so the retention
    sweep shreds it on its own schedule. Nothing is shredded here and nothing is read.
    Every refusal has already been raised by the time this runs."""
    current = Path(source.path)
    stamp = ctx.clock.now().strftime("%Y%m%dT%H%M%SZ")
    retired = current.with_name(current.name + RETIRED_MARK + stamp)
    os.rename(current, retired)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_ROTATED,
        principal=principal,
        reason=f"login --replace: {reason}",
        before={"state": "present"},
        after={"state": "retained"},
        harness=harness,
        retained_as=retired.name,
        retention_hours=ctx.credential_retention_hours,
    )
    return retired.name


def _restore_retired(
    ctx: AdminContext,
    source: Any,
    retired_name: str,
    *,
    principal: str,
    harness: str,
    failure: str,
) -> bool:
    """The start failed after the credential had been moved aside, so put it back at the
    configured path: a credential is never left off its path because a later step failed.
    Returns whether it is back, because the caller's message to the operator is only true
    if it is.

    The caller's transaction rolls back with the exception and takes the retire event with
    it, but a rename does not roll back, so the undo is here and the failure is recorded
    through a unit of work of its own, the way a failed rotate records its own.

    Nothing at the configured path is removed to make room. A concurrent login that has
    already created the directory owns it, and renaming the retained copy over it would
    mix two credentials; the operator is told instead, which is recoverable, while a
    destroyed directory is not."""
    current = Path(source.path)
    retired = current.with_name(retired_name)
    restored = False
    problem = ""
    try:
        if not retired.is_dir():
            problem = "the retired directory is not where it was left"
        elif current.exists():
            if current.is_dir() and not any(current.iterdir()):
                current.rmdir()
                os.rename(retired, current)
                restored = True
            else:
                problem = "something else is at the configured path already"
        else:
            os.rename(retired, current)
            restored = True
    except OSError as exc:
        problem = f"the rename back failed with {type(exc).__name__}"
    record_refusal(
        ctx,
        principal=principal,
        operation=f"credentials login {harness}",
        detail=(
            f"the login failed to start ({failure}) after the credential was retired; "
            + (
                f"{retired_name} was renamed back to the configured path"
                if restored
                else f"{retired_name} is still retired and the configured path is not the "
                f"credential, because {problem}"
            )
        ),
    )
    return restored


def login_status(
    registry: LoginRegistry, harness: str, ctx: AdminContext | None = None
) -> dict[str, Any]:
    """The login's state; before any has run, what the operator will do in it."""
    session = registry.get(harness)
    if session is None:
        flow = (flows_for(ctx) if ctx is not None else FLOWS).get(harness)
        return {
            "harness": harness,
            "state": "none",
            "guidance": list(flow.guidance) if flow is not None else [],
        }
    return session.as_dict()


def submit_code(
    registry: LoginRegistry,
    harness: str,
    code: str,
    *,
    ctx: AdminContext | None = None,
    uow: UnitOfWork | None = None,
    principal: str = "",
    reason: str | None = None,
) -> dict[str, Any]:
    session = registry.get(harness)
    if session is None or session.state != "waiting_for_code":
        raise ConflictError(_not_waiting(harness, session))
    flows = flows_for(ctx) if ctx is not None else FLOWS
    code = normalize_code(flows.get(harness), code)
    if not code:
        raise ConflictError("the code is empty; paste the code the sign-in page showed")
    audited_reason: str | None = None
    if ctx is not None and uow is not None:
        audited_reason = guard_mutation(
            ctx,
            uow,
            reason,
            principal=principal,
            operation=f"credentials login code {harness}",
        )
    if not session.accept_code(code):
        raise ConflictError(_not_waiting(harness, session))
    if ctx is not None and uow is not None:
        assert audited_reason is not None
        admin_event(
            uow,
            ctx,
            EventKind.CREDENTIAL_LOGIN_CODE_SUBMITTED,
            principal=principal,
            reason=audited_reason,
            before=None,
            after={"submitted": True},
            harness=harness,
        )
    return session.as_dict()


def _not_waiting(harness: str, session: LoginSession | None) -> str:
    if session is not None and session.state == "finishing":
        return (
            f"the login for {harness} has already ended and is cleaning up its Job and lock; "
            "it no longer takes a code"
        )
    return f"no login for {harness} is waiting for a code"


def cancel_login(
    registry: LoginRegistry,
    harness: str,
    *,
    ctx: AdminContext,
    uow: UnitOfWork,
    principal: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials login cancel {harness}"
    )
    session = registry.get(harness)
    before = session.request_cancel() if session is not None else None
    if session is None or before is None:
        if session is not None and session.state == "finishing":
            raise ConflictError(
                f"the login for {harness} has already ended and is cleaning up its Job and "
                "lock; there is nothing left to cancel"
            )
        raise ConflictError(f"no login for {harness} is in progress")
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_LOGIN_CANCELLED,
        principal=principal,
        reason=reason,
        before={"state": before},
        after={"cancel_requested": True},
        harness=harness,
    )
    return session.as_dict()


def finish_login(
    ctx: AdminContext,
    uow: UnitOfWork,
    registry: LoginRegistry,
    *,
    principal: str,
    harness: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """25 step 4 after the CLI exits: validate the resulting structure and record only
    the result. The probe (steps 5 to 8) is `credentials validate`.

    It writes `session_compatibility` and clears `last_validated_at`, so it is a mutation
    and takes the same two guards as every other one."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials login finish {harness}"
    )
    session = registry.get(harness)
    if session is None or session.state not in ("finished", "failed"):
        raise ConflictError(f"the login for {harness} has not finished")
    spec = spec_for(ctx, harness)
    store = secret_store(ctx)
    if store is not None:
        shape = check_shape_files(spec, stored_files(store, harness) or {})
    else:
        shape = check_shape(spec, source_for(ctx, harness).path)
    state = uow.harnesses.get(harness)
    if state is not None:
        state.session_compatibility = "unverified"
        state.last_validated_at = None
        if session.credential_written:
            # The Secret now holds a different credential, so an auth failure seen on
            # the one it replaced says nothing about it, as after a rotate.
            state.last_auth_failure_at = None
        state.updated_at = ctx.clock.now()
        uow.harnesses.put(state)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_LOGIN_FINISHED,
        principal=principal,
        reason=reason,
        before=None,
        after={"shape_ok": shape.ok, "session_compatibility": "unverified"},
        harness=harness,
        exit_code=session.exit_code,
        token_written=session.token_written,
        credential_written=session.credential_written,
        shape=shape.as_dict(),
    )
    return {"harness": harness, "login": session.as_dict(), "shape": shape.as_dict()}
