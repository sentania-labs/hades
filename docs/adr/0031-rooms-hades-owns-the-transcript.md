# ADR 0031: Rooms: Hades owns the transcript

## Status

Accepted.

## Context

Hades #208 asks for a persistent principal: Scott talks to Hades, closes the laptop,
comes back from another client, and the same conversation continues. The two spikes of
2026-10-08 settled how a model is driven. `docs/spikes/principal-harness.md` showed that
a Claude Code session resumes across processes only while its session file exists, and
that a wiped store is a hard error that only Hades's own record can repair.
`docs/spikes/room-runner-sdk.md` showed that a Claude Agent SDK client over the
stream-json CLI takes injected turns, streams partial text, interrupts, runs Hades's
client as in-process tools and fires a PreToolUse hook, but that a killed child cannot
be reconnected and that the partial text of an interrupted turn is kept only sometimes.

So the harness's session is not a record anyone can rely on. It lives in one
CLAUDE_CONFIG_DIR, under one working directory, as long as one process does.

## Decision

A room is a conversation whose transcript Hades owns. Every turn is a row of
`room_turns` (migration 0059) written before any harness sees it: the operator's words
when they arrive, the assistant's reply as it streams, a system line when something
about the room changes. The harness session is a cache.

- A room runner is a process Hades starts when a message finds no runner warm, in a Pod
  from the worker image (a container under `make up`), with no checkout and no
  repository. It holds one SDK client, reads injected turns from the room's inbox and
  posts its reply back as events on the assistant turn. It stops when Hades says so (a
  switch, a close, the idle reclaim) or when its own idle timer runs out.
- Every runner starts a new harness session from the record: the identity in
  `config/principal/IDENTITY.md`, the shared memory recalled for the room's subject,
  the decisions that touch it, a rolling summary of older turns and the last turns
  verbatim. It never depends on a session another runner held.
- A switch of harness or model is a system turn ("Switched to ... Same history, same
  memory."), a stopped runner, and a new one on the next message from the same record.
- The runner acts through a room-scoped token Hades mints at each launch and forgets at
  each stop. The token is good on the room's runner routes and nothing else, and its
  tools act only on the room's card and the cards the room filed.
- A decision is recorded only when the agent calls `hades_record_decision` with the
  operator's own words from the room; Hades does not infer approvals.

## Consequences

A runner can die, be reclaimed after `rooms.idle_timeout_minutes` (30 by default) or be
replaced by another harness without losing a word: the next runner reads the record. The
cost is a cold session on every start (the prompt cache is warm only for what repeats),
the session-start text Hades assembles, and an installation of `claude-agent-sdk` by
`uv` when the runner Pod starts. CLAUDE_CONFIG_DIR is an emptyDir for now, so a session
does not outlive its Pod; a persistent per-room volume would let a runner resume
instead, and changes nothing in this record. Only `claude_code` is a room harness in
this increment; any other harness is refused with 409 until its runner exists.
