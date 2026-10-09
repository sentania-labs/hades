"""The room runner (hades #208, ADR 0031, docs/spec/28-rooms.md).

Hades starts this as a Pod (or a container under `make up`) from the worker image, with
no checkout and no repository:

    uv run --no-project --with claude-agent-sdk python tools/room_runner.py

It holds one Claude Agent SDK client for one room. It asks Hades for its session start
(`GET /v1/rooms/{id}/session`), long-polls the room's inbox for injected messages
(`GET /v1/rooms/{id}/inbox`), streams each reply back as events on the assistant turn
(`POST /v1/rooms/{id}/turns/{seq}/events`), stops a turn when the inbox says
`interrupt`, and exits when it says `stop` or when no message came for the idle timeout
(`POST /v1/rooms/{id}/runner/exit`). Every call carries the room-scoped token Hades
minted for this launch, read from HADES_ROOM_TOKEN_FILE.

The model's only tools are the Hades tools, in-process SDK MCP tools on the server
`hades`, each a call to `POST /v1/rooms/{id}/tools/{name}`; a PreToolUse hook records
every tool call on the turn. The SDK options are the ones the spike proved
(docs/spikes/room-runner-sdk.md): `cli_path` the image's claude, `cwd` fixed per room,
`tools=[]`, `permission_mode="dontAsk"`, `extra_args={"permission-prompts": "none"}`,
`allowed_tools` naming only the Hades tools, the `disallowed_tools` list, and
`include_partial_messages=True`. The environment is scrubbed of every CLAUDE* and
ANTHROPIC* variable before the SDK starts its child; the child gets the OAuth token and
its own CLAUDE_CONFIG_DIR through `options.env` and nothing else of the parent's.

Only the standard library and `claude_agent_sdk` are used, and the SDK is imported only
when the runner starts, so the protocol is testable without it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("room_runner")

SCRUB_PREFIXES = ("CLAUDE", "ANTHROPIC")
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOOL_SERVER = "hades"
POLL_SECONDS = 25
# While a turn runs the inbox is asked often, so an interrupt lands within a second.
TURN_POLL_SECONDS = 1
FLUSH_SECONDS = 0.25
FLUSH_CHARS = 2000
# Statuses that mean this runner is not wanted any more: its token was revoked (401),
# the room is another's (403), gone (404), or its turn was ended by Hades (409).
GONE = frozenset({401, 403, 404, 409})

# The Hades tools, as the model sees them: name, what it does, and its JSON schema.
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "hades_recall": (
        "Recall what Hades remembers: the shared memory items matching a subject and "
        "scope tags, newest first.",
        {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["subject"],
        },
    ),
    "hades_record_decision": (
        "Record the operator's decision in the ledger. Quote the operator's own words "
        "exactly as they wrote them in this room; name the tasks it applies to.",
        {
            "type": "object",
            "properties": {
                "verbatim": {"type": "string"},
                "applies_to": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verbatim"],
        },
    ),
    "hades_file_card": (
        "File a proposed card (a task nothing starts until the operator approves it).",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "project": {"type": "string"},
            },
            "required": ["title", "objective", "project"],
        },
    ),
    "hades_read_task": (
        "Read a task this room may see: its card, or a card this room filed.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    ),
    "hades_post_note": (
        "Post a note on a task this room may act on.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["task_id", "text"],
        },
    ),
}


# ----- the environment ------------------------------------------------------------


def scrub_environment(environ: MutableMapping[str, str]) -> tuple[str | None, list[str]]:
    """Take the OAuth token out and delete every CLAUDE* and ANTHROPIC* variable, so
    the SDK's child inherits none of the parent's (the SDK copies os.environ)."""
    token = environ.get(TOKEN_ENV)
    removed = sorted(key for key in environ if key.startswith(SCRUB_PREFIXES))
    for key in removed:
        del environ[key]
    return token, removed


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    api_url: str
    room_id: str
    token: str = field(repr=False)
    model: str
    cwd: str
    cli_path: str
    config_dir: str
    idle_timeout_seconds: int
    oauth_token: str | None = field(default=None, repr=False)
    poll_seconds: int = POLL_SECONDS
    turn_poll_seconds: int = TURN_POLL_SECONDS

    @classmethod
    def from_environment(cls, environ: MutableMapping[str, str]) -> RunnerConfig:
        oauth, _removed = scrub_environment(environ)
        with open(environ["HADES_ROOM_TOKEN_FILE"], encoding="utf-8") as handle:
            token = handle.read().strip()
        return cls(
            api_url=environ["HADES_API_URL"].rstrip("/"),
            room_id=environ["HADES_ROOM_ID"],
            token=token,
            model=environ.get("ROOM_MODEL", ""),
            cwd=environ["ROOM_CWD"],
            cli_path=environ.get("ROOM_CLI_PATH", "/usr/local/bin/claude"),
            config_dir=environ["ROOM_CLAUDE_CONFIG_DIR"],
            idle_timeout_seconds=int(environ.get("ROOM_IDLE_TIMEOUT_SECONDS", "1800")),
            oauth_token=oauth,
        )


# ----- Hades ----------------------------------------------------------------------


class HadesError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class Transport(Protocol):
    def __call__(
        self, method: str, path: str, body: Mapping[str, Any] | None, timeout: float
    ) -> tuple[int, Any]: ...


def urllib_transport(api_url: str, token: str) -> Transport:
    """HTTP with the standard library: JSON in, JSON out, the bearer token on every call."""

    def call(
        method: str, path: str, body: Mapping[str, Any] | None, timeout: float
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{api_url}/v1{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else None
            except ValueError:
                return exc.code, {"detail": raw.decode("utf-8", "replace")}

    return call


class Hades:
    """The runner's side of the room protocol."""

    def __init__(self, room_id: str, transport: Transport) -> None:
        self.room_id = room_id
        self.transport = transport

    def _call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None, timeout: float = 30
    ) -> Any:
        status, payload = self.transport(method, f"/rooms/{self.room_id}{path}", body, timeout)
        if status >= 400:
            detail = payload.get("detail", "") if isinstance(payload, dict) else str(payload)
            raise HadesError(status, str(detail))
        return payload

    def session(self) -> dict[str, Any]:
        return dict(self._call("GET", "/session"))

    def inbox(self, wait: int) -> dict[str, Any]:
        return dict(self._call("GET", f"/inbox?wait={int(wait)}", timeout=wait + 30))

    def events(self, seq: int, events: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(self._call("POST", f"/turns/{seq}/events", {"events": events}))

    def tool(self, name: str, arguments: Mapping[str, Any]) -> tuple[bool, Any]:
        try:
            payload = self._call("POST", f"/tools/{name}", {"arguments": dict(arguments)})
        except HadesError as exc:
            return False, {"status": exc.status, "error": exc.detail}
        return True, payload.get("result") if isinstance(payload, dict) else payload

    def exit(self, reason: str) -> None:
        try:
            self._call("POST", "/runner/exit", {"reason": reason})
        except HadesError as exc:
            log.info("exit notice refused: %s", exc)


# ----- the SDK options --------------------------------------------------------------


def options_kwargs(
    config: RunnerConfig,
    session: Mapping[str, Any],
    *,
    server: Any,
    pre_tool_use: Any,
    resume: str | None = None,
) -> dict[str, Any]:
    """ClaudeAgentOptions as the spike proved them (docs/spikes/room-runner-sdk.md)."""
    child_env = {"CLAUDE_CONFIG_DIR": config.config_dir}
    if config.oauth_token:
        child_env[TOKEN_ENV] = config.oauth_token
    kwargs: dict[str, Any] = {
        "cli_path": config.cli_path,
        "cwd": config.cwd,
        "model": session.get("model") or config.model,
        "tools": [],
        "permission_mode": "dontAsk",
        "extra_args": {"permission-prompts": "none"},
        "allowed_tools": list(session["allowed_tools"]),
        "disallowed_tools": list(session["disallowed_tools"]),
        "include_partial_messages": True,
        # The CLI's own system prompt, with Hades's session start after it (the SDK's
        # default would replace it with an empty one).
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": str(session["system_prompt"]),
        },
        "env": child_env,
        "mcp_servers": {TOOL_SERVER: server},
        "hooks": {"PreToolUse": [pre_tool_use]},
    }
    if resume:
        kwargs["resume"] = resume
    return kwargs


class Client(Protocol):
    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


ClientFactory = Callable[[str | None], Awaitable[Client]]


def text_delta(message: Any) -> str | None:
    """The text of a partial `StreamEvent`'s `text_delta`, or None."""
    event = getattr(message, "event", None)
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    if delta.get("type") == "text_delta":
        return str(delta.get("text") or "")
    return None


def assistant_text(message: Any) -> str:
    parts = []
    for block in getattr(message, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def is_result(message: Any) -> bool:
    return type(message).__name__ == "ResultMessage"


def is_connection_failure(exc: BaseException) -> bool:
    """The SDK's ProcessError and CLIConnectionError: the child died or is gone."""
    return type(exc).__name__ in {"ProcessError", "CLIConnectionError", "CLIJSONDecodeError"}


# ----- the runner -------------------------------------------------------------------


class RoomRunner:
    """One room, one client, one turn at a time."""

    def __init__(
        self,
        config: RunnerConfig,
        hades: Hades,
        client_factory: ClientFactory,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.hades = hades
        self.client_factory = client_factory
        self.clock = clock
        self.session_id: str | None = None
        self.current_seq: int | None = None
        self.tool_calls: list[dict[str, Any]] = []
        self.stopping = False
        self.exit_reason = ""

    # The PreToolUse hook: every tool call the model makes, recorded on the turn before
    # it runs (an attempt the permission mode then denies is recorded too).
    async def pre_tool_use(
        self, input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        record = {
            "kind": "tool_call",
            "name": str(input_data.get("tool_name", "")),
            "input": dict(input_data.get("tool_input") or {}),
            "tool_use_id": tool_use_id,
        }
        self.tool_calls.append(record)
        if self.current_seq is not None:
            try:
                await asyncio.to_thread(self.hades.events, self.current_seq, [record])
            except HadesError as exc:
                log.warning("tool call not recorded: %s", exc)
        return {}

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """One Hades tool, as the SDK's in-process MCP tool returns it."""
        ok, result = await asyncio.to_thread(self.hades.tool, name, arguments)
        content = [{"type": "text", "text": json.dumps(result, sort_keys=True)}]
        return {"content": content, "is_error": not ok}

    async def run(self) -> str:
        """Until the inbox says stop, the token stops working, or the idle timeout."""
        client = await self.client_factory(None)
        last = self.clock()
        try:
            while not self.stopping:
                remaining = self.config.idle_timeout_seconds - (self.clock() - last)
                if remaining <= 0:
                    self.exit_reason = "idle"
                    await asyncio.to_thread(self.hades.exit, "idle")
                    break
                wait = max(0, min(self.config.poll_seconds, int(remaining)))
                try:
                    inbox = await asyncio.to_thread(self.hades.inbox, wait)
                except HadesError as exc:
                    if exc.status in GONE:
                        self.exit_reason = f"refused ({exc.status})"
                        break
                    raise
                if inbox.get("control") == "stop":
                    self.exit_reason = "stop"
                    break
                message = inbox.get("message")
                if message:
                    client = await self.answer(client, message)
                    last = self.clock()
                elif not wait:
                    await asyncio.sleep(0.01)
        finally:
            await client.disconnect()
        return self.exit_reason

    async def answer(self, client: Client, message: Mapping[str, Any]) -> Client:
        """Send one user turn and stream the reply into its assistant turn. Returns the
        client to use next: a new one resumed from the session when the child died."""
        seq = int(message["assistant_seq"])
        self.current_seq = seq
        buffer: list[str] = []
        streamed: list[str] = []
        final_text = ""
        interrupted = False
        error: str | None = None
        done = asyncio.Event()
        flushed_at = self.clock()
        # Deltas leave in order: one post at a time, whichever task flushes.
        posting = asyncio.Lock()

        async def flush(force: bool = False) -> None:
            nonlocal flushed_at
            async with posting:
                text = "".join(buffer)
                if not text or (
                    not force
                    and len(text) < FLUSH_CHARS
                    and self.clock() - flushed_at < FLUSH_SECONDS
                ):
                    return
                buffer.clear()
                flushed_at = self.clock()
                await asyncio.to_thread(self.hades.events, seq, [{"kind": "delta", "text": text}])

        async def watch() -> None:
            nonlocal interrupted
            while not done.is_set():
                try:
                    inbox = await asyncio.to_thread(self.hades.inbox, self.config.turn_poll_seconds)
                except HadesError as exc:
                    if exc.status in GONE:
                        self.stopping = True
                        interrupted = True
                        await client.interrupt()
                        return
                    raise
                control = inbox.get("control")
                if control in ("interrupt", "stop"):
                    interrupted = True
                    if control == "stop":
                        self.stopping = True
                    await client.interrupt()
                    return
                # A reply that pauses still reaches the stream within a poll.
                await flush()
                if not self.config.turn_poll_seconds:
                    await asyncio.sleep(0.01)

        watcher = asyncio.create_task(watch())
        try:
            await client.query(str(message["text"]))
            async for item in client.receive_response():
                delta = text_delta(item)
                if delta:
                    buffer.append(delta)
                    streamed.append(delta)
                    await flush()
                    continue
                if type(item).__name__ == "AssistantMessage":
                    final_text += assistant_text(item)
                if is_result(item):
                    session_id = getattr(item, "session_id", None)
                    if session_id and session_id != self.session_id:
                        self.session_id = str(session_id)
                        await asyncio.to_thread(
                            self.hades.events,
                            seq,
                            [{"kind": "session", "session_id": self.session_id}],
                        )
                    if getattr(item, "is_error", False):
                        interrupted = True
        except Exception as exc:
            if not is_connection_failure(exc):
                raise
            # docs/spikes/room-runner-sdk.md (f): the child died. Never connect() the old
            # client again; a new one resumes the session from this Pod's config dir.
            interrupted = True
            error = f"{type(exc).__name__}: {exc}"[:2000]
            with contextlib.suppress(BaseException):
                await client.disconnect()
            client = await self.client_factory(self.session_id)
        finally:
            # The watcher's poll in flight is let finish, not cancelled: a poll that
            # outlived its task could be handed the next message after this turn ends.
            done.set()
            with contextlib.suppress(BaseException):
                await watcher
        await flush(force=True)
        end: dict[str, Any] = {"kind": "end", "interrupted": interrupted}
        if not streamed and final_text:
            end["text"] = final_text
        if error:
            end["error"] = error
        try:
            await asyncio.to_thread(self.hades.events, seq, [end])
        except HadesError as exc:
            if exc.status not in GONE:
                raise
            self.stopping = True
        self.current_seq = None
        return client


# ----- the SDK ------------------------------------------------------------------------


def sdk_client_factory(
    config: RunnerConfig, session: Mapping[str, Any], runner_ref: list[RoomRunner]
) -> ClientFactory:
    """Build ClaudeSDKClients with the Hades tools as in-process MCP tools."""
    import claude_agent_sdk as sdk  # type: ignore[import-not-found]  # noqa: PLC0415

    def make_tool(name: str, description: str, schema: dict[str, Any]) -> Any:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            return await runner_ref[0].call_tool(name, args)

        return sdk.tool(name, description, schema)(handler)

    server = sdk.create_sdk_mcp_server(
        TOOL_SERVER, tools=[make_tool(n, d, s) for n, (d, s) in TOOLS.items()]
    )

    async def hook(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        return await runner_ref[0].pre_tool_use(input_data, tool_use_id, context)

    async def build(resume: str | None) -> Client:
        options = sdk.ClaudeAgentOptions(
            **options_kwargs(
                config,
                session,
                server=server,
                pre_tool_use=sdk.HookMatcher(hooks=[hook]),
                resume=resume,
            )
        )
        client: Client = sdk.ClaudeSDKClient(options=options)
        await client.connect()
        return client

    return build


async def main_async(environ: MutableMapping[str, str]) -> int:
    config = RunnerConfig.from_environment(environ)
    os.makedirs(config.cwd, exist_ok=True)
    hades = Hades(config.room_id, urllib_transport(config.api_url, config.token))
    session = hades.session()
    runner_ref: list[RoomRunner] = []
    runner = RoomRunner(config, hades, sdk_client_factory(config, session, runner_ref))
    runner_ref.append(runner)
    reason = await runner.run()
    log.info("room runner exiting: %s", reason)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    return asyncio.run(main_async(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
