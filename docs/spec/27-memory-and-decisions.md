# 27. Memory and decisions: the shared store, the recall API, the decision ledger, and the Admin Memory page

Hades #208, the conversation design of 2026-10-08: transcripts stay per channel;
decisions and memory are shared by every channel and every persona; minion findings
become memory only when Hades or the operator promotes them. This page is the contract
for the store that holds the shared half, the API every channel runner recalls from and
promotes into, the append-only ledger of the principal's words, and the page where the
operator reads and tends both.

## What is shared and what is not

A channel (a Telegram chat, a room, a task) keeps its own transcript; nothing here
stores one. What every channel and every persona reads in common is two things:

- **Memory**: facts Hades may rely on, in words, each with where it came from, when it
  was true, and the scope tags a recall matches. A minion's finding is not memory until
  Hades or the operator promotes it; until then it is only in that minion's report.
- **Decisions**: the principal's words, verbatim, with the channel they were said in,
  when, where in that channel's transcript they sit, what they apply to, and who acted
  on them and when. The ledger is append-only. A line is never edited or deleted; a
  later line can say the earlier one no longer holds.

## Tables (migration 0058)

`memory_items`: `id`, `text`, `source`, `observed_at`, `scope_tags` (a varchar array,
GIN indexed), `promoted_by`, `promoted_at`, `superseded_by` (the replacing item, when
there is one), `superseded_at`. An item is never edited in place. An edit is a new item
that supersedes the old one, which then points at its replacement. A forget retires an
item with no replacement, so `superseded_at` alone marks an item that is no longer
current. Both kinds of retired item stay as the record of what was once remembered.

`decision_ledger`: `id`, `principal`, `channel`, `said_at`, `verbatim`,
`transcript_ref`, `applies_to` (a varchar array), `acted_by`, `acted_at`. The table
carries the append-only trigger revision 0001 gives `events`: PostgreSQL refuses every
UPDATE and DELETE, whoever asks. The contract for this work named the table
`decisions`; that name was taken in revision 0004 by Foundry's per-task decisions (the
`decisions` endpoint on tasks), so the shared ledger is `decision_ledger` and the API
path stays `/v1/decisions`.

Four event kinds join the audit: `memory_promoted`, `memory_superseded`,
`memory_forgotten` and `ledger_decision_recorded`. The downgrade archives their rows in
`events_0058_archive` and the upgrade moves them back, the pattern 14 describes.

## Recall

`GET /v1/memory?subject=...&tags=...&limit=...` returns the current items that answer a
recall, newest observed first, at most `limit` of them (default 20, at most 100). The
rule, defined once in `crucible/domain/memory.py` and applied the same way in SQL:

- Tags compare case-insensitively after trimming; `tags=a,b` and `tags=a&tags=b` both
  name a and b.
- The subject is split into words; words shorter than three characters and a short list
  of common words ("the", "with", "where") are dropped.
- With neither tags nor subject words, every current item answers. Otherwise an item
  answers when one of its scope tags is asked for, or one of the subject's words is in
  its text.
- An item that was superseded or forgotten never answers, whatever is asked.

The response says what was understood: the subject, the normalized tags, the limit, and
the items.

## Writing

| Method | Path | Who | What |
|---|---|---|---|
| POST | `/v1/memory` | orchestrator or operator | Promote: `text`, `source`, optional `observed_at` (default now), `scope_tags`. Returns the item, 201. Event `memory_promoted`. |
| POST | `/v1/memory/{id}/supersede` | orchestrator or operator | Edit by superseding: `text`, and optionally `source`, `observed_at`, `scope_tags`; a field left out keeps the old item's value. The new item is returned (201); the old one points at it. An item already superseded or forgotten is refused (409). Event `memory_superseded`. |
| POST | `/v1/memory/{id}/forget` | orchestrator or operator | Supersede with no replacement. Returns the retired item (200). Refused when already retired (409). Event `memory_forgotten`. |
| GET | `/v1/decisions?channel=&limit=` | any principal | The ledger, newest said first, at most `limit` (default 100, at most 500), optionally one channel's. |
| POST | `/v1/decisions` | orchestrator or operator | Append a line: `principal`, `channel`, `verbatim` are required; `said_at` defaults to now; `transcript_ref`, `applies_to`, `acted_by`, `acted_at` are optional, and `acted_at` defaults to now when `acted_by` is named without a time. Returns the line, 201. Event `ledger_decision_recorded`. There is no edit and no delete route. |

The role rule is the one `POST /tasks/{id}/dispositions` uses: Hades (the orchestrator
role) or the operator. The admin role is the operator's administrative role and is
accepted the same way, as the task note and waiver services accept it; an observer
reads. The services are `crucible/application/memory.py`; the API and the page are two
thin clients of them (ADR 0012).

## Task decisions are mirrored

From revision 0058 on, a decision recorded on a task through `POST /tasks/{id}/decisions`
(or the task page's waiver form, or a board card action, which all run the same
service) is also one line of the ledger: channel `task`, the task id in `applies_to`,
the deciding principal's words as `verbatim`, `said_at` the decision's time, the task
page (`/ui/tasks/{id}`) as the transcript ref, and the recording principal as
`acted_by`. The `decision_recorded` event carries the ledger line's id. Decisions
recorded before this revision are not migrated.

## The Admin Memory page

`/ui/memory` opens with the one line that states the rule: "Transcripts stay per
channel. Decisions and memory are shared by every channel and every persona. Minion
findings become memory only when Hades or Scott promotes them." Two tabs follow, each a
link (`?tab=memory`, `?tab=decisions`), rendered from the same services as the API:

- **Memory items**: text, source, when (the observed time, with the promoted time as the
  cell's title), scope, promoted by, and for a principal who may write, Edit and Forget.
  Edit opens a small form in the row (a corrected text and the tags) and saves as a new
  item that supersedes the old; Forget is one button. Both are plain form posts with the
  session's CSRF token; the page has no script.
- **Decisions**: the words with the principal under them, channel, local time, applies
  to, and the transcript as a link when the ref is a URL or a `/ui` path, otherwise as
  the words it is.

Times are the operator's local time (`service.render_timezone`, default America/Chicago)
with no UTC or Z. An observer reads both tabs without the controls. The page has no
navigation link yet: `base.html` and `render.py` belong to another task in the same
wave, and the link is a one-line follow-up there.

## Tests

`tests/unit/test_memory_store.py` covers the migration's graph and rendered SQL, the
recall rule (tags, subject words, recency, bound, nothing superseded), the role checks
and the supersede and forget chain, the ledger's lack of any edit or delete surface,
the task-decision mirror, the API through a test client, and the page's two tabs with
local times and click-only Edit and Forget. `tests/integration/test_migrations.py`
applies 0058 over a populated database, checks both tables and the append-only trigger,
and goes down and back up with the event archive; that tier is CI's.
