# Secret shapes

Crucible's repository holds no secret value and no sealed ciphertext. What it holds is
the *shape* of every Secret the deployment reads, so lab-admin can seal or source the
real values without guessing a key name.

Four kinds of secret, and where each comes from. Two are delivered by GitOps. The
harness credential Secrets and the GitHub App Secret are not: Crucible creates and owns
them (ADR 0015, ADR 0017).

| Object | Namespace | Keys | Read by |
|---|---|---|---|
| `hades-database` | `hades` | `password`, `url` | the PostgreSQL StatefulSet (`password`) and the api, supervisor and migration Job (`url`) |
| `hades-github-app` | `hades` | `app-id`, `app.pem`, `webhook.secret` | written only by Crucible (Create GitHub App on the GitHub page); read by the api and supervisor through the API server, and mounted on those pods only for the webhook secret (12). **Never delivered by GitOps.** |
| `hades-harness-<harness>` | `hades-workers` | one per auth file the adapter declares | written only by Crucible (the login, the Hermes key, the sync-back); copied per attempt into `cred-<attempt>` (12, 26). **Never delivered by GitOps.** |
| an image pull secret | both | `.dockerconfigjson` | the kubelet, when the packages are private |

`password` and `url` must agree: `url` is the whole DSN
(`postgresql+psycopg://crucible:<password>@hades-postgres:5432/crucible`) because
pydantic-settings reads one environment variable and does no interpolation.

A harness whose auth file sits in a subdirectory is keyed with the separator replaced by
an underscore, and the volume projects it back to the declared path (C8a). AGY's
`antigravity-cli/antigravity-oauth-token` is therefore the key
`antigravity-cli_antigravity-oauth-token`.

## The harness Secrets are Crucible's

`service-owned-harnesses.yaml` documents their layout and is in no kustomization. The
service creates each one the first time the admin login (or, for Hermes, the key entry
on the Local gateway page) writes it, labels it `app.kubernetes.io/managed-by: crucible`, and
replaces its data whole on each later write; the supervisor writes a newer refreshed
token back after an attempt. A Secret written by both Crucible and GitOps drifts, and a
sync would restore a token the harness has already rotated, so none of these is sealed,
sourced or applied by GitOps. The supervisor's Role already carries the verbs this needs
(`create`, `get` and `patch` on Secrets in `hades-workers`).

A deployment that delivered them through GitOps before this change removes them from
its GitOps repository first. Argo's prune deletes a Secret it stops tracking, which
would delete the credential, so either take the objects out of Argo's tracking without
pruning them, or accept that each harness is logged in again from the UI afterwards. On
its first write Crucible takes over a Secret that already exists: it sets its labels and
replaces its data.

## The GitHub App Secret is Crucible's

The operator presses Create GitHub App on the GitHub page. GitHub makes the App and its
key and hands them to Crucible once, which then creates `hades-github-app` in
`hades`, labelled `app.kubernetes.io/managed-by: crucible`, and replaces its data when
the App is replaced. The control plane's Role in `hades`
(`../base/crucible/github-app-rbac.yaml`) allows exactly that: `get` and `patch` on this
one Secret and `create`. A deployment that sealed it before this change removes it from
its GitOps repository without pruning it, as above, or creates the App again.

## `sealed/`

SealedSecret placeholders, one per object, carrying `REPLACE_WITH_SEALED_*` in place of
every ciphertext. They are real resources of the lab overlay, so a `kubectl kustomize`
of that overlay shows lab-admin exactly which objects have to exist. They are
deliberately not valid ciphertext: the sealed-secrets controller refuses them and no
workload starts with a wrong value. Sealing is `kubeseal` against the cluster's own key,
and the result belongs in lab-admin's GitOps repository, never here.

Automated sync is off in `argocd/application.yaml` for exactly this reason: the first
sync is a person looking at what is about to be created.

## ExternalSecret instead

A cluster running external-secrets replaces `sealed/` with `external/` in the overlay's
resources. The key names above are the same either way; only where the value comes from
changes. `external/` carries one worked example.

## Plain Secrets, for kind only

`make deploy-kind` generates plain Secrets from throwaway values with kustomize's
`secretGenerator`, in `../overlays/kind`. That overlay exists to prove the manifests on a
disposable cluster and is never applied anywhere else. There is no plain-Secret path for
the lab.
