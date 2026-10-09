# ADR 0015: On Kubernetes the service owns the harness credential Secrets, and the UI logs the harnesses in

Status: accepted. The operator's decisions of 2026-09-23 (quoted below), made concrete
by FDY-0112 on 2026-09-24.

## Context

On Kubernetes each harness credential is one Secret in `crucible-workers` (26). Until
this change the deployment delivered those Secrets through GitOps as SealedSecrets,
`docs/deployment.md` said the `/ui` login filled them, and no login existed on this
provider: `probe_credential` refused with "not part of this provider yet (C8a)"
(crucible#92). The only way to get a Claude Code, Codex or AGY credential onto a cluster
was to hand-seal the files.

Two writers already existed for the same object. GitOps applied the sealed value, and the
supervisor patched a newer refreshed token back into it after an attempt (12, the
`rw-narrow` sync-back). Codex and AGY rotate their refresh token on their own during a
run (S1), so a GitOps sync after a rotation puts back a token the provider has already
revoked, and the next worker is locked out. The same Secret with two writers drifts by
construction.

The operator, 2026-09-23: "the UI needs to be able to support logging into the
harnesses." Hand-sealing harness credentials is not an accepted substitute. And: "You
should not be concerned with deployment. You are building an app/seevice." The service
has to carry the whole flow itself; the deployment only has to give it room to.

## Decision

1. **The service is the single owner of the harness credential Secrets.** It creates a
   harness's Secret the first time something writes it, labels it
   `app.kubernetes.io/managed-by: crucible` and `crucible.credential: <harness>`, and is
   its only writer: the login, the Hermes key entry, and the sync-back of a refreshed
   token. A write replaces the Secret's data whole with one merge patch, so it never
   mixes two sessions. GitOps no longer delivers these Secrets; the database, GitHub App
   and TLS Secrets stay with GitOps because nothing inside Crucible writes them.
2. **The login is a Job.** The api runs the harness's own login CLI in the promoted
   worker image as a Job in `crucible-workers`: no workspace, no credential mounted, a
   memory-backed home, and a NetworkPolicy for that harness's declared login endpoints
   only, never its model API (crucible#58). The operator sees the device or browser URL
   and the device code, read from the Pod log; a driver inside the Pod keeps the one-time
   token Claude Code prints, and the code the operator pastes, out of that log. A pasted
   code goes in over exec stdin. The auth files come back over exec, never through a
   log. They are written to the Secret only after the CLI has exited and they pass the
   shape check, so a login that is cancelled, times out, or whose files fail the check
   leaves the credential exactly as it was, and there is no retired copy to keep and
   shred as there is on Docker. Files that pass are stored whatever the CLI's exit
   code, as a Docker login leaves what the CLI wrote. The Pod's terminal is 4096
   columns wide so the CLI never wraps a token across the line the driver masks.
3. **The probe runs on the cluster.** `probe_credential` is the attempt path cut down to
   one prompt: a claim, a two-file identity ConfigMap, the per-run copy of the Secret,
   the worker Job under the worker's egress and the namespace readiness gate, the
   sync-back, and removal of everything. It is labelled `crucible.admin=probe`, so the
   supervisor's retention sweep and reconcile leave it alone while the api runs it.
4. **A login and an attempt of one harness never overlap** (12's rotation rules). A
   login refuses while an attempt or a credential probe holds that harness's
   credential, and checks again at the moment it writes: if one came to hold it while
   the login ran, the new files are not stored and the login says so. A launch waits,
   deferred rather than failed, while a login Job for its harness exists (the
   supervisor reads that before each launch), and a probe refuses while one exists.
   The login Job is the lock: it is the one object every process can see.

## Alternatives considered

- **Keep GitOps as the writer and have the UI produce a sealed file for the operator to
  commit.** Rejected by the operator's words: the UI logs the harness in, and a sealed
  file is hand-sealing with extra steps. It also leaves the sync-back drift in place.
- **Let GitOps create the Secret empty and the service fill its data.** Still two
  writers of one object: a sync that prunes or re-applies it removes the login. Argo can
  be told to ignore `data`, but that is a deployment-side exception every deployment
  would have to know to make.
- **Attach to the login Pod's terminal (`pods/attach`).** Needs a new verb, and the
  attach stream is exactly what the kubelet also writes to the node's log, so the
  Claude Code token would land on the node's disk (12). The in-Pod driver plus exec
  keeps the Role unchanged and the token off the node.

## Consequences

- The supervisor's Role is unchanged: `create`, `get` and `patch` on Secrets, Jobs,
  NetworkPolicies, `pods/log` and `pods/exec` in `crucible-workers` were already granted,
  and the api already runs under that account. `update` stays absent.
- A deployment that sealed harness Secrets before this change removes them from its
  GitOps repository. Argo prunes an object it stops tracking, which would delete the
  credential; either take them out of tracking without pruning, or log each harness in
  again. On its first write the service takes over a Secret that already exists.
- The Secret name defaults to `hades-harness-<harness>` (`crucible-harness-<harness>`
  before hades #609 step 2) with `_` as `-`, a valid Kubernetes name; `CRUCIBLE_KUBERNETES__CREDENTIAL_SECRETS` may name them. An optional
  credential (Hermes) is mounted when its Secret exists, without needing a mapping.
- Rotate and remove move and shred directories and are refused where the credential is
  a Secret; a login with replace is how a Secret-held credential is replaced.
- Each adapter now declares `login_endpoints`. They were read from the pinned CLIs'
  strings, not observed on a live login (FDY-0112 used no real harness login). AGY's
  login command ends with a prompt to its model API, which the login Job cannot reach,
  so its CLI exits non-zero on Kubernetes; the token it wrote is still stored when it
  passes the shape check, and validate decides whether it works.
