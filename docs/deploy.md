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

The chart accepts object name and namespace overrides that also reach the settings
ConfigMap; service and worker repository, tag, and digest pins (a tag other than
`latest` needs a digest); bundled or external PostgreSQL through a Secret name, with
its user and database; ingress class, host, and TLS Secret name; a storage class and
size per claim; the cluster's DNS, local endpoint, time zone, PID limit and pull Secret;
the attempt provider's workspace, concurrency, and timeout settings under `provider`;
a `buildkit.enabled` toggle, off by default; worker egress names and quota; GitHub App
and first-run Secret names; room runner settings; and an `extraSettings` map for any
other `CRUCIBLE_*` setting. It never accepts or creates secret values. Create the
named Kubernetes Secrets separately. [deployment.md](deployment.md#the-helm-chart)
lists every value against the overlay placeholder it replaces.

Run `make chart` to lint and validate the default, lab-like and override renders and
check that the names and settings follow the values. Run `make chart-sync-check` to
compare chart objects, rendered with `tools/chart/values-base.yaml`, with the kustomize
base. Both targets
name any missing command directly. Component manifests live directly in
`charts/hades/templates/`; each document starts with a standalone `---` line.
The structure unit test guards this layout, and the CI chart job supplies the real
Helm lint, render, schema validation, and kustomize comparison proof.

Helm names the migration Job `crucible-migrate-REVISION` (the prefix follows `nameOverride`), creating a fresh Job on
install and each upgrade, including image tag or digest changes. Helm removes the
previous revision's Job as an obsolete release resource. This is a regular Job so
its bundled database, ConfigMap and RBAC can be installed together. The services
refuse to start until the schema is at head. Use `--wait --wait-for-jobs` to wait
for migrations and readiness; size `--timeout` for the migration workload.
The sync check normalizes only this Job's revision suffix when comparing names.

`secrets.githubApp` names the application-managed GitHub App Secret and is passed
to the application and RBAC. Optional `secrets.githubAppPrivateKey` and
`secrets.githubAppWebhook` names mount the `app.pem` and `webhook.secret` keys from
separate operator-managed Secrets. An empty name inherits `secrets.githubApp`.
The GitHub setup UI continues to manage only `secrets.githubApp`; operators maintain
any separate credential Secrets themselves. `secrets.firstRunToken` configures both
RBAC and `CRUCIBLE_KUBERNETES__FIRST_RUN_SECRET_NAME`, so migration delivery and
API cleanup use the same Secret, including after a database reset.
