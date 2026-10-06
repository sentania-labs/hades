# 06. Injected worker identity (WorkerIdentityV1)

The worker self-review is the internal review. The required `self_review` section
names where documentation was updated (or why no update was needed), maps every
acceptance criterion with evidence, and lists anything knowingly left out and why.

Crucible assembles a per-attempt identity bundle at launch, mounts it
read-only into the worker, and never writes it into the repository.

## Bundle layout (mounted at `/crucible/identity`)

```
identity/
  IDENTITY.md          rendered role, objective, boundaries, reporting protocol
  contract.yaml        the TaskContractV1 verbatim
  contract.json        the same document as JSON (worker images have jq, no YAML parser)
  contract.sha256
  project/             copies or references of project_instructions entries
  skills/<name>/       only the skills the contract names
  policy.md            the deterministic policy in words: timeouts, paths, gates
  report-schema.json   CompletionClaimV1 JSON schema
  hooks/commit-msg     Crucible's commit hook, the one executable file: adds the
                       `<commit_trailer>: <external_id>` trailer to every commit,
                       as a courtesy nothing checks
  history/             prior attempts' reports, open and answered escalations with
                       verbatim decisions, and the latest AcceptanceResult reasoning
                       (present on retries, after a decision, and on needs_more_work)
```

Skills named in `project_instructions` are copied from the configured
`skills_root`; only the named ones, nothing else in that root.

The bundle's content hash is recorded on the attempt. The rendered
`IDENTITY.md` is stored as an artifact so the exact instructions a worker saw
are reproducible.

## IDENTITY.md (rendered by `crucible/adapters/execution/identity.py`)

Short and plain: about 300 words for a typical contract (the operator's
direction of 2026-09-29, FDY-0140). Crucible re-runs every check, commits what
the worker leaves uncommitted, and enforces the scope and the gates itself, so
the worker is told what to do, not how Crucible checks it. In order:

1. **Heading and role.** The task id and title; the repository by name, the
   checkout path, the work branch and the base ref; "do the task yourself; do
   not redefine, widen or delegate it."
2. **Objective.** From the contract.
3. **This is a correction** (only on a correction version). The correction's
   instructions and the review comments or findings it addresses.
4. **Scope.** Allowed and prohibited paths, whether dependencies may be added
   and CI changed (yes or no), the network mode, "Commit your work on
   `work_branch`; never push.", and each of the contract's
   `constraints.prohibited_actions`. Nothing about the trailer, the author,
   hooks or `--no-verify`: none of them is the worker's concern, and nothing
   refuses a commit for them (operator decision, 2026-09-29, hades FDY-0143).
5. **Read first** (when the contract names any). The contract's `context`
   references and `project_instructions`, each as `kind: ref`.
6. **Acceptance criteria.** Each criterion's id and text.
7. **Checks.** The `required_verification` commands, verbatim, to run and fix
   what fails; an artifact entry is the file to write in the report directory.
   Then two lines: a program a required command needs that is missing from the
   image is not substituted for, it is `blocked.md` naming the program with the
   line `reason: missing_capability` (hades #393); and the
   sentence CONTRIBUTING.md shares, "Docker, kind and kubectl are absent in a
   worker and are CI's; a missing one is expected and is not a reason to stop."
   (hades #429). The two do not meet: a contract whose required check needs
   `docker`, `kind` or `kubectl`, including inside literal shell `-c` command
   strings, is refused at validation (05), so the first line
   is about `uv` or `gitleaks`, and the second is for the worker that reads a
   project's instructions to run the kind tier or build an image (ADR 0020).
8. **Report.** Write `/crucible/report/report.yaml` against
   `report-schema.json` with `schema_version: "1.0"` (the format version, not
   the schema's name, hades #181), `summary`, `self_review` (where documentation was
   updated or why no update was needed, every acceptance criterion mapped with evidence,
   and anything knowingly
   left out and why), `acceptance_mapping` (one entry
   per criterion id, which it lists, hades #187), `proposed_pull_request`, and
   the four lists; then run `crucible-report check` and fix every problem it
   prints (hades #215).
9. **If you are stuck.** Write `/crucible/report/blocked.md`: a first line
   `reason: missing_capability` (the task needs a program or capability the
   image does not have) or `reason: ambiguous_contract` (the contract reads
   more than one way and the readings differ in result), then what blocks you
   and what you tried, in your own words; then stop. One paragraph follows,
   the operator's words (hades #393): when the contract is ambiguous, stop
   rather than pick a reading, because "a workaround that changes the result
   is not a workaround, it is a wrong answer"; a blocked attempt is not
   retried, Hades hands the reason and the words verbatim to the person who
   answers, and a correction brings the answer back. Then the contract's
   `escalation.conditions`, listed as the cases to stop in. Crucible parses the reason line
   (`contracts.completion_claim.parse_blocked_md`) onto the attempt record and
   the escalation (09, 14); a reason it does not know stays in the statement.

No exit codes (a model cannot set its harness's exit code; `blocked.md` on a
clean exit is the escalation, 16), no precedence list, no author line (the
checkout's git config names the author, and the collector commits anything left
uncommitted as the policy's author, 08), and no log capture (Crucible
re-runs the checks, 11). Lists and booleans are rendered as words, never as
Python values.

## The commit hook

The preparer sets the checkout's `core.hooksPath` to
`/crucible/identity/hooks`, so the only hook that runs on a worker's commit
is Crucible's own text, mounted read-only and covered by the bundle hash; the
repository's own hooks never run (hades FDY-0135). The hook runs `git
interpret-trailers --if-exists doNothing --if-missing add` on the message
file and nothing else: no network, no repository content. A trailer with the
same key already in the message is kept as it is, so an amend, a harness
that writes the trailer itself, or a second run never adds a duplicate.
`--no-divider` keeps a `---` line in a body from being read as the start of
a patch. Any harness that commits through the git command line gets the
hook.

The trailer is a courtesy, not a requirement. On 2026-09-29 the operator
decided that the commit trailer stops being required and the task record is
the paper trail (hades FDY-0143): nothing checks the trailer, neither the
`commit_policy` gate (11) nor the publisher (23), and a commit made with
`--no-verify`, through a git library that runs no hooks, or by cherry-pick
is published like any other. The task view (04) records the work branch,
the pushed head, the pull request, and the merge commit and who merged.

## Harness-specific delivery

The bundle is the same for every harness. How the harness is pointed at it
differs (07): all harnesses use the single generated, untracked `AGENTS.md`
shim when the checkout has no applicable project instruction file. Claude Code
uses the existing project `CLAUDE.md` when one is present, so Crucible writes
no `AGENTS.md` shim in that case. Codex gets `IDENTITY.md` on stdin ahead of
the prompt; AGY gets `--add-dir /crucible/identity` and a short argv prompt
that says to read `IDENTITY.md` first. Hermes gets the text of `IDENTITY.md`
in its prompt, ahead of the pointer (FDY-0140): its launch wrapper reads the
mounted file, so the argv Crucible builds still carries only the pointer.

Shims are written by Crucible after checkout, listed in
`.git/info/exclude`, and their absence from the diff is a gate (11).

## What is never in the bundle

Credentials, the Crucible API token, GitHub tokens, other tasks'
contracts, external review feedback that Foundry has not turned into a
correction contract, the orchestrator's own identity, or any file the
contract did not name.
