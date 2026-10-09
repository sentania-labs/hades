# Card threads

Open a card at `/ui/tasks/{id}` (or its `/ui/board/{id}` link). The first visit by an
operator or orchestrator creates the card's room through the same application service
as `POST /v1/rooms`, with `kind: card` and the card's internal `card_task_id`. Later
visits reuse that room, including from another browser. Concurrent first visits lock
the task row before looking up and creating the room. Opening the page does not start
a runner; sending a message does.

If room settings or room repositories are not configured, the Thread panel shows the
existing notes and "Threads need the rooms settings; see Settings". It does not create
a room or offer room controls. The rest of the card, including History and Actions,
remains available.

The Thread panel uses the principal room's shared panel and stream script. It has the
same transcript window, composer, streaming replies, Interrupt control and History
link for older turns. The transcript window starts at 50 and grows to 500; earlier
turns remain stored. Changing the selector records a system turn in this card's room.
It does not change the principal room or the task's execution routing.

The selector starts with `Project default: <harness> (<model>)`. The settings backend
has no project-scoped room defaults, so these come from `rooms.default_harness` and
`rooms.default_model`. After a switch, the label becomes Talking to. The current room
model remains selectable even if it is removed from the configured model list. The
rooms backend currently supports only Claude rooms and refuses unsupported harnesses.

Existing operator notes appear as system turns in the panel, keeping their authors,
local times and complete text. They remain task-note records, so workers still receive
them in their next identity bundle. Save note is in Actions beside the thread.
The card's room session start also includes those notes, the title, objective,
acceptance criteria, PR URL and state, CI state and head, and references to each
attempt's transcript at `/v1/attempts/{id}/logs`.

The History strip is separate from turn history. It lists decision words, channel and
local time when `applies_to` names the card's internal ID or external ID, from the same
ledger query as `GET /v1/decisions`. It examines that API's maximum window of 500 recent
decisions. Task actions stay in Actions, with the note: "Clicks live here, beside the
thread, never in it."

Observers read existing rooms and notes without creating rooms or receiving mutating
controls. A closed room remains readable and does not get replaced on page load.
At phone widths the thread and Actions stack in one column; the composer stays inside
the thread and long transcript text wraps.

## Minion questions

The merged FDY-0586 API supplies questions and handoffs on task detail. Questions appear
as distinct Minion question bubbles with local times. While an unanswered question is
present, the composer says Answer and resume. Sending calls the existing question
answer endpoint through a CSRF-protected UI route, recording the answer and resuming
from the last attempt when the task can take a correction. Answered questions keep the
answer, author and local time in the card thread. Observers cannot answer.

When a minion asks, the task principal's latest open principal room receives only a
system turn: "A minion on <card> asked a question. Open the card." The question text
stays on the card. If that principal has no open room, no room is created by the
notification. The card History strip also includes recorded handoff words, direction,
action and local time.

## Verification

`tests/unit/test_card_thread.py` renders the real card page over fake rooms and ledger
repositories, exercises read-only routes, first-use reuse, history windows and the
shared JavaScript transport, and checks card session-start context. Tests cover missing
settings and room repositories, question answering through the API,
read-only access, principal-room pointers and handoff history. The shared script is
exercised for both normal messages and question answers. The phone layout has a
structural CSS regression check; browser geometry and live runner delivery require
the browser and deployment tiers.
