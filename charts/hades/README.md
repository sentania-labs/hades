# Hades chart

The chart deploys Hades and its Kubernetes execution provider. Attempt workspaces are
Pod-local scratch, not PersistentVolumeClaims created by Hades.

`provider.workspaceEphemeral` controls every role Pod's scratch:

- `size` is the volume bound.
- An empty `class` uses `emptyDir` with `sizeLimit` and consumes node ephemeral storage.
- A named `class` uses a generic ephemeral volume with a claim template owned by the
  Pod. Kubernetes creates and removes that claim with the Pod.

The artifact-root and reference-cache claims in `storage` remain persistent and are not
affected by this setting. No role Pod mounts another role Pod's scratch.
