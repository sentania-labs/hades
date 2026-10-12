# ADR 0016: The first-run administrator token is delivered to a Secret or a private file, never to a log

Status: accepted. Decided by Foundry on 2026-09-25 under the operator's go of that day
("Go on everything", 12:49 PM), for crucible#122.

## Context

A fresh database has no administrator, so the migration mints one, `first-run-admin`,
and has to hand its token to the operator exactly once. Until this change it printed the
token to stderr in a framed block, and `docs/deployment.md` told the operator to read it
from the migrate Job's log. On Kubernetes a pod log is collected and shipped by the
cluster's log pipeline, so on the v0.5.5 lab deployment the token was copied to Loki and
had to be revoked (audit seq 4, 2026-09-24 5:02 PM). Any deployment with log shipping
leaks a full administrator credential that way. The sign-in page also sent the operator
to `docker compose logs migrate`, which is wrong on Kubernetes.

## Decision

1. **The token never reaches stdout, stderr or a log, on any provider.** The migration
   writes it to one private place and its stderr names only that place.
2. **Kubernetes: a Secret in the service namespace.** `crucible-first-run-admin`, key
   `token`, in the namespace the api runs in (`kubernetes.namespace`, `crucible`). Since
   hades #609 step 2 the defaults are `hades-first-run-admin`, `hades` and `hades-migrate`. The
   migrate Job runs under its own ServiceAccount, `crucible-migrate`, which may create
   Secrets there and patch that one (to replace a token a previous run left, when the
   database was reset). The api's account may delete that one Secret and nothing else.
   No Crucible account can read it: reading it is the operator's `kubectl get secret`.
   The names and grants are in `deploy/kubernetes/base/crucible/first-run-rbac.yaml`.
3. **Docker: a mode 0600 file in the credential root.** `first-run-admin-token` under
   `docker.credential_root`, which on compose is the credential volume only the service
   container and the migration mount. It is written to a temporary name and renamed.
4. **Delivered before it is committed.** The migration writes the token before it
   commits the principal, so a token it could not deliver is never minted and the Job
   fails with the reason; the next run tries again. With neither place configured (a
   host-mode developer database), it mints nothing and says how to create an
   administrator with `crucible admin --reason "<why>" token create`, which needs the
   supervisor running like every administrative mutation.
5. **Removed after first use.** The api deletes the Secret or file when the first-run
   principal first signs in at `/ui`, and when that principal is revoked. A removal that
   fails is logged, without the value, and never fails the sign-in or the revoke.
   Principal names starting `first-run-admin` are reserved for the migration, so that
   prefix identifies the principal exactly.
6. **The sign-in page names the place for the running deployment**: the Secret and the
   `kubectl` command on Kubernetes, the file and the `docker compose exec` command on
   Docker.

## Alternatives considered

- **A one-time claim flow in the UI bound to something only the deployer holds.** It
  needs a second secret the deployer already has, which moves the same delivery problem
  one step back, and a new unauthenticated endpoint.
- **Keep printing, but tell operators to exclude the Job from log shipping.** A
  deployment-side exception every deployment would have to know to make, and a missed
  one is a leaked administrator.
- **Let the api read the Secret and show the token once in the browser.** The api would
  need `get` on a Secret holding an administrator credential, and anyone reaching the
  page first would take it.

## Consequences

- The migrate Job now mounts a ServiceAccount token (it had none). Its account can
  create Secrets in `crucible`, because Kubernetes cannot narrow `create` by name,
  including a `service-account-token` Secret the token controller would fill (it cannot
  read one back). It is used by that Job alone, whose command is fixed in the image. A
  ValidatingAdmissionPolicy could narrow the create to the one name on clusters that
  want it.
- The api and supervisor share `crucible-supervisor`, so the supervisor can also delete
  that one Secret. That is the whole of the new grant to it.
- An operator who uses the token only through the CLI and never signs in at `/ui` leaves
  the Secret or file in place until the principal is revoked. Revoking it after creating
  a named administrator is the tidy end state.
- The compose migration now mounts the credential volume and waits for
  `credential-init`, which gives the volume root to the service user.
