# Documentation index

**Operating** -- Deployment and client reference.

| File | Purpose |
| --- | --- |
| [`deployment.md`](deployment.md) | Deploying Crucible on Kubernetes |
| [`client.md`](client.md) | The `crucible` client: a reference for agents |
| [`deploy/kubernetes/secret-shapes/README.md`](../deploy/kubernetes/secret-shapes/README.md) | Secret shapes |

**Direction** -- Vision and roadmap.

| File | Purpose |
| --- | --- |
| [`vision.md`](vision.md) | Hades Roadmap and Bootstrap Directive |
| [`roadmap.md`](roadmap.md) | Hades roadmap |

**Design** -- Specifications. Each spec covers one concern; the code and ADRs win where they differ.

| File | Purpose |
| --- | --- |
| [`00-overview.md`](spec/00-overview.md) | 00. Overview, scope, and non-goals |
| [`01-architecture.md`](spec/01-architecture.md) | 01. Architecture and trust boundaries |
| [`02-prior-art.md`](spec/02-prior-art.md) | 02. Prior-art decision record |
| [`03-domain-model.md`](spec/03-domain-model.md) | 03. Domain model and ownership of authoritative state |
| [`04-api.md`](spec/04-api.md) | 04. Versioned API contracts |
| [`05-task-contract.md`](spec/05-task-contract.md) | 05. Task contract schema (TaskContractV1) |
| [`05b-policy-schema.md`](spec/05b-policy-schema.md) | 05b. Policy schema (PolicyV1) |
| [`06-worker-identity.md`](spec/06-worker-identity.md) | 06. Injected worker identity (WorkerIdentityV1) |
| [`07-harness-adapters.md`](spec/07-harness-adapters.md) | 07. Harness adapter contracts |
| [`08-execution-providers.md`](spec/08-execution-providers.md) | 08. Execution-provider contracts |
| [`09-lifecycle.md`](spec/09-lifecycle.md) | 09. Lifecycle state machines |
| [`10-events-leases-reconciliation.md`](spec/10-events-leases-reconciliation.md) | 10. Events, leases, heartbeats, and reconciliation |
| [`11-definition-of-done.md`](spec/11-definition-of-done.md) | 11. Definition of done, gates, and evidence |
| [`12-credentials.md`](spec/12-credentials.md) | 12. Credential and secret handling |
| [`13-local-operation.md`](spec/13-local-operation.md) | 13. Local operation: Docker Compose, worker images, and the Docker security model |
| [`14-persistence.md`](spec/14-persistence.md) | 14. PostgreSQL schema outline and migration strategy |
| [`15-bootstrap-ledger-handoff.md`](spec/15-bootstrap-ledger-handoff.md) | 15. Foundry bootstrap ledger and authority handoff |
| [`16-failure-semantics.md`](spec/16-failure-semantics.md) | 16. Failure, restart, retry, cancellation, cleanup, and retention |
| [`17-notification.md`](spec/17-notification.md) | 17. Notification and Foundry-wake contract |
| [`18-testing.md`](spec/18-testing.md) | 18. Testing strategy |
| [`19-readiness-gate.md`](spec/19-readiness-gate.md) | 19. Crucible readiness gate |
| [`20-implementation-phases.md`](spec/20-implementation-phases.md) | 20. Proposed implementation phases |
| [`21-spikes.md`](spec/21-spikes.md) | 21. Technical spikes (Phase C0) |
| [`22-open-questions.md`](spec/22-open-questions.md) | 22. Decisions taken and questions still open |
| [`23-github-delivery.md`](spec/23-github-delivery.md) | 23. GitHub delivery: publication, PR observation, external review, CI certification |
| [`24-release.md`](spec/24-release.md) | 24. Release contract and release lifecycle |
| [`25-administration.md`](spec/25-administration.md) | 25. Crucible administration: admin API, `crucible admin` CLI, credential onboarding |
| [`26-kubernetes-provider.md`](spec/26-kubernetes-provider.md) | 26. Kubernetes execution provider |
| [`27-comment-delivery.md`](spec/27-comment-delivery.md) | 27. Comment delivery states, minion questions as records, and bootstrap handoff events |

**Decisions** -- Architectural decision records.

| File | Purpose |
| --- | --- |
| [`0001-modular-monolith-python.md`](adr/0001-modular-monolith-python.md) | ADR 0001: Modular monolith in typed Python. Status: proposed. |
| [`0002-fastapi-pydantic-sqlalchemy-alembic.md`](adr/0002-fastapi-pydantic-sqlalchemy-alembic.md) | ADR 0002: FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, pytest. Status: proposed. |
| [`0003-postgres-authoritative.md`](adr/0003-postgres-authoritative.md) | ADR 0003: PostgreSQL is the only authoritative state. Status: proposed. |
| [`0004-docker-socket-proxy.md`](adr/0004-docker-socket-proxy.md) | ADR 0004: Rootless Docker daemon preferred; restricted socket proxy in front of whichever daemon is used. Status: accepted. |
| [`0005-container-is-the-boundary.md`](adr/0005-container-is-the-boundary.md) | ADR 0005: The execution environment is the security boundary; harness sandboxes are defense in depth. Status: proposed. |
| [`0006-sqlite-bootstrap-ledger.md`](adr/0006-sqlite-bootstrap-ledger.md) | ADR 0006: SQLite bootstrap ledger for Foundry until handoff. Status: accepted. |
| [`0007-github-app-and-crucible-owned-mutations.md`](adr/0007-github-app-and-crucible-owned-mutations.md) | ADR 0007: GitHub App authentication; Crucible owns all routine GitHub mutations; workers hold no GitHub credential. Status: accepted. |
| [`0008-external-review-bounded.md`](adr/0008-external-review-bounded.md) | ADR 0008: External review is a bounded quality input, not a consensus loop. Status: accepted. |
| [`0009-ci-certification-escalates.md`](adr/0009-ci-certification-escalates.md) | ADR 0009: Pre-PR verification is the proof; PR CI is certification; a required failure escalates. Status: accepted. |
| [`0010-release-by-contract-and-tag.md`](adr/0010-release-by-contract-and-tag.md) | ADR 0010: Releases happen only through an operator-authorized release contract; Crucible tags, the repository's workflow releases. Status: accepted. |
| [`0011-harness-version-pinning-and-promotion.md`](adr/0011-harness-version-pinning-and-promotion.md) | ADR 0011: Harness versions are pinned per image, recorded per attempt, and promoted explicitly. Status: accepted. |
| [`0012-admin-api-and-cli-share-services.md`](adr/0012-admin-api-and-cli-share-services.md) | ADR 0012: Administration is a versioned admin API and a CLI on the same application services; no web UI before readiness. Status: accepted. |
| [`0013-postgresql-only-transport.md`](adr/0013-postgresql-only-transport.md) | ADR 0013: PostgreSQL is the only transport; no Redis or broker until measured need. Status: accepted. |
| [`0014-quota-observation.md`](adr/0014-quota-observation.md) | ADR 0014: Subscription quota is observed through a pinned third-party reader, and is advisory only. Status: rejected. |
| [`0015-service-owns-harness-credential-secrets.md`](adr/0015-service-owns-harness-credential-secrets.md) | ADR 0015: On Kubernetes the service owns the harness credential Secrets, and the UI logs the harnesses in. Status: accepted. |
| [`0016-first-run-token-never-logged.md`](adr/0016-first-run-token-never-logged.md) | ADR 0016: The first-run administrator token is delivered to a Secret or a private file, never to a log. Status: accepted. |
| [`0017-service-owns-the-github-app-credential.md`](adr/0017-service-owns-the-github-app-credential.md) | ADR 0017: The service owns the GitHub App credential, and the UI connects the App. Status: accepted. |
| [`0018-per-harness-image-promotion.md`](adr/0018-per-harness-image-promotion.md) | ADR 0018: Each harness has its own default worker image. Status: accepted. |
| [`0019-private-checkout-through-the-github-app.md`](adr/0019-private-checkout-through-the-github-app.md) | ADR 0019: A private repository is cloned with a read-only GitHub App token. Status: accepted. |
| [`0020-project-toolchain-in-the-worker-image.md`](adr/0020-project-toolchain-in-the-worker-image.md) | ADR 0020: A project's check toolchain rides in the worker image; its dependencies come from PyPI at run time. Status: accepted. |
| [`0021-enabling-a-harness-is-an-administrators-decision.md`](adr/0021-enabling-a-harness-is-an-administrators-decision.md) | ADR 0021: Enabling a harness is an administrator's decision; configuration is the default. Status: accepted. |
| [`0022-kubernetes-publisher.md`](adr/0022-kubernetes-publisher.md) | ADR 0022: On Kubernetes, the publisher is a Job and its token a per-push Secret. Status: accepted. |
| [`0024-review-is-the-enforcement.md`](adr/0024-review-is-the-enforcement.md) | ADR 0024: The review is the enforcement; paperwork gates are advisory. Status: accepted. |
| [`0025-the-delivery-half-always-has-a-way-out.md`](adr/0025-the-delivery-half-always-has-a-way-out.md) | ADR 0025: The delivery half always has a way out. Status: accepted. |
| [`0028-hermes-first-routing.md`](adr/0028-hermes-first-routing.md) | ADR 0028: Hermes first in routing; frontier by intent; demotion that recovers. Status: accepted. |
| [`0029-discard-an-import-and-rename-a-principal.md`](adr/0029-discard-an-import-and-rename-a-principal.md) | ADR 0029: Discard a verified import, skip native tasks, rename a principal. Status: accepted. |
| [`0030-ui-sessions-server-side.md`](adr/0030-ui-sessions-server-side.md) | ADR 0030: UI sessions are server-side. Status: accepted. |

**History** -- Phase notes, the closest thing to a changelog, and spikes.

| File | Purpose |
| --- | --- |
| [`FDY-0149-codex-local.md`](implementation-notes/FDY-0149-codex-local.md) | FDY-0149: local Codex, issue #249 |
| [`advisory-gates.md`](implementation-notes/advisory-gates.md) | Advisory gates (FDY-0138, ADR 0024) |
| [`c1.md`](implementation-notes/c1.md) | C1 implementation notes: walking skeleton |
| [`c10.md`](implementation-notes/c10.md) | C10: authenticated lab-local gateway |
| [`c11.md`](implementation-notes/c11.md) | C11: one worker image, built by CI and published by the release |
| [`c12.md`](implementation-notes/c12.md) | C12: one `crucible` command with an envelope an agent can act on |
| [`c14.md`](implementation-notes/c14.md) | C14: the worker image publish works against GHCR, proved on GHCR (FDY-0090) |
| [`c2.md`](implementation-notes/c2.md) | C2 implementation notes: gates, claims, review, acceptance, corrections, wakes |
| [`c3.md`](implementation-notes/c3.md) | C3 implementation notes: the Docker provider and the worker images |
| [`c4.md`](implementation-notes/c4.md) | C4 implementation notes: GitHub delivery |
| [`c5.md`](implementation-notes/c5.md) | C5 implementation notes: harness adapters live |
| [`c6.md`](implementation-notes/c6.md) | C6 implementation notes: bootstrap import and readiness |
| [`c6b.md`](implementation-notes/c6b.md) | C6b implementation notes: class routing and reactive quota reroute |
| [`c6c.md`](implementation-notes/c6c.md) | C6c: readiness gaps that were code |
| [`c6d.md`](implementation-notes/c6d.md) | C6d: Hermes on the DGX Spark local pool |
| [`c7a.md`](implementation-notes/c7a.md) | C7a: administrative UI |
| [`c7b.md`](implementation-notes/c7b.md) | C7b: readable administration panels |
| [`c7c.md`](implementation-notes/c7c.md) | C7c: the image manifest cannot drift from its sources |
| [`c7d.md`](implementation-notes/c7d.md) | C7d implementation notes |
| [`c7e.md`](implementation-notes/c7e.md) | C7e: release candidate image classification |
| [`c7f.md`](implementation-notes/c7f.md) | C7f: release boot follows the shared Compose path |
| [`c7g.md`](implementation-notes/c7g.md) | C7g: one identity shim |
| [`c8a.md`](implementation-notes/c8a.md) | C8a: the Kubernetes execution provider |
| [`c8b.md`](implementation-notes/c8b.md) | C8b: the Kubernetes end-to-end tier on kind |
| [`c9.md`](implementation-notes/c9.md) | C9: cluster deployment manifests, proven on kind |
| [`command-timeout.md`](implementation-notes/command-timeout.md) | Command timeout from the launch (issue 128, FDY-0122) |
| [`crane-registry.md`](implementation-notes/crane-registry.md) | Worker images resolve through crane (108) |
| [`delivery-no-stall.md`](implementation-notes/delivery-no-stall.md) | The first real pull request does not get stuck (FDY-0139) |
| [`deploy-local.md`](implementation-notes/deploy-local.md) | Running a release locally: the deployment directory |
| [`harness-logins.md`](implementation-notes/harness-logins.md) | Harness logins on Kubernetes, and enabling a harness from the UI (hades #173, #174) |
| [`k8s-provider-defects.md`](implementation-notes/k8s-provider-defects.md) | Kubernetes provider defects (FDY-0121) |
| [`lab-stays-up.md`](implementation-notes/lab-stays-up.md) | The lab keeps running (2026-09-29) |
| [`lab-test-findings.md`](implementation-notes/lab-test-findings.md) | Lab test findings on v0.6.3 (hades #187, #189, #190, #191, FDY-0129) |
| [`one-click-github-app.md`](implementation-notes/one-click-github-app.md) | The one-click GitHub App (crucible#168, FDY-0127) |
| [`operator-ui.md`](implementation-notes/operator-ui.md) | Operator UI: per-harness images, optional reasons, harness test, density (FDY-0120) |
| [`private-checkout.md`](implementation-notes/private-checkout.md) | Private repository checkout (crucible#157, FDY-0124) |
| [`release.md`](implementation-notes/release.md) | Release: the tag is the trigger |
| [`self-hosting-worker.md`](implementation-notes/self-hosting-worker.md) | Hades works on its own repository from a worker (hades #184, part of #85, FDY-0131) |
| [`trailer-not-required.md`](implementation-notes/trailer-not-required.md) | The commit trailer is not required; the task record is the paper trail (FDY-0143) |
| [`worker-survival.md`](implementation-notes/worker-survival.md) | Workers do not die or lose work, and are told less (FDY-0140) |
| [`S1.md`](history/spikes/S1.md) | S1: Does each harness's subscription auth work from a mounted config directory inside a non-root container? |
| [`S1b.md`](history/spikes/S1b.md) | S1b: Do Crucible's dedicated harness credential sessions leave the operator's daily sessions valid? |
| [`S2.md`](history/spikes/S2.md) | S2: Can Codex's own sandbox run inside the container as defense in depth? |
| [`S3.md`](history/spikes/S3.md) | S3: Does AGY operate headless with `--add-dir` and a pointer prompt, and what is its real argv ceiling? |
| [`S4.md`](history/spikes/S4.md) | S4: With harness sandboxes disabled, does the container hardening hold? |
| [`S5.md`](history/spikes/S5.md) | S5: Is exit-code and report-file detection reliable across the three harnesses? |
| [`S6.md`](history/spikes/S6.md) | S6: Which egress endpoints does each harness need? |
| [`S7.md`](history/spikes/S7.md) | S7: Can harness versions be pinned and updated predictably in images? |
| [`S8.md`](history/spikes/S8.md) | S8: Does a running worker survive a supervisor restart? |
| [`S9.md`](history/spikes/S9.md) | S9: dedicated rootless Docker daemon for Crucible |
| [`S10.md`](history/spikes/S10.md) | S10: App installation token, publisher container, push and tag without the token leaving tmpfs |
| [`S11.md`](history/spikes/S11.md) | S11: Can each harness be prevented from self-updating inside the container, and does it report its version reliably? |
| [`S12.md`](history/spikes/S12.md) | S12: what the external reviewer emits on a PR, under which login, and how it is triggered |
| [`S14.md`](history/spikes/S14.md) | S14: Can Crucible observe current subscription quota state per harness, and use it for routing and for a wait-and-resume strategy? |
| [`S15.md`](history/spikes/S15.md) | S15: quota-axi validated across all three providers |
| [`S16.md`](history/spikes/S16.md) | S16: Which harness should front the DGX Spark? |
| [`README.md`](history/spikes/README.md) | Spike results (Phase C0) |

**Readiness** -- Readiness evidence as of phase C7a, 2026-09-21.

| File | Purpose |
| --- | --- |
| [`readiness.md`](readiness.md) | Crucible readiness report (19) |
