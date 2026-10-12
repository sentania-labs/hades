# Hades chart

The chart deploys Hades and its Kubernetes execution provider. Attempt workspaces are
Pod-local scratch, not PersistentVolumeClaims created by Hades.

`provider.workspaceEphemeral.size` is the one deploy-time workspace setting. It bounds
every role Pod's `emptyDir`, which consumes node ephemeral storage and is removed with
the Pod.

The artifact-root and reference-cache claims in `storage` remain persistent and are not
affected by this setting. No role Pod mounts another role Pod's scratch.
