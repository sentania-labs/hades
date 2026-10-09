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
as a normal task attempt. The task has whole-repository scope with the standing
protected paths, the repository policy's checks, the persona's default tier, and
the job's project. Its objective combines the persona role and task text. When
carry notes forward is enabled, it also includes memory recalled by the job name.
The generated task records the persona's catalog tool names, but those tools are
not mounted into the task yet. A later task will honor the recorded names.

Results destinations behave as follows:

- `inbox_card` gives the task the scheduled-job and Inbox tags. The run writes its
  findings as the task card body shown in the board's Inbox lane.
- `chat_message` records the findings on the run and asks the runner to post a
  system turn in the principal room. The rooms API does not exist yet, so the run
  records the findings and explicitly says that it could not post the turn.
- `report_only` records the findings on the run without an Inbox or chat delivery.

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
