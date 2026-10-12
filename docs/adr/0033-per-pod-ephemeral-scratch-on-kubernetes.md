# ADR 0033: Per-pod ephemeral scratch on Kubernetes

Status: accepted. FDY-0633, 2026-10-12, following hades #632, #629 and #630.
Completes ADR 0032 and changes the Kubernetes parts of 08, 16 and 26.

## Context

ADR 0032 moved the sealed work bundle into the artifact store. Publication and a
correction no longer need the attempt workspace. The remaining PersistentVolumeClaim
only passed a checkout between short-lived role Pods. It required claim retention,
cleaner Pods, keep-policy handling and collision recovery when a deleted claim with the
next attempt's name was still terminating.

A Pod-local volume removes those lifecycle edges, but a collector in a later Pod cannot
read it. Collection must finish while the worker Pod and its checkout still exist.

## Decision

1. Every Kubernetes role Pod gets a `ws` scratch volume of its own. With no class it is
   an `emptyDir` with `sizeLimit`. With a configured class it is a generic ephemeral
   volume with the same requested size. `provider.workspaceEphemeral` is the chart's
   one workspace setting and contains `class` and `size`.
2. No attempt-scoped PersistentVolumeClaim is created. The artifact and reference-cache
   claims are unchanged. No Pod inherits another Pod's files.
3. Checkout preparation is an init container of the worker Pod, `workspace-prepare`.
   It runs the preparer script the `prepare-<attempt>` Job already ran, cloning from
   the repository with the read-only reference cache as an object source when one is
   configured. Thus the agent consumes the checkout made in its own Pod; the
   preparer Job's scratch only proves the checkout can be built and goes with its Pod.
   Harness credential init containers follow the checkout's, never replace it.
   - A correction's sealed bundle is delivered from the artifact store into this init
     container over exec, exactly as into the preparer Job (ADR 0032), so the worker
     resumes from the bundle and not from the remote work branch.
   - The clone reaches the remote, so the worker Pod gets a second NetworkPolicy,
     `np-checkout-<attempt>`, with the preparer's git destinations. A NetworkPolicy
     selects Pods, not containers; a separate object keeps the worker's own policy
     exactly its allowlist (hades #425), and it is deleted as soon as the Pod leaves
     `Pending`, which happens only once its init containers have run.
   - A private repository's `checkout-<attempt>` Secret (ADR 0019) is mounted into
     this init container and into no other container of the Pod. Whether it is
     mounted is read from the cluster at launch, not from process memory, so a
     supervisor restarted between prepare and launch still mounts it. It is deleted
     together with the checkout policy, and by `discard` and `cleanup` otherwise.
4. The collector is a second container in the worker Pod. It is the in-Pod form of the
   collector role, not a phase of the agent's entrypoint, so the role split is kept:
   the agent's container never runs Crucible's collection code. The collector waits
   for the provider rather than for the agent: once `collect` has seen the agent's
   container end and classified its exit, it writes `quota` or `normal` to
   `.collect-mode` in the scratch, so a quota checkpoint (16) is committed as `wip`
   or refused as one before anything is packaged. This happens on every exit the Pod
   survives, including a supervised timeout: drain signals the agent's processes and
   leaves the collector running, and the worker Job's deadline leaves the collector
   timeout beyond the drain grace. The collector shares only that Pod's `ws` volume,
   records its result, and remains alive long enough for the provider to read the
   bounded outputs over exec.
5. Bundle verification and verification remain isolated phases. A later Pod must clone
   from the reference cache or receive the sealed bundle from the artifact store. The
   publisher remains the separate Job defined by ADR 0032 and receives its bundle from
   the artifact store.
6. Kubernetes ignores `keep` and `keep_diff_only` workspace policies. Pod deletion is
   workspace deletion. There is no cleaner role, retained claim, pre-launch claim
   cleanup, workspace retention event, or terminating claim name to collide with.
   Docker keeps its host-path workspace behavior.
7. If the worker Pod disappears before collection, its scratch disappears too. OOM,
   eviction and node loss therefore follow the existing lost-attempt classification
   and retry rule. A missing Pod is never reconstructed from another Pod's files.

## Consequences

The cluster no longer needs ReadWriteMany storage for attempt workspaces and attempt
count no longer consumes persistent claim quota. Generic ephemeral storage can still
consume storage capacity selected by its class. `emptyDir` consumes node ephemeral
storage. Operators size either form with the same `size` value.

Collection survives normal exits and supervised timeouts because it runs before the
worker Pod is removed. Collection now needs the supervisor: a collector that is never
released (a supervisor down past the Job's deadline) ends with its Pod, and the attempt
is retried as lost.

Between the end of the init containers and the provider's next look at the Pod, the
agent can reach the git destinations through the checkout policy. It holds no git
credential and its checkout's `origin` is a placeholder, so what it gets is read-only
in effect, as for a policy that allowlists GitHub. A token-bearing clone needs a token
that is still valid when the worker Pod starts; an installation token outlives a
normal launch, but a launch that waited for quota room longer than the token lives
fails its checkout and is reported as a failed init container. An abrupt Pod or node loss cannot be collected and is retried as
lost, exactly as a lost claim was before this decision.
