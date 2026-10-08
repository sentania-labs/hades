# Crucible

Crucible is a deterministic supervisor for AI coding workers. It accepts an
explicit, versioned task contract, launches a worker harness (Claude Code,
Codex, AGY, Hermes, or Qwen Code) in an isolated execution environment, records everything the
worker does as durable events, logs, artifacts, and evidence, enforces
mechanical completion gates, and reports state through a versioned HTTP API.

It does not decide what to build. An orchestrator (Foundry, or a person)
decides outcomes, scope, model, and acceptance. Crucible executes, persists,
observes, and enforces.

**Status: specification version 0.3; implementation phase C7a (administrative UI).**
The specification is under [`docs/spec/`](docs/spec/00-overview.md) and the
decisions behind it under [`docs/adr/`](docs/adr/). Phase notes are under
[`docs/implementation-notes/`](docs/implementation-notes/c5.md).

## What it will do

- Validate a task contract before anything runs.
- Persist tasks, workers, executions, attempts, events, artifacts, evidence,
  decisions, and gate results in PostgreSQL, with explicit migrations.
- Launch workers as ephemeral containers (Docker locally, Kubernetes later),
  each with its own repository checkout, injected identity, and only the
  credentials its harness needs.
- Detect completion, failure, timeout, cancellation, stall, and loss with
  leases and heartbeats, and reconcile after its own restart.
- Evaluate deterministic definition-of-done gates and record the evidence.
- Keep authorized work running while the orchestrator is disconnected, and
  wake it when judgment is required.
- Own the routine GitHub mutations through a narrowly scoped GitHub App:
  push the verified branch, open the PR with a body built from verified
  evidence, watch external review and CI, observe the merge, and tag a
  release only from an explicit, operator-authorized release contract.
- Never give a worker a GitHub credential, the Docker socket, or another
  harness's credentials.

## What it will not do

Interpret ambiguous requirements, choose architecture, invent acceptance
criteria, broaden scope, pick a model by judgment, create follow-up work on
its own, approve semantic correctness, accept a risk, declare success,
interpret review feedback, merge a PR, or decide that a release should
happen.

## Running locally

Needs Docker with Compose, and [uv](https://docs.astral.sh/uv/) for the
developer mode and the test suites.

```sh
make up          # postgres, the two proxies, migrations, crucible on 127.0.0.1:8080
make dev         # postgres and the proxies; then: uv run crucible serve --all
make proxies     # the socket proxy and the egress proxy only
make lint        # ruff, mypy --strict, import-linter
make test        # unit tier, then the integration tier against postgres:16 in a container
make test PYTEST_WORKERS=0  # the same, serially in one process, for debugging
make smoke       # after `make up`: drive one task end to end; the same script CI and release run
make e2e-image   # the script-harness worker image the e2e tier runs
make e2e         # the Docker provider against real containers, no model
make e2e-github  # local only: the real GitHub App against a throwaway repository
make e2e-live    # local only: the real harness images with the dedicated credentials
make e2e-admin   # local only: the admin API and CLI on a live stack, probes with the dedicated credentials
make deploy-local # run a pinned published release from /var/lib/crucible/deploy
make down
make reset       # DESTRUCTIVE: down and delete the postgres and artifact volumes
```

### The Docker provider

Workers run on a **dedicated rootless Docker daemon** owned by a service user,
behind `docker-socket-proxy`; Crucible never sees the raw socket. Point the
Makefile at that daemon, which on a host where the service user has no login
shell means a wrapper:

```sh
make e2e DOCKER='sudo -u crucible -H env HOME=/var/lib/crucible-docker \
  XDG_RUNTIME_DIR=/run/user/$(id -u crucible) \
  DOCKER_HOST=unix:///run/user/$(id -u crucible)/docker.sock docker'
```

Nothing else changes: CI has an ordinary daemon and needs none of it. What a
worker gets is a checkout whose origin resolves nowhere, a read-only identity
bundle, a writable report directory, `--init`, uid 1000, all capabilities
dropped, `no-new-privileges`, a read-only root, and an `internal: true` network
whose only way out is the egress proxy's hostname allowlist. It never receives
the socket, the proxy endpoint, the database, or another harness's credential,
and `make e2e` asserts each of those from inside a real worker.

`make proxy-config` writes the egress proxy's allowlist from `EGRESS_ALLOWLIST`;
Crucible refuses to launch an attempt that needs a hostname the running proxy
does not permit, rather than letting it fail quietly on the network.

### The harnesses

Claude Code, Codex, AGY, Hermes and Qwen Code run behind one `HarnessAdapter`
port (spec 07), with a separate script harness for e2e tests. Each adapter declares the version
range it was tested with; the image label carries the installed version, and a
launch outside the range is refused with a wake. `GET /v1/harnesses` reports
installed and supported versions, both enable flags with their reasons, and a
sanitized credential state; `GET /v1/images` lists every labelled image with the
harnesses it is the default image of. Each harness has its own default image,
promoted and rolled back on its own (ADR 0018).

| Harness | Routing name | Endpoint |
| --- | --- | --- |
| Claude Code | `claude_code` | Subscription |
| Codex | `codex` | Subscription or local |
| AGY | `agy` | Subscription |
| Hermes | `hermes` | Local |
| Qwen Code 0.25.0 | `qwen_code` | Local |

Qwen Code uses the lab-local gateway key already configured for Hermes. Its
wrapper writes `~/.qwen/settings.json` before launch: `model.maxToolCallsPerTurn`
is `0` to prevent the per-turn loop cap from stopping productive runs, and
`model.generationConfig.contextWindowSize` is the routing model's `context_length`
(or **131072** tokens when absent). This is the full engine window: Qwen budgets
its output request inside it rather than adding 32000 output tokens beyond it.
Set the routing value to the engine's actual capacity. Mirroring Hermes (hades
#498), the wrapper also allows only the file and shell tools (no sub-agent, skill,
memory, web or MCP tool), loads no `QWEN.md` or `AGENTS.md` as rules, sets thinking
off and the response cap, and writes a minimal `report.yaml` from the run log when
the model left neither a report nor `blocked.md`. Both local wrappers, Qwen Code's
and Hermes's, start the harness again after a transport-level API error (the
gateway gave no answer: refused, reset, timed out), up to three times with 5, 15
and 45 second pauses, before giving up (hades #490); an attempt that still ends
`provider_error` is rerouted to the next eligible candidate, and the collector
leaves a bundle for a failed attempt with no commit too. The worker also includes
Node 22 and npm for repository checks (#288). Promote its image separately with
`crucible admin images promote <digest> --harness qwen_code`.

A harness credential is a directory Crucible reads (`[credentials.<harness>]`,
paths only), never the operator's own `~/.claude`, `~/.codex` or `~/.gemini`.
Per attempt, only the named auth files are seeded into a copy owned by the
worker's uid, mounted read-only or narrow-writable at the path the CLI expects
with Crucible-owned templates read-only on top; after the run the named files
are read back, a valid file with a newer issued-at is written back, and the copy
is removed. No credential value is in the create request, the environment,
argv, an event, a log, an artifact, or the database.

A harness ships disabled until its dedicated session and its daily-session
compatibility are verified (S1b); `[harnesses.<name>]` is the operator's gate
and migration 0008 seeds the administrator's flag the same way. `make e2e-live
HARNESS=<name>` runs one harness live against the throwaway repository:

```sh
make e2e-live HARNESS=claude_code \
  CRUCIBLE_LIVE_CREDENTIAL_ROOT=/path/to/dedicated/credentials \
  CRUCIBLE_GITHUB_APP_JSON=... CRUCIBLE_GITHUB_APP_KEY=... \
  CRUCIBLE_GITHUB_TARGET_REPO=owner/throwaway DOCKER='<the wrapper above>'
```

Administration (25) is one set of operations behind three entry points: the
server-rendered `/ui`, `/v1/admin`, and `crucible admin`. They call the same
services and own no separate state. On a fresh deployment, read the
one-time administrator token with `docker compose exec crucible cat
/var/lib/crucible/credentials/first-run-admin-token` (it is never in a log,
ADR 0016), then open `http://127.0.0.1:8080/ui`; the first sign-in removes
the file. Every mutation needs a live supervisor and leaves an event with the
principal and a before/after summary, never a value. A reason is an optional
audit note, required only to revoke a token, remove a repository or a
credential, or commit a bootstrap import:

```sh
crucible admin status                                        # the sanitized status document
crucible admin harnesses list
crucible admin harnesses disable codex --reason "refresh unverified"
crucible admin credentials status --harness claude_code      # presence, permissions, expiry class
crucible admin credentials validate --harness claude_code    # shape and expiry, no network
crucible admin credentials probe --harness claude_code       # bounded run of the hardened image
crucible admin credentials login --harness codex                 # prints the URL and the code; token file mode 600
crucible admin credentials rotate --harness agy --new-path /path/to/staged
crucible admin credentials remove --harness codex --reason "..."
crucible admin images list
crucible admin images promote <digest> --harness hermes   # per harness (ADR 0018)
crucible admin images rollback --harness hermes
crucible admin providers status
crucible admin github status
crucible admin github check                                  # mints and discards a token per registered repository
crucible admin audit tail --limit 50
```

The UI and API run `credentials login` inside the promoted worker image with
only that harness's credential subpath and the egress proxy. The CLI keeps its
local-host mode. A login refuses to overwrite a credential that still passes
the shape check unless `--replace` is given, which retires the old one first.
`rotate` copies the directory you name and leaves it in place; disposing of it
is yours to do.

Orchestrators read `GET /v1/capabilities`: which harnesses and images are enabled,
providers and GitHub reachable, and the worker, task and wake counts, nothing more.

`/v1/ready` reports not ready with "schema drift" when the live schema does not
match what the code expects (for example a database created by an earlier
build of this branch). Before the first tagged release the answer is
`make reset`, which deletes the database; after it, a migration.

`make up` copies `.env.example` to `.env` if none exists; change
`POSTGRES_PASSWORD` there. First use after `make up`:

```sh
# the first-run administrator token (never in a log; removed on first sign-in)
docker compose exec crucible cat /var/lib/crucible/credentials/first-run-admin-token
# Open http://127.0.0.1:8080/ui and create any additional principals there.
docker compose exec crucible crucible admin --reason "onboarding" repository register \
  --name example-service --url https://github.com/example-org/example-service \
  --installation-id 0 --attest-external-review-all-prs
curl -s http://127.0.0.1:8080/v1/ready
```

Then `POST /v1/tasks` with a `TaskContractV1` whose `execution_request.provider`
is `fake` and whose image is `crucible-worker:fake-succeed` (or `docker` with a
worker image the policy allowlist admits), `POST
/v1/tasks/{id}/start`, and watch `GET /v1/tasks/{id}/events`. OpenAPI is at
`/v1/openapi.json`.

### Running the released image on the rootless daemon

> These manifests are examples that track `latest`. A real deployment copies them into
> the deployer's own repository and pins the exact tag and digest there. `make
> deploy-local` below applies that same pin locally: give it `CRUCIBLE_DEPLOY_IMAGE` or
> `DEPLOY_TAG` as `tag` or `tag@sha256:...` and it runs exactly that reference.

`make up` runs your working tree, which means the compose client must read it,
which means it must run as the daemon's owner. On a workstation where the
`crucible` service user cannot read the operator's home (mode 750, correctly),
that cannot work. `make deploy-local` is the arrangement that does:

```sh
make deploy-local                              # the pin in DEPLOY_TAG
make deploy-local DEPLOY_TAG=0.2.2             # or another published release
make deploy-local DEPLOY_TAG=0.2.2@sha256:...  # tag and digest, passed through unchanged
make deploy-local-down                         # containers down, volumes kept
```

It creates `/var/lib/crucible/deploy` owned by the service user, containing
`compose.yaml`, an override that pins `ghcr.io/sentania-labs/crucible:<tag>` (or
`<tag>@sha256:...`, unchanged) and drops the build section, a generated `.env` whose
database password is created
once and never printed, and the egress allowlist; creates the credential root
layout under `/var/lib/crucible/credentials`; then brings the stack up as that
user against its own daemon and prints `/v1/ready`. Nothing in the deployment
directory references the operator's home, which is the point. Details:
[docs/implementation-notes/deploy-local.md](docs/implementation-notes/deploy-local.md).

## The supervision half, end to end

A run on the fake provider goes contract, attempt, collected head, gates,
internal review, acceptance:

```sh
GET  /v1/tasks/{id}                     # state, head_sha, gate_summary, review, acceptance
GET  /v1/attempts/{id}/gates            # one row per pre-PR gate, with its evidence ids
GET  /v1/attempts/{id}/evidence         # what the gates read; a worker row is never verified
GET  /v1/attempts/{id}/report           # the parsed CompletionClaimV1
POST /v1/tasks/{id}/review              # upload a ReviewReportV1, or ask for a review execution
POST /v1/tasks/{id}/accept              # Foundry's AcceptanceResult; Crucible never infers one
POST /v1/tasks/{id}/corrections         # a narrowed contract version and a `correct` execution
GET  /v1/wakes                          # what needs judgment; poll is the durable path
POST /v1/wakes/{id}/ack                 # what you did about it
```

The same operations from the command line are `crucible`'s orchestrator verbs,
which replace Foundry's `foundry-crucible` verb for verb. Each prints one JSON
envelope: the API's record as `data`, and in `next` the commands valid from
the record's state for the token in use, so an agent follows `next` rather
than a procedure of its own. [docs/client.md](docs/client.md) is the reference.

```sh
export CRUCIBLE_URL=http://127.0.0.1:8080 CRUCIBLE_TOKEN="$(cat ~/.config/crucible/token)"
crucible tasks --state awaiting_acceptance
crucible task 01M3...                    # the task, its state, and its `next` actions
crucible accept 01M3... --verdict accepted --reason "checked the evidence"
crucible wakes
crucible schema                          # the envelope and every kind's JSON schema
```

The gate rows say `pending` for `internal_review_recorded` until a non-author
review of that exact head exists. An `artifacts` deliverable reaches `accepted`;
a `branch` or `pull_request` one moves to `publishing`, where the supervisor
pushes the verified head and opens the pull request.

## The delivery half, end to end

Crucible, not the worker and not the orchestrator, performs every routine GitHub
mutation and watches the pull request afterwards:

```sh
GET  /v1/tasks/{id}/pull-request        # heads, review cycles, comments, reactions, CI
POST /v1/tasks/{id}/dispositions        # Foundry's reading of one review comment
POST /v1/tasks/{id}/ci-decision         # cause and action for a required CI failure
POST /v1/tasks/{id}/head-decision       # recollect, reject, or cancel a diverged head
POST /v1/github/webhook                 # optional accelerator, HMAC, off by default
```

A task that is accepted goes `publishing`, then `awaiting_external_review` or
`awaiting_ci_certification`, then `ready_for_merge`, and reaches `merged` when
Crucible observes the merge. **Merging is the operator's act: Crucible has no
merge endpoint.** A required CI failure lands in `ci_certification_failed` with
the check, the head, and a log excerpt, and never retries by itself. A head
Crucible did not push moves the task to `head_diverged` and supersedes that
head's acceptance, review, and gates.

Delivery needs a GitHub App with Metadata read, Contents read/write, Pull
requests read/write, Checks read, Actions read/write (write re-runs failed jobs on a
`ci-decision rerun`; without it the operator re-runs them), and Issues read (the last for
reactions on the pull request, which is the only place a clean external review
appears). The App id is configuration; the private key and the webhook secret
are *paths* to files mounted read-only into the `crucible` container alone.
Installation tokens are minted per job, live in memory and in the publisher
container's tmpfs, and are never in configuration, the environment, a log, an
event, or the database. Workers hold no GitHub credential at all.

Policies and the routing policy they name are documents, not defaults in code:

```sh
GET  /v1/policies/default-software/2    # every tunable the specification mentions
PUT  /v1/policies/{name}/{version}      # admin; a version a task references is immutable
GET  /v1/routing/default-routing/2      # the models Foundry may name, per tier
GET  /v1/routing/usage                  # per-pool usage in the current window
GET  /v1/routing/history?model=&project=  # what each model actually did
```

`examples/policies/default-software.yaml` is the seeded default (version 2), verbatim;
version 1, the C1 seed, names the placeholder routing policy and stays for reference.

## The bootstrap handoff

Until Crucible is ready, Foundry keeps its own ledger (`foundry-ledger`, SQLite). The
handoff is an import, not a copy:

```sh
POST /v1/import/bootstrap?reason=&owner=   # admin; the body is the exported bundle, verbatim
GET  /v1/import/bootstrap/{id}             # the verification report
POST /v1/import/bootstrap/{id}/commit      # makes the import authoritative
crucible admin bootstrap submit --file crucible.json --owner foundry   # the same, in process
crucible admin bootstrap show|list|commit
```

The bundle is validated in full (schema version, recomputed content hash, counts, id
uniqueness, every state mappable, event order) and either every record is written in
one transaction under an import in state `verified`, or nothing is and every problem
comes back. External ids are kept, timestamps are normalized to UTC with the original
kept in each event, and the report names every field the columns could not carry. The
commit records the handoff on every imported task; only one import ever holds
authority. Foundry then freezes its ledger and reads Crucible's state alone
(`docs/spec/15-bootstrap-ledger-handoff.md`, `docs/implementation-notes/c6.md`). The
readiness gate's evidence is `docs/readiness.md`.

## Releases

A release is a tag push, not a merge: `git tag -a vX.Y.Z -m vX.Y.Z && git push
origin vX.Y.Z` on `main`. The version is derived from that tag (`hatch-vcs`), so
nothing in the tree pins it and `/v1/health` reports what was tagged. The
release workflow refuses a tag that is not `vMAJOR.MINOR.PATCH` or whose commit
is not reachable from `main`, builds and smokes the image, publishes
`ghcr.io/sentania-labs/crucible:<version>` to GHCR, and then cuts the GitHub
release. Details: [docs/implementation-notes/release.md](docs/implementation-notes/release.md).

## Layout

```
crucible/       the service: domain, contracts, application, ports, adapters, scheduler, cli,
                and client (the library every `crucible` command group uses; docs/client.md)
tests/          unit (no I/O) and integration (PostgreSQL in a container, fake provider)
docs/spec/      the specification, one concern per file
docs/adr/       architectural decision records
docs/implementation-notes/  what each phase decided where the spec was open
docs/README.md            documentation index
examples/       sanitized example task and release contracts, policies, and configuration
```

## License

MIT. See [LICENSE](LICENSE).
