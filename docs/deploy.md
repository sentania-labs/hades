# Deploy Hades

Hades supports three deployment paths. Compose is the local and single-host path,
kustomize is the cluster source of truth, and the Helm chart is the packaged cluster
interface. The chart is derived from and checked against `deploy/kubernetes/base` for
now. Change the kustomize base first, then update `charts/hades` in the same commit.

## Compose

Copy `.env.example` to `.env`, fill in the named credentials, and run:

```console
make up
```

For host-based service development with only PostgreSQL and the proxies in containers,
run `make dev`. See [deployment.md](deployment.md) for the production host runbook.

## Kustomize

Render and validate the source manifests with `make manifests`. Apply a suitable
overlay, for example:

```console
kubectl apply -k deploy/kubernetes/overlays/lab
```

The overlay must pin images, storage classes, ingress, cluster settings, and names of
pre-provisioned Secrets. It must not contain secret values.

## Helm

Review [values-lab-example.yaml](../charts/hades/values-lab-example.yaml), replace its
example host and deployment pins, and install from the checkout:

```console
helm upgrade --install hades charts/hades \
  --values charts/hades/values-lab-example.yaml
```

For a released OCI chart:

```console
helm upgrade --install hades \
  oci://ghcr.io/sentania-labs/charts/hades \
  --version VERSION \
  --values values-lab.yaml
```

The chart accepts service and worker repository, tag, and digest pins; bundled or
external PostgreSQL through a Secret name; ingress class, host, and TLS Secret name;
PVC storage classes and sizes; worker egress names and quota; GitHub App and first-run
Secret names; and room runner concurrency, workspace, and timeout settings. It never
accepts or creates secret values. Create the named Kubernetes Secrets separately.

Run `make chart` to lint and validate the default and lab-like renders. Run
`make chart-sync-check` to compare chart objects with the kustomize base. Both targets
name any missing command directly.
