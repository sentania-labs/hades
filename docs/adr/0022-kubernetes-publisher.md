# ADR 0022: On Kubernetes, the publisher is a Job and its token a per-push Secret

Status: accepted. FDY-0133, 2026-09-28, on the operator's go for that batch after the lab
found that nothing had ever been published from a Kubernetes deployment (practice task
HT-0007 sat in `publishing` with no publisher and no log line). Extends ADR 0007 and 23
"Publication" to the Kubernetes provider (26); changes nothing about the Docker one.
Point 5 and point 8 are replaced by ADR 0032 (2026-10-12): the bundle is read from the
artifact store and delivered into the publisher Pod over exec, the Pod mounts no claim,
and the attempt's workspace path picks the publisher.

## Context

The publisher is the one process that holds a GitHub credential that can write. With the
Docker provider it is a hardened throwaway container: the token arrives on stdin onto a
tmpfs, the bundle is copied into a directory of its own on the host, the outcome is a
directory Crucible reads back, and the network is the publisher's own (23, S10).

`wire()` built only that one. On a Kubernetes deployment the publisher was `None` and
the delivery step returned without a word on every tick, so a task accepted there stayed
in `publishing` for good. A cluster has no stdin to write to, no host directory to stage
in, and no Docker network, so the Docker publisher cannot simply be reused.

## Decision

1. **Same port, same script, same outcome rules; only the carriers differ.** The
   Kubernetes publisher implements `Publisher` with the script `publisher_script` renders
   for both providers, and both turn its output files into a `PublishOutcome` with one
   function (`outcome_from_files`). A change to what a publication checks is made once.
2. **One Job per push**, `publish-<attempt>`, role `publisher`, the pod shape of every
   role (26), `backoffLimit: 0`, deadline `github.publisher_timeout_seconds` (default
   600), the same limit the Docker publisher's container gets.
3. **The token is a Secret created for that push alone**, `publish-token-<attempt>`,
   mounted read-only (mode 0400) as a Secret volume, which the kubelet keeps in memory,
   at the path the script reads. It is never in an env var, the command, the Job, the
   Pod spec, a NetworkPolicy, a log, an event, or the database. It is deleted as soon as
   the Job's Pod is gone, on every path (failure and cancel included), and a stale one
   of the same name is deleted before the next push creates its own.
4. **Before it pushes, the script asks the credential helper for the token** (`git
   credential fill` for https on `github.credential_host`) and refuses (exit 3) unless a
   password comes back; only that answer is recorded, never the value. On Kubernetes
   this is what proves the Secret volume is readable by the Pod's uid through the very
   path the push uses; the Docker publisher runs the same check.
5. **The bundle is read where the collector left it.** The Pod mounts
   `output/work_branch.bundle` off the attempt's workspace claim as one read-only file.
   Nothing else of the claim is visible to it except its own `publish/` leaf for the
   outcome. The publish request's bundle path must be exactly
   `k8s://<namespace>/ws-<attempt>/output/work_branch.bundle`; any other path is refused
   before anything is created. Because Crucible cannot hash a file on a claim it never
   mounts, the script hashes the bundle inside the Pod against the collector's seal
   before any remote is contacted (the Docker publisher now runs the same check, after
   its own host-side one).
   Neither subPath is left for the kubelet to create. A subPath that does not exist is
   made by the kubelet as root when the Pod starts, and on an NFS claim that directory
   may be unwritable by the worker uid or refused, which fails the Pod's setup with
   nothing in its log. Before the Secret exists, a short Job in the preparer role (the
   role that makes the claim's other leaves), running as the worker uid with the whole
   claim mounted and no network, checks that the bundle is a regular file and creates
   `publish/` with the owner and mode `output/` has. A missing bundle is refused at
   `bundle-seal`, a `publish/` leaf the worker uid cannot own and write at
   `publish-leaf`, both by name and before any token is placed. The check runs on every
   push rather than at collection, so a claim collected before this shipped (HT-0007's)
   is made ready the same way. Added 2026-09-28 on the review of hades PR 225.
6. **The outcome comes back through the reader Pod over exec**, as every other file an
   attempt's Pod writes does (26), never through a Pod log (12). The script exits 0 only
   after its push succeeded, so a Job that exited 0 whose outcome files cannot be read
   back is reported as pushed at the expected head, and Crucible's own remote-head check
   decides; a Pod that is slow to be removed after its Job is logged, not turned into a
   failed publication.
7. **Egress is the publisher's own NetworkPolicy** (`github.com`, `api.github.com`, and
   `github.credential_host` when it is another host), resolved and pinned into the
   Pod's `hostAliases` (hades #191). A namespace whose egress enforcement the readiness
   probe has not proven gets no publisher Pod and no Secret.
8. **With both providers wired, the bundle path picks the publisher**: a `k8s://` path is
   the Kubernetes publisher's, anything else the Docker one's.
9. **A publication that cannot start says so** (23): a `task_publish_pending` event with
   the reason, a warning logged once per task, `GET /supervisor`'s
   `github.publishing_waiting`, the admin UI's task list, and an escalation after
   `github.publisher_timeout_seconds`. No new setting: the wait is the publisher's own
   time limit.

## Consequences

- A task already waiting in `publishing` when this ships is published on the first tick
  after the upgrade, with no manual step: the delivery step lists every task in that
  state, as it always did, and now has a publisher to hand it to.
- The publisher needs the attempt's workspace claim to still exist. The default cleanup
  (`keep_diff_only` on success) keeps the bundle; a policy that deletes the workspace on
  success leaves nothing to publish, and the publication fails at `bundle-seal` saying
  the claim is gone, as the Docker publisher fails when the bundle file is gone.
- The publisher Pod asks for the policy's worker resources, as the Docker publisher's
  container does. A small cluster at capacity may take longer to schedule it.
- No new RBAC: create, get, list and delete on Jobs, Pods, Secrets, NetworkPolicies and
  PersistentVolumeClaims, and `pods/exec`, are what the workers-namespace Role already
  grants.
