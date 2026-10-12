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
3. Checkout preparation is an init container of the worker Pod. It clones from the
   read-only reference cache when configured and otherwise from the repository. Thus
   the agent consumes the checkout made in its own Pod.
4. The collector is a second container in the worker Pod. The agent wrapper always
   releases it on exit, including the termination signal used after the existing grace
   window. The collector shares only that Pod's `ws` volume, records its result, and
   remains alive long enough for the provider to read the bounded outputs over exec.
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
worker Pod is removed. An abrupt Pod or node loss cannot be collected and is retried as
lost, exactly as a lost claim was before this decision.
