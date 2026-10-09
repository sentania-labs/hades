# Card threads

Open a card at `/ui/tasks/{id}` (or its `/ui/board/{id}` link). The first visit by an
operator or orchestrator creates the card's room through the same application service
as `POST /v1/rooms`, with `kind: card` and the card's internal `card_task_id`. Later
visits reuse that room, including from another browser. Concurrent first visits lock
the task row before looking up and creating the room. Opening the page does not start
a runner; sending a message does.

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

## Minion questions dependency

FDY-0586 is absent from the prepared `origin/main` merged for FDY-0595. Task detail
has no questions list and there is no question answer endpoint. This increment
therefore leaves out Answer and resume, question bubbles, principal-room question
pointers and FDY-0586 handoff events. The normal room composer remains available;
it does not claim to answer or resume a minion. Existing escalation actions keep their
existing behavior in Actions. Wire the question records and answer endpoint when
FDY-0586 is available.

## Verification

`tests/unit/test_card_thread.py` renders the real card page over fake rooms and ledger
repositories, exercises read-only routes, first-use reuse, history windows and the
shared JavaScript transport, and checks card session-start context. An empty or extra
fake questions list verifies that an unavailable answer endpoint does not produce an
Answer control. The phone layout has a structural CSS regression check; browser
geometry and live runner delivery require the browser and deployment tiers.
