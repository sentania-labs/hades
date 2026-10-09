# Personas and scheduled jobs

Personas combine a role with named entries from the read-only catalog. Create and
manage them through `GET`, `POST`, `PUT`, and `DELETE /v1/personas`, or use the
builder at `/ui/personas`. Skill and tool values are catalog names. A request with
an unknown name is refused. Personas reference tools by name; credentials live in
Admin.

Scheduled jobs combine a persona, prompt or script, five-field cron expression,
human cadence label, project, and results destination. Their timezone is always
`America/Chicago`. Manage them through the corresponding CRUD methods under
`/v1/scheduled_jobs`, use `/ui/jobs`, or file one immediately with
`POST /v1/scheduled_jobs/{id}/run-now`. Observer tokens may read both resources.
Orchestrator and operator tokens may create, change, delete, and run them.

The supervisor checks enabled jobs on every tick. It calculates cron instants in
Central local time, including daylight saving transitions, and files each due run
as a normal task attempt owned by the job creator's persisted principal. The owner
must still have operator or orchestrator authority. Each due job has its own fenced
transaction: a failed submission rolls back only that job, logs its ID and error,
and leaves it due for the next tick. Other jobs and ordinary task launches continue.
Disabling or correcting a broken job stops repeated submission failures.

Runs choose an installed provider in this order: Kubernetes, Docker, host process,
then the fake test provider. They pass the same harness, credential, and wired
provider validation context as ordinary task submissions. A Docker-only deployment
therefore submits Docker runs. The task has whole-repository scope with the standing
protected paths, the repository policy's checks, the persona's default tier, and
the job's project. Its objective combines the persona role and task text. When
carry notes forward is enabled, it also includes memory recalled by the job name.
The generated task records the persona's catalog tool names, but those tools are
not mounted into the task yet. A later task will honor the recorded names.

Results destinations behave as follows:

- `inbox_card` gives the task the scheduled-job and Inbox tags. The run writes its
  findings in the completion report's summary. The board projects that saved summary
  into the Inbox card body and its existing preview text. Before a report arrives,
  the opened card shows the objective. The tag changes the display lane only; the
  task still follows the normal execution lifecycle.
- `chat_message` records the findings on the run and asks the runner to post a
  system turn in the principal room. The rooms API does not exist yet, so the run
  records the findings and explicitly says that it could not post the turn.
- `report_only` records the findings on the run without an Inbox or chat delivery.

The contract stores `scheduled_job` metadata, including the job ID, destination,
tags, and persona tool names. `GET /v1/scheduled_jobs` and the individual job
response expose `last_run` with the task ID, state, saved findings, and a delivery
note. Findings come from the latest valid work-attempt completion claim; review
reports do not replace them. Chat delivery explicitly reports that the rooms API
is unavailable in this deployment.

Daily and weekly presets generate cron expressions from the selected Central time
and weekday, including a matching human label. Select Raw cron to use the cron and
label fields directly. Existing jobs have click-only Enable or Disable buttons.

The pages are registered without navigation links because the shared base template
and renderer are outside this task. They use the existing responsive form and table
tokens, including horizontal containment for tables at 390 px. Run now is a POST
button, not an automatic page action, and new jobs have carry notes and enabled off
unless selected.

## Disabled example

This example is documentation only. It is not seeded into the database.

```yaml
persona_id: 01EXAMPLEPERSONA0000000000
name: Weekly dependency review
task_kind: prompt
task_text: Review dependencies and summarize actionable upgrades.
cadence: "0 9 * * 1"
cadence_label: Mondays at 9:00 AM Central
timezone: America/Chicago
results_to: inbox_card
carry_notes_forward: false
project: hades
enabled: false
```
