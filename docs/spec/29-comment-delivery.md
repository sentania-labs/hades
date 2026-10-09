# 29. Comment delivery, minion questions and bootstrap handoffs

Hades #208 item 2. Three records that make the conversation between the operator, the
worker (the minion) and Foundry legible on the task: where an operator's note is on its
way to the worker, the question a worker stopped on and its answer, and the decisions
handed between Foundry and Hades while both run the work. Backend only: the API carries
the records; the board and a card thread render them.

## Delivery states of a note

A note (04, `POST /tasks/{id}/notes`; hades #489) is the operator's words on a task.
From hades #208 item 2 it carries `delivery_state`, one of three, and the evidence the
state rests on. The supervisor sets the state from what it observed; the author never
does. The note body has no state field, and a body that names one is refused as an
unknown field (422). `add_note` stores every note `awaiting`.

| State | Meaning | Evidence recorded |
|---|---|---|
| `awaiting` | Written; no attempt has been given it yet. | none |
| `acknowledged` | The supervisor put the note at the top of an attempt's `IDENTITY.md` (06), on a first attempt, a retry or a correction. | `acknowledged.attempt_id`, `acknowledged.at` (local Central time) |
| `acted_on` | That attempt's report references the note, by its id or by quoting its first line as typed (case and runs of whitespace ignored; the quotation is at least 12 characters and stands on word boundaries, so a shorter note is referenced by its id only). This holds for a report that does not parse as well: the raw text is read for references and never stored. | `acted_on.attempt_id`, `acted_on.at`, `acted_on.commit` (the head the attempt's bundle collected, when there is one), `acted_on.event_seq` (the `report_parsed` or `report_parse_failed` event that read the report) |

Transitions are forward only and happen once:

- `awaiting` to `acknowledged` at launch, in the same unit of work that renders the
  identity (`task_notes.acknowledge_notes`). A note added after the launch stays
  `awaiting` until the next attempt carries it. Audit: `task_note_acknowledged`.
- `acknowledged` to `acted_on` at collection, after the report is parsed
  (`task_notes.mark_notes_acted_on`). A note the worker was never given cannot be acted
  on: an `awaiting` note stays `awaiting` however the report reads, and an acknowledged
  note the report does not name stays `acknowledged`. Audit: `task_note_acted_on`.

Both events are the supervisor's (`principal: crucible`) and carry the note id, the
attempt, the evidence sentence and `local_time`. The task read lists every note with
its state newest first; the identity renders the same view the supervisor acknowledges,
so what the worker saw and what Hades recorded are one list.

## A minion question as a record

A worker that stops on `blocked.md` has asked a question: the statement it wrote. The
supervisor opens the escalation as before (09, hades #393) and, in the same unit of
work, records a `MinionQuestion` beside it (`minion_questions.ask_question`):

| Field | Meaning |
|---|---|
| `question_text` | The worker's statement, verbatim. |
| `asked_by_attempt_id`, `asked_at` | The attempt that stopped and when (local Central on the task read). |
| `escalation_id` | The escalation the same stop opened. |
| `answered_by`, `answered_by_name`, `answered_at` | Who answered and when; empty until then. |
| `answer_text` | The answer, in the answering principal's words. |
| `answer_action` | `corrected` or `recorded` (below). |
| `answer_contract_version` | The correction version that carried the answer, when there is one. |

`GET /tasks/{id}` lists the task's questions oldest first under `questions`. Audit:
`minion_question_asked` on the attempt.

### The question flow

1. The worker writes `blocked.md` and exits 75. The supervisor classifies the attempt
   `blocked`, moves the task to `blocked`, opens the escalation and records the question.
2. The operator (or Foundry) reads the question on the task or the card and answers it
   through one call: `POST /tasks/{id}/questions/{question_id}/answer` with
   `answer_text` and, optionally, `resume_from` (`last_attempt`, the default, or
   `remote_branch` when a pull request exists). Operator, admin or orchestrator role.
3. Hades records the answer on the question (who, when, the words) and brings it back to
   the worker. When the task can take a correction (every `blocked` task can, 05), a
   correction version is attached with the answer as its `correction.instructions`,
   resumed from the sealed bundle (`last_attempt`) or from the pushed branch tip
   (`remote_branch`, which puts `resume_from_work_branch` on the `task_scheduled`
   event the next execution reads); that schedules the task again and closes the
   question's own escalation (and only that one) with a `correction` decision whose
   verbatim is the answer
   (`answer_action: corrected`). When the task cannot (it moved on, or was cancelled), the
   answer is kept and an escalation still open is closed with an `escalation_answer`
   decision (`answer_action: recorded`).
4. The next attempt reads the answer in its `IDENTITY.md` as the correction's
   instructions (06). Audit: `minion_question_answered` under the answering principal.

A question is answered once; a second answer is a conflict (409) that names the first.
The board's Answer action (`board_actions.apply_move("answer")`) goes through the same
call with the operator's note as the answer, so the board, the API and a future card
thread cannot disagree. An escalation that has no question record (opened before this
revision, or by Hades itself for a publication that cannot start) keeps the decision
path.

## Handoff events during bootstrap

While Foundry and Hades share the work, decisions pass between them. Each pass is one
`handoff_recorded` event on the task, readable on `GET /tasks/{id}/events` and
summarized on the task read under `handoffs`. The payload carries `action` (`accept`,
`merge`, `cancel`, `reroute`), `direction` (`foundry_to_hades`, `hades_to_foundry`),
`principal` (who handed it over; Hades's own are `crucible`), `local_time` (Central wall
time, `YYYY-MM-DD HH:MM CDT|CST`, never UTC or `Z`) and `words`, the principal's own.

| Action | Foundry to Hades | Hades to Foundry |
|---|---|---|
| `accept` | `POST /tasks/{id}/accept`: the reasoning. | An advisory gate failed on a passing head; the orchestrator review is required (11). |
| `merge` | The merge observed on GitHub, under the name of whoever merged (23). | The pull request is ready for merge; the sentence says whether Hades merges or the operator performs it. |
| `cancel` | `POST /tasks/{id}/cancel`: the verbatim. | (Hades cancels only on a principal's request; the request is the Foundry to Hades event.) |
| `reroute` | A correction that pins the harness: the instructions. | The supervisor rerouted the attempt (quota, refusal or provider error): the why. |

`application.handoffs.record_handoff` is the one writer; the event is never fenced
differently from its neighbours and never edited.

## Persistence

Revision `0060_comment_delivery` (14): seven columns on `task_notes`
(`delivery_state` with default `awaiting`, `acknowledged_attempt_id`, `acknowledged_at`,
`acted_on_attempt_id`, `acted_on_at`, `acted_on_commit`, `acted_on_event_seq`), the
`minion_questions` table, and the five event kinds in the `events` CHECK constraint. The
downgrade archives the five kinds' events in `events_0060_archive` and the next upgrade
restores them, the pattern 0055 set. The revision was assigned 0057 and is numbered past
`0059_rooms` because the memory store (0058) and the rooms (0059) reached main first; a
revision placed below a head that databases have already reached never runs on them. The
number stays provisional until merge (hades #447, 23), so the tests find the revision by
its slug and the revision reads its predecessor's event kinds through `down_revision`.
